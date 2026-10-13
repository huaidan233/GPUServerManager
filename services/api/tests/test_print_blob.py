from __future__ import annotations

import base64
import hashlib
import io

import pytest

from gsm_api.print_blob import EncryptedPrintBlobStore, LocalPrintBlobBackend


PRINT_KEY = base64.urlsafe_b64encode(b"p" * 32).decode("ascii")
OTHER_KEY = base64.urlsafe_b64encode(b"q" * 32).decode("ascii")


def test_encrypted_blob_round_trip_leaves_no_plaintext(tmp_path):
    backend = LocalPrintBlobBackend(tmp_path / "blobs")
    store = EncryptedPrintBlobStore(backend, key=PRINT_KEY)

    saved = store.put(io.BytesIO(b"private-manuscript"))

    ciphertext = (tmp_path / "blobs" / saved.key).read_bytes()
    assert b"private-manuscript" not in ciphertext
    assert b"".join(store.iter_plaintext(saved.key)) == b"private-manuscript"
    assert saved.plaintext_size == len(b"private-manuscript")
    assert saved.sha256 == hashlib.sha256(b"private-manuscript").hexdigest()


def test_local_backend_removes_partial_file_when_stream_fails(tmp_path):
    backend = LocalPrintBlobBackend(tmp_path / "blobs")

    def broken_chunks():
        yield b"partial"
        raise RuntimeError("source failed")

    with pytest.raises(RuntimeError, match="source failed"):
        backend.put("opaque-key", broken_chunks())

    assert list((tmp_path / "blobs").iterdir()) == []


@pytest.mark.parametrize("key", ["../secret", "folder/file", r"C:\secret", ".", "..", "bad\0key"])
def test_local_backend_rejects_non_opaque_keys(tmp_path, key):
    backend = LocalPrintBlobBackend(tmp_path / "blobs")

    with pytest.raises(ValueError):
        backend.put(key, [b"data"])
    with pytest.raises(ValueError):
        list(backend.iter_bytes(key))
    with pytest.raises(ValueError):
        backend.delete(key)


def test_wrong_key_and_tampered_tag_are_rejected(tmp_path):
    backend = LocalPrintBlobBackend(tmp_path / "blobs")
    store = EncryptedPrintBlobStore(backend, key=PRINT_KEY)
    saved = store.put(io.BytesIO(b"confidential" * 10_000))

    with pytest.raises(ValueError, match="authentication"):
        b"".join(
            EncryptedPrintBlobStore(backend, key=OTHER_KEY).iter_plaintext(
                saved.key
            )
        )

    blob_path = tmp_path / "blobs" / saved.key
    tampered = bytearray(blob_path.read_bytes())
    tampered[-1] ^= 1
    blob_path.write_bytes(tampered)
    with pytest.raises(ValueError, match="authentication"):
        b"".join(store.iter_plaintext(saved.key))


def test_delete_removes_encrypted_blob(tmp_path):
    backend = LocalPrintBlobBackend(tmp_path / "blobs")
    store = EncryptedPrintBlobStore(backend, key=PRINT_KEY)
    saved = store.put(io.BytesIO(b"delete-me"))

    store.delete(saved.key)

    assert not (tmp_path / "blobs" / saved.key).exists()
