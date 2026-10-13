from __future__ import annotations

import io
import json
import sys

import pytest
from PIL import Image, PngImagePlugin
from pypdf import PdfReader, PdfWriter

from gsm_api.print_files import (
    IsolatedPrintNormalizer,
    PrintNormalizationConfigurationError,
    PrintNormalizationError,
    _apply_linux_resource_limits,
    normalize_print_file,
    spool_upload,
)


def _pdf_bytes(*, pages: int = 1, metadata: dict[str, str] | None = None) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    if metadata:
        writer.add_metadata(metadata)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_spool_upload_enforces_stream_limit_and_removes_partial_file(tmp_path):
    destination = tmp_path / "upload.bin"

    with pytest.raises(ValueError, match="size limit"):
        spool_upload(io.BytesIO(b"12345"), destination, max_bytes=4)

    assert not destination.exists()


def test_pdf_is_parsed_bounded_and_metadata_is_removed(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(_pdf_bytes(pages=2, metadata={"/Subject": "private-note"}))

    result = normalize_print_file(
        source,
        filename="paper.pdf",
        claimed_type="application/pdf",
    )

    assert result.path.parent == tmp_path
    assert result.path != source
    assert result.source_media_type == "application/pdf"
    assert result.media_type == "application/pdf"
    assert result.page_count == 2
    assert len(PdfReader(result.path).pages) == 2
    assert b"private-note" not in result.path.read_bytes()


def test_png_is_normalized_to_metadata_free_single_page_pdf(tmp_path):
    source = tmp_path / "plot.png"
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Comment", "private-note")
    Image.new("RGB", (100, 50), "white").save(source, pnginfo=metadata)

    result = normalize_print_file(
        source,
        filename="plot.png",
        claimed_type="image/png",
    )

    assert result.media_type == "application/pdf"
    assert result.page_count == 1
    assert len(PdfReader(result.path).pages) == 1
    assert b"private-note" not in result.path.read_bytes()


def test_jpeg_exif_orientation_is_applied_before_normalization(tmp_path):
    source = tmp_path / "scan.jpg"
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (120, 60), "white").save(source, exif=exif)

    result = normalize_print_file(
        source,
        filename="scan.jpg",
        claimed_type="image/jpeg",
    )
    page = PdfReader(result.path).pages[0]

    assert float(page.mediabox.width) < float(page.mediabox.height)


@pytest.mark.parametrize(
    ("filename", "claimed_type"),
    [
        ("paper.png", "application/pdf"),
        ("paper.pdf", "image/png"),
        ("paper.txt", "application/pdf"),
    ],
)
def test_signature_mime_and_extension_must_agree(
    tmp_path, filename, claimed_type
):
    source = tmp_path / "source"
    source.write_bytes(_pdf_bytes())

    with pytest.raises(ValueError):
        normalize_print_file(
            source,
            filename=filename,
            claimed_type=claimed_type,
        )


def test_encrypted_and_malformed_pdfs_are_rejected(tmp_path):
    encrypted = tmp_path / "encrypted.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=10, height=10)
    writer.encrypt("password")
    with encrypted.open("wb") as output:
        writer.write(output)

    with pytest.raises(ValueError, match="encrypted"):
        normalize_print_file(
            encrypted,
            filename="encrypted.pdf",
            claimed_type="application/pdf",
        )

    malformed = tmp_path / "malformed.pdf"
    malformed.write_bytes(b"%PDF-1.7\nnot-a-pdf")
    with pytest.raises(ValueError, match="malformed"):
        normalize_print_file(
            malformed,
            filename="malformed.pdf",
            claimed_type="application/pdf",
        )


def test_image_decompression_limit_is_enforced(tmp_path, monkeypatch):
    source = tmp_path / "large.png"
    Image.new("RGB", (20, 20), "white").save(source)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)

    with pytest.raises(ValueError, match="pixel limit"):
        normalize_print_file(
            source,
            filename="large.png",
            claimed_type="image/png",
        )


