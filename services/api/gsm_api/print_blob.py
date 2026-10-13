from __future__ import annotations

import base64
import hashlib
import os
import re
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


_CHUNK_SIZE = 64 * 1024
_MAGIC = b"GSMPRINT\x01"
_NONCE_SIZE = 12
_TAG_SIZE = 16
_HEADER_SIZE = len(_MAGIC) + _NONCE_SIZE
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class StoredPrintBlob:
    key: str
    plaintext_size: int
    sha256: str


class PrintBlobBackend(Protocol):
    def put(self, key: str, chunks: Iterable[bytes]) -> None: ...

    def iter_bytes(self, key: str) -> Iterator[bytes]: ...

    def delete(self, key: str) -> None: ...


class LocalPrintBlobBackend:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.directory.chmod(0o700)
        except OSError:
            pass

    def put(self, key: str, chunks: Iterable[bytes]) -> None:
        destination = self._path(key)
        temporary = self.directory / f".{key}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("print blob chunks must be bytes")
                    if chunk:
                        stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            self._sync_directory()
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise

    def iter_bytes(self, key: str) -> Iterator[bytes]:
        with self._path(key).open("rb") as stream:
            while chunk := stream.read(_CHUNK_SIZE):
                yield chunk

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def _path(self, key: str) -> Path:
        if (
            not key
            or key in {".", ".."}
            or "\0" in key
            or "/" in key
            or "\\" in key
            or _DRIVE_PREFIX.match(key)
        ):
            raise ValueError("print blob key must be opaque")
        return self.directory / key

    def _sync_directory(self) -> None:
        if not hasattr(os, "O_DIRECTORY"):
            return
        try:
            descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class EncryptedPrintBlobStore:
    def __init__(self, backend: PrintBlobBackend, key: str):
        self.backend = backend
        self._key = _decode_key(key)

    def put(self, source: BinaryIO) -> StoredPrintBlob:
        blob_key = uuid.uuid4().hex
        digest = hashlib.sha256()
        plaintext_size = 0

        def encrypted_chunks() -> Iterator[bytes]:
            nonlocal plaintext_size
            nonce = os.urandom(_NONCE_SIZE)
            header = _MAGIC + nonce
            encryptor = Cipher(
                algorithms.AES(self._key), modes.GCM(nonce)
            ).encryptor()
            encryptor.authenticate_additional_data(header)
            yield header
            while chunk := source.read(_CHUNK_SIZE):
                if not isinstance(chunk, bytes):
                    raise TypeError("print source must be a binary stream")
                plaintext_size += len(chunk)
                digest.update(chunk)
                encrypted = encryptor.update(chunk)
                if encrypted:
                    yield encrypted
            final = encryptor.finalize()
            if final:
                yield final
            yield encryptor.tag

        self.backend.put(blob_key, encrypted_chunks())
        return StoredPrintBlob(
            key=blob_key,
            plaintext_size=plaintext_size,
            sha256=digest.hexdigest(),
        )

    def iter_plaintext(self, key: str) -> Iterator[bytes]:
        chunks = iter(self.backend.iter_bytes(key))
        buffer = bytearray()
        while len(buffer) < _HEADER_SIZE:
            try:
                buffer.extend(next(chunks))
            except StopIteration as exc:
                raise ValueError("encrypted print blob is truncated") from exc

        header = bytes(buffer[:_HEADER_SIZE])
        if not header.startswith(_MAGIC):
            raise ValueError("encrypted print blob has an invalid header")
        nonce = header[len(_MAGIC) :]
        decryptor = Cipher(
            algorithms.AES(self._key), modes.GCM(nonce)
        ).decryptor()
        decryptor.authenticate_additional_data(header)
        initial_ciphertext = bytes(buffer[_HEADER_SIZE:])
        tail = b""

        def ciphertext_chunks() -> Iterator[bytes]:
            if initial_ciphertext:
                yield initial_ciphertext
            yield from chunks

        try:
            for chunk in ciphertext_chunks():
                combined = tail + chunk
                if len(combined) <= _TAG_SIZE:
                    tail = combined
                    continue
                ciphertext, tail = combined[:-_TAG_SIZE], combined[-_TAG_SIZE:]
                plaintext = decryptor.update(ciphertext)
                if plaintext:
                    yield plaintext
            if len(tail) != _TAG_SIZE:
                raise ValueError("encrypted print blob is truncated")
            final = decryptor.finalize_with_tag(tail)
        except InvalidTag as exc:
            raise ValueError("encrypted print blob authentication failed") from exc
        if final:
            yield final

    def delete(self, key: str) -> None:
        self.backend.delete(key)


def _decode_key(value: str) -> bytes:
    if not value:
        raise ValueError("print file encryption key is required")
    try:
        decoded = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("print file encryption key is malformed") from exc
    if len(decoded) != 32:
        raise ValueError("print file encryption key must contain 32 bytes")
    return decoded


__all__ = [
    "EncryptedPrintBlobStore",
    "LocalPrintBlobBackend",
    "PrintBlobBackend",
    "StoredPrintBlob",
]
