from __future__ import annotations

import multiprocessing
import json
import os
import sys
import time
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable

from PIL import Image, ImageOps, UnidentifiedImageError
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError


MAX_PRINT_FILE_BYTES = 100 * 1024 * 1024
MAX_PRINT_PAGES = 1000
MAX_CONFIGURED_PRINT_FILE_BYTES = 1024 * 1024 * 1024
MAX_CONFIGURED_PRINT_PAGES = 10_000
DEFAULT_NORMALIZATION_TIMEOUT_SECONDS = 30
DEFAULT_NORMALIZATION_MEMORY_MB = 512
DEFAULT_NORMALIZATION_CPU_SECONDS = 20
MAX_NORMALIZATION_TIMEOUT_SECONDS = 120
MIN_NORMALIZATION_MEMORY_MB = 128
MAX_NORMALIZATION_MEMORY_MB = 2048
MAX_NORMALIZATION_CPU_SECONDS = 120
# Covers an A3 page at roughly 300 DPI while bounding decoded image memory.
Image.MAX_IMAGE_PIXELS = 25_000_000

_CHUNK_SIZE = 64 * 1024
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_EXPECTED_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


@dataclass(frozen=True)
class NormalizedPrintFile:
    path: Path
    source_media_type: str
    media_type: str
    page_count: int


class PrintNormalizationConfigurationError(RuntimeError):
    pass


class PrintNormalizationError(ValueError):
    pass