def test_pdf_page_limit_is_enforced(tmp_path):
    source = tmp_path / "long.pdf"
    source.write_bytes(_pdf_bytes(pages=1001))

    with pytest.raises(ValueError, match="page limit"):
        normalize_print_file(
            source,
            filename="long.pdf",
            claimed_type="application/pdf",
        )


def test_non_default_file_and_page_limits_are_enforced(tmp_path):
    source = tmp_path / "two-pages.pdf"
    source.write_bytes(_pdf_bytes(pages=2))

    with pytest.raises(ValueError, match="page limit"):
        normalize_print_file(
            source,
            filename="two-pages.pdf",
            claimed_type="application/pdf",
            max_file_bytes=len(source.read_bytes()),
            max_pages=1,
        )

    with pytest.raises(ValueError, match="size limit"):
        normalize_print_file(
            source,
            filename="two-pages.pdf",
            claimed_type="application/pdf",
            max_file_bytes=len(source.read_bytes()) - 1,
            max_pages=2,
        )


def test_isolated_normalizer_returns_child_process_result(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(_pdf_bytes(pages=2, metadata={"/Subject": "private-note"}))
    normalizer = IsolatedPrintNormalizer(
        timeout_seconds=10,
        memory_limit_mb=512,
        cpu_limit_seconds=5,
    )

    result = normalizer(
        source,
        filename="paper.pdf",
        claimed_type="application/pdf",
    )

    assert result.page_count == 2
    assert result.path.is_file()
    assert b"private-note" not in result.path.read_bytes()


def test_isolated_normalizer_terminates_timeout_and_removes_partial_output(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(_pdf_bytes())
    context = _FakeProcessContext(poll_result=False, worker_result=None)
    normalizer = IsolatedPrintNormalizer(
        timeout_seconds=1,
        memory_limit_mb=512,
        cpu_limit_seconds=5,
        process_context=context,
    )

    with pytest.raises(PrintNormalizationError, match="timed out"):
        normalizer(
            source,
            filename="paper.pdf",
            claimed_type="application/pdf",
        )

    assert context.process.terminate_calls == 1
    assert not context.process.is_alive()
    assert not list(tmp_path.glob("normalized-*.pdf"))


def test_isolated_normalizer_maps_child_exception_without_detail_leak(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(_pdf_bytes())
    context = _FakeProcessContext(
        poll_result=True,
        worker_result=("error", "sensitive parser internals"),
    )
    normalizer = IsolatedPrintNormalizer(
        timeout_seconds=5,
        memory_limit_mb=512,
        cpu_limit_seconds=5,
        process_context=context,
    )

    with pytest.raises(PrintNormalizationError) as raised:
        normalizer(
            source,
            filename="paper.pdf",
            claimed_type="application/pdf",
        )

    assert str(raised.value) == "print file could not be normalized"
    assert "sensitive" not in str(raised.value)
    assert not list(tmp_path.glob("normalized-*.pdf"))


def test_isolated_normalizer_maps_pipe_eof_and_removes_partial_output(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(_pdf_bytes())
    context = _FakeProcessContext(
        poll_result=True,
        worker_result=None,
        receive_error=EOFError(),
    )
    normalizer = IsolatedPrintNormalizer(
        timeout_seconds=5,
        memory_limit_mb=512,
        cpu_limit_seconds=5,
        process_context=context,
    )

    with pytest.raises(PrintNormalizationError, match="could not be normalized"):
        normalizer(
            source,
            filename="paper.pdf",
            claimed_type="application/pdf",
        )

    assert not list(tmp_path.glob("normalized-*.pdf"))


def test_isolated_normalizer_cleans_up_when_worker_cannot_start(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(_pdf_bytes())
    context = _FakeProcessContext(
        poll_result=False,
        worker_result=None,
        start_error=OSError("spawn failed"),
    )
    normalizer = IsolatedPrintNormalizer(
        timeout_seconds=5,
        memory_limit_mb=512,
        cpu_limit_seconds=5,
        process_context=context,
    )

    with pytest.raises(PrintNormalizationError, match="could not start"):
        normalizer(
            source,
            filename="paper.pdf",
            claimed_type="application/pdf",
        )

    assert context.receive.closed
    assert context.send.closed
    assert not list(tmp_path.glob("normalized-*.pdf"))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GSM_PRINT_NORMALIZE_TIMEOUT_SECONDS", "0"),
        ("GSM_PRINT_NORMALIZE_MEMORY_MB", "4096"),
        ("GSM_PRINT_NORMALIZE_CPU_SECONDS", "not-an-integer"),
        ("GSM_PRINT_MAX_FILE_BYTES", "0"),
        ("GSM_PRINT_MAX_PAGES", "10001"),
    ],
)
def test_isolated_normalizer_configuration_fails_closed(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(PrintNormalizationConfigurationError, match=name):
        IsolatedPrintNormalizer.from_environment()


def test_isolated_normalizer_consumes_non_default_print_limits(monkeypatch):
    monkeypatch.setenv("GSM_PRINT_MAX_FILE_BYTES", "123456")
    monkeypatch.setenv("GSM_PRINT_MAX_PAGES", "37")

    normalizer = IsolatedPrintNormalizer.from_environment()

    assert normalizer.max_file_bytes == 123456
    assert normalizer.max_pages == 37


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux RLIMIT only")
def test_linux_worker_applies_address_space_and_cpu_limits(monkeypatch):
    import resource

    calls = []
    monkeypatch.setattr(resource, "setrlimit", lambda kind, limits: calls.append((kind, limits)))

    _apply_linux_resource_limits(memory_limit_mb=256, cpu_limit_seconds=7)

    assert calls == [
        (resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024)),
        (resource.RLIMIT_CPU, (7, 7)),
    ]


class _FakeConnection:
    def __init__(self, *, poll_result: bool, result=None, receive_error=None):
        self._poll_result = poll_result
        self._result = result
        self._receive_error = receive_error
        self.closed = False
        self.maximum_lengths = []

    def poll(self, _timeout):
        return self._poll_result

    def recv_bytes(self, maxlength=None):
        self.maximum_lengths.append(maxlength)
        if self._receive_error is not None:
            raise self._receive_error
        return json.dumps(self._result).encode("utf-8")

    def close(self):
        self.closed = True


class _FakeProcess:
    def __init__(self, *, args, remains_alive, start_error=None):
        self._output_path = args[2]
        self._remains_alive = remains_alive
        self._start_error = start_error
        self._alive = False
        self.exitcode = None
        self.terminate_calls = 0
        self.kill_calls = 0

    def start(self):
        self._output_path.write_bytes(b"partial")
        if self._start_error is not None:
            raise self._start_error
        self._alive = self._remains_alive
        self.exitcode = None if self._alive else 0

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        return None

    def terminate(self):
        self.terminate_calls += 1
        self._alive = False
        self.exitcode = -15

    def kill(self):
        self.kill_calls += 1
        self._alive = False
        self.exitcode = -9


class _FakeProcessContext:
    def __init__(
        self,
        *,
        poll_result: bool,
        worker_result,
        receive_error=None,
        start_error=None,
    ):
        self.receive = _FakeConnection(
            poll_result=poll_result,
            result=worker_result,
            receive_error=receive_error,
        )
        self.send = _FakeConnection(poll_result=False)
        self.process = None
        self._remains_alive = not poll_result
        self._start_error = start_error

    def Pipe(self, *, duplex):
        assert duplex is False
        return self.receive, self.send

    def Process(self, **kwargs):
        self.process = _FakeProcess(
            args=kwargs["args"],
            remains_alive=self._remains_alive,
            start_error=self._start_error,
        )
        return self.process