class IsolatedPrintNormalizer:
    def __init__(
        self,
        *,
        timeout_seconds: int,
        memory_limit_mb: int,
        cpu_limit_seconds: int,
        max_file_bytes: int = MAX_PRINT_FILE_BYTES,
        max_pages: int = MAX_PRINT_PAGES,
        process_context: Any | None = None,
        worker_target: Callable[..., None] | None = None,
    ) -> None:
        _validate_normalization_limits(
            timeout_seconds=timeout_seconds,
            memory_limit_mb=memory_limit_mb,
            cpu_limit_seconds=cpu_limit_seconds,
        )
        _validate_print_limits(max_file_bytes=max_file_bytes, max_pages=max_pages)
        self._timeout_seconds = timeout_seconds
        self._memory_limit_mb = memory_limit_mb
        self._cpu_limit_seconds = cpu_limit_seconds
        self._max_file_bytes = max_file_bytes
        self._max_pages = max_pages
        self._context = (
            process_context
            if process_context is not None
            else multiprocessing.get_context("spawn")
        )
        self._worker_target = (
            worker_target if worker_target is not None else _normalization_worker
        )

    @classmethod
    def from_environment(cls) -> IsolatedPrintNormalizer:
        return cls(
            timeout_seconds=_bounded_environment_int(
                "GSM_PRINT_NORMALIZE_TIMEOUT_SECONDS",
                DEFAULT_NORMALIZATION_TIMEOUT_SECONDS,
                minimum=1,
                maximum=MAX_NORMALIZATION_TIMEOUT_SECONDS,
            ),
            memory_limit_mb=_bounded_environment_int(
                "GSM_PRINT_NORMALIZE_MEMORY_MB",
                DEFAULT_NORMALIZATION_MEMORY_MB,
                minimum=MIN_NORMALIZATION_MEMORY_MB,
                maximum=MAX_NORMALIZATION_MEMORY_MB,
            ),
            cpu_limit_seconds=_bounded_environment_int(
                "GSM_PRINT_NORMALIZE_CPU_SECONDS",
                DEFAULT_NORMALIZATION_CPU_SECONDS,
                minimum=1,
                maximum=MAX_NORMALIZATION_CPU_SECONDS,
            ),
            max_file_bytes=_bounded_environment_int(
                "GSM_PRINT_MAX_FILE_BYTES",
                MAX_PRINT_FILE_BYTES,
                minimum=1,
                maximum=MAX_CONFIGURED_PRINT_FILE_BYTES,
            ),
            max_pages=_bounded_environment_int(
                "GSM_PRINT_MAX_PAGES",
                MAX_PRINT_PAGES,
                minimum=1,
                maximum=MAX_CONFIGURED_PRINT_PAGES,
            ),
        )

    @property
    def max_file_bytes(self) -> int:
        return self._max_file_bytes

    @property
    def max_pages(self) -> int:
        return self._max_pages

    def __call__(
        self,
        path: Path,
        *,
        filename: str,
        claimed_type: str,
    ) -> NormalizedPrintFile:
        source_path = Path(path)
        output_path = source_path.parent / f"normalized-{uuid.uuid4().hex}.pdf"
        receive_result = None
        send_result = None
        process = None
        deadline = time.monotonic() + self._timeout_seconds
        try:
            receive_result, send_result = self._context.Pipe(duplex=False)
            process = self._context.Process(
                name="gsm-print-normalizer",
                target=self._worker_target,
                args=(
                    send_result,
                    source_path,
                    output_path,
                    filename,
                    claimed_type,
                    self._max_file_bytes,
                    self._max_pages,
                    self._memory_limit_mb,
                    self._cpu_limit_seconds,
                ),
            )
            try:
                process.start()
            except Exception as exc:
                raise PrintNormalizationError(
                    "print file normalization worker could not start"
                ) from exc
            finally:
                send_result.close()
                send_result = None

            try:
                has_result = receive_result.poll(_remaining_seconds(deadline))
            except (EOFError, OSError) as exc:
                raise PrintNormalizationError(
                    "print file could not be normalized"
                ) from exc
            if not has_result:
                _terminate_process(process)
                raise PrintNormalizationError("print file normalization timed out")
            try:
                result_bytes = receive_result.recv_bytes(4096)
                result = json.loads(result_bytes.decode("utf-8"))
            except (EOFError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PrintNormalizationError(
                    "print file could not be normalized"
                ) from exc
            process.join(timeout=_remaining_seconds(deadline))
            if process.is_alive():
                _terminate_process(process)
                raise PrintNormalizationError(
                    "print file normalization worker did not exit"
                )
            if process.exitcode != 0:
                raise PrintNormalizationError("print file could not be normalized")
            return _result_from_worker(
                result,
                output_path,
                max_pages=self._max_pages,
            )
        except BaseException:
            output_path.unlink(missing_ok=True)
            if process is not None and process.is_alive():
                _terminate_process(process)
            raise
        finally:
            if send_result is not None:
                send_result.close()
            if receive_result is not None:
                receive_result.close()


def spool_upload(
    source: BinaryIO,
    destination: Path,
    *,
    max_bytes: int,
) -> int:
    if max_bytes < 1:
        raise ValueError("print upload size limit must be positive")
    destination = Path(destination)
    total = 0
    try:
        with destination.open("xb") as output:
            try:
                destination.chmod(0o600)
            except OSError:
                pass
            while chunk := source.read(_CHUNK_SIZE):
                if not isinstance(chunk, bytes):
                    raise TypeError("print upload must be a binary stream")
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("print upload exceeds size limit")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return total


def normalize_print_file(
    path: Path,
    *,
    filename: str,
    claimed_type: str,
    max_file_bytes: int = MAX_PRINT_FILE_BYTES,
    max_pages: int = MAX_PRINT_PAGES,
) -> NormalizedPrintFile:
    _validate_print_limits(max_file_bytes=max_file_bytes, max_pages=max_pages)
    path = Path(path)
    if path.stat().st_size > max_file_bytes:
        raise ValueError("print upload exceeds size limit")
    source_type = _detect_and_validate_type(
        path,
        filename=filename,
        claimed_type=claimed_type,
    )
    output_path = path.parent / f"normalized-{uuid.uuid4().hex}.pdf"
    return _normalize_print_file_to(
        path,
        output_path,
        source_type=source_type,
        max_pages=max_pages,
    )


def _normalize_print_file_to(
    path: Path,
    output_path: Path,
    *,
    source_type: str,
    max_pages: int,
) -> NormalizedPrintFile:
    try:
        if source_type == "application/pdf":
            page_count = _normalize_pdf(path, output_path, max_pages=max_pages)
        else:
            page_count = _normalize_image(path, output_path, source_type)
        try:
            output_path.chmod(0o600)
        except OSError:
            pass
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise
    return NormalizedPrintFile(
        path=output_path,
        source_media_type=source_type,
        media_type="application/pdf",
        page_count=page_count,
    )


def _detect_and_validate_type(
    path: Path,
    *,
    filename: str,
    claimed_type: str,
) -> str:
    if not filename or "\0" in filename:
        raise ValueError("print filename is invalid")
    expected_type = _EXPECTED_TYPES.get(Path(filename).suffix.lower())
    if expected_type is None or claimed_type != expected_type:
        raise ValueError("print filename and media type do not agree")
    with path.open("rb") as source:
        signature = source.read(8)
    if signature.startswith(b"%PDF-"):
        detected_type = "application/pdf"
    elif signature.startswith(_PNG_MAGIC):
        detected_type = "image/png"
    elif signature.startswith(_JPEG_MAGIC):
        detected_type = "image/jpeg"
    else:
        raise ValueError("unsupported or malformed print file")
    if detected_type != expected_type:
        raise ValueError("print signature and media type do not agree")
    return detected_type


def _normalize_pdf(source: Path, destination: Path, *, max_pages: int) -> int:
    try:
        reader = PdfReader(source, strict=True)
        if reader.is_encrypted:
            raise ValueError("encrypted PDF files are not supported")
        page_count = len(reader.pages)
        if page_count < 1:
            raise ValueError("PDF must contain at least one page")
        if page_count > max_pages:
            raise ValueError("PDF exceeds page limit")
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        writer.add_metadata({})
        with destination.open("xb") as output:
            writer.write(output)
    except ValueError:
        raise
    except (PdfReadError, OSError, TypeError, KeyError) as exc:
        raise ValueError("PDF is malformed") from exc
    return page_count


def _normalize_image(
    source: Path,
    destination: Path,
    source_type: str,
) -> int:
    expected_format = "PNG" if source_type == "image/png" else "JPEG"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as probe:
                if probe.format != expected_format:
                    raise ValueError("image decoder and media type do not agree")
                probe.verify()
            with Image.open(source) as image:
                image.load()
                oriented = ImageOps.exif_transpose(image)
                converted = oriented.convert("RGB")
                clean = Image.new("RGB", converted.size)
                clean.paste(converted)
                converted.close()
                if oriented is not image:
                    oriented.close()
                clean.save(destination, format="PDF", resolution=300.0)
                clean.close()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("image exceeds pixel limit") from exc
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError("image is malformed") from exc
    return 1


def _normalization_worker(
    result_connection: Any,
    source_path: Path,
    output_path: Path,
    filename: str,
    claimed_type: str,
    max_file_bytes: int,
    max_pages: int,
    memory_limit_mb: int,
    cpu_limit_seconds: int,
) -> None:
    try:
        os.umask(0o077)
        _apply_linux_resource_limits(
            memory_limit_mb=memory_limit_mb,
            cpu_limit_seconds=cpu_limit_seconds,
        )
        if source_path.stat().st_size > max_file_bytes:
            raise ValueError("print upload exceeds size limit")
        source_type = _detect_and_validate_type(
            source_path,
            filename=filename,
            claimed_type=claimed_type,
        )
        normalized = _normalize_print_file_to(
            source_path,
            output_path,
            source_type=source_type,
            max_pages=max_pages,
        )
        _send_worker_result(
            result_connection,
            {
                "status": "ok",
                "source_media_type": normalized.source_media_type,
                "media_type": normalized.media_type,
                "page_count": normalized.page_count,
            },
        )
    except ValueError as exc:
        output_path.unlink(missing_ok=True)
        message = str(exc)
        _send_worker_result(
            result_connection,
            {
                "status": "invalid",
                "message": message if message in _SAFE_NORMALIZATION_ERRORS else None,
            },
        )
    except BaseException:
        output_path.unlink(missing_ok=True)
        try:
            _send_worker_result(result_connection, {"status": "error"})
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        result_connection.close()


def _result_from_worker(
    result: Any,
    output_path: Path,
    *,
    max_pages: int,
) -> NormalizedPrintFile:
    if not isinstance(result, dict):
        raise PrintNormalizationError("print file could not be normalized")
    status_name = result.get("status")
    if status_name == "invalid" and set(result) == {"status", "message"}:
        message = result.get("message")
        raise PrintNormalizationError(
            message
            if message in _SAFE_NORMALIZATION_ERRORS
            else "print file could not be normalized"
        )
    if status_name != "ok" or set(result) != {
        "status",
        "source_media_type",
        "media_type",
        "page_count",
    }:
        raise PrintNormalizationError("print file could not be normalized")
    source_type = result.get("source_media_type")
    media_type = result.get("media_type")
    page_count = result.get("page_count")
    if (
        source_type not in {"application/pdf", "image/jpeg", "image/png"}
        or media_type != "application/pdf"
        or not isinstance(page_count, int)
        or not 1 <= page_count <= max_pages
        or not output_path.is_file()
    ):
        raise PrintNormalizationError("print file could not be normalized")
    return NormalizedPrintFile(
        path=output_path,
        source_media_type=source_type,
        media_type=media_type,
        page_count=page_count,
    )


def _send_worker_result(result_connection: Any, payload: dict[str, Any]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > 4096:
        raise ValueError("print normalization result is too large")
    result_connection.send_bytes(encoded)


def _apply_linux_resource_limits(
    *,
    memory_limit_mb: int,
    cpu_limit_seconds: int,
) -> None:
    if not sys.platform.startswith("linux"):
        return
    import resource

    memory_bytes = memory_limit_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(
        resource.RLIMIT_CPU,
        (cpu_limit_seconds, cpu_limit_seconds),
    )


def _terminate_process(process: Any) -> None:
    if not process.is_alive():
        process.join(timeout=0)
        return
    process.terminate()
    process.join(timeout=1)
    if process.is_alive():
        kill = getattr(process, "kill", None)
        if kill is not None:
            kill()
        process.join(timeout=1)


def _remaining_seconds(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _bounded_environment_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    configured = os.environ.get(name, str(default))
    try:
        value = int(configured)
    except ValueError as exc:
        raise PrintNormalizationConfigurationError(
            f"{name} must be an integer"
        ) from exc
    if not minimum <= value <= maximum:
        raise PrintNormalizationConfigurationError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


def _validate_normalization_limits(
    *,
    timeout_seconds: int,
    memory_limit_mb: int,
    cpu_limit_seconds: int,
) -> None:
    for name, value, minimum, maximum in (
        (
            "timeout_seconds",
            timeout_seconds,
            1,
            MAX_NORMALIZATION_TIMEOUT_SECONDS,
        ),
        (
            "memory_limit_mb",
            memory_limit_mb,
            MIN_NORMALIZATION_MEMORY_MB,
            MAX_NORMALIZATION_MEMORY_MB,
        ),
        (
            "cpu_limit_seconds",
            cpu_limit_seconds,
            1,
            MAX_NORMALIZATION_CPU_SECONDS,
        ),
    ):
        if not isinstance(value, int) or not minimum <= value <= maximum:
            raise PrintNormalizationConfigurationError(
                f"{name} must be between {minimum} and {maximum}"
            )


def _validate_print_limits(*, max_file_bytes: int, max_pages: int) -> None:
    for name, value, maximum in (
        ("max_file_bytes", max_file_bytes, MAX_CONFIGURED_PRINT_FILE_BYTES),
        ("max_pages", max_pages, MAX_CONFIGURED_PRINT_PAGES),
    ):
        if not isinstance(value, int) or not 1 <= value <= maximum:
            raise PrintNormalizationConfigurationError(
                f"{name} must be between 1 and {maximum}"
            )


_SAFE_NORMALIZATION_ERRORS = frozenset(
    {
        "print upload exceeds size limit",
        "print filename is invalid",
        "print filename and media type do not agree",
        "unsupported or malformed print file",
        "print signature and media type do not agree",
        "encrypted PDF files are not supported",
        "PDF must contain at least one page",
        "PDF exceeds page limit",
        "PDF is malformed",
        "image decoder and media type do not agree",
        "image exceeds pixel limit",
        "image is malformed",
    }
)


__all__ = [
    "MAX_PRINT_FILE_BYTES",
    "MAX_PRINT_PAGES",
    "IsolatedPrintNormalizer",
    "NormalizedPrintFile",
    "PrintNormalizationConfigurationError",
    "PrintNormalizationError",
    "normalize_print_file",
    "spool_upload",
]
