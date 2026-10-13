"""Run on Linux against real directory descriptors; no Docker or host data needed."""
import base64
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gsm_server_agent import personal_data as data


@unittest.skipUnless(os.name == "posix", "Linux directory descriptors required")
class PersonalDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".gsm-data-volume").touch()
        self.env = patch.dict(os.environ, {"GSM_PERSONAL_DATA_ROOT": str(self.root), "GSM_PERSONAL_DATA_DEVICE": "",
            "GSM_DATA_UID": str(os.getuid()), "GSM_DATA_GID": str(os.getgid()), "GSM_DATA_RESERVE_BYTES": "0"})
        self.env.start()
        data.operate("alice", "capacity", {})

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def run_op(self, operation, **value):
        return data.operate("alice", operation, {"path": "result.bin", "upload_id": "a" * 32, "size": 3, **value})

    def test_upload_publish_read_and_no_overwrite(self):
        self.run_op("begin")
        self.run_op("chunk", offset=0, data=base64.b64encode(b"abc").decode())
        self.assertEqual(self.run_op("finish")["container_path"], "/workspace/result.bin")
        self.assertEqual((self.root / "alice/result.bin").read_bytes(), b"abc")
        result = self.run_op("read")
        self.assertEqual(base64.b64decode(result["data"]), b"abc")
        with self.assertRaises(ValueError): self.run_op("begin")
        (self.root / "alice/result.bin").write_bytes(b"changed")
        with self.assertRaises(ValueError): self.run_op("read", version=result["version"])

    def test_incomplete_offset_limit_and_cancel(self):
        self.run_op("begin")
        with self.assertRaises(ValueError): self.run_op("chunk", offset=1, data="YQ==")
        with self.assertRaises(ValueError): self.run_op("chunk", offset=0, data="YWJjZA==")
        with self.assertRaises(ValueError): self.run_op("finish")
        self.run_op("cancel")
        self.assertFalse(list((self.root / "alice").glob(".gsm-upload-*")))

    def test_symlink_hardlink_device_and_traversal_cannot_escape(self):
        outside = self.root / "private.txt"
        outside.write_text("secret")
        (self.root / "alice/link").symlink_to(outside)
        os.link(outside, self.root / "alice/hardlink")
        (self.root / "alice/dirlink").symlink_to(self.root, target_is_directory=True)
        for path in ("../private.txt", "/private.txt", "dirlink/private.txt", "link", "hardlink", "a//b", "a\\b"):
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                self.run_op("read", path=path)
        for username in ("..", "../bob", "/root", "alice/bob"):
            with self.assertRaises(ValueError): data.operate(username, "capacity", {})

    def test_missing_or_wrong_volume_fails_closed(self):
        with patch.dict(os.environ, {"GSM_PERSONAL_DATA_DEVICE": "999:999"}):
            with self.assertRaises(ValueError): self.run_op("begin")
        (self.root / ".gsm-data-volume").unlink()
        with self.assertRaises(ValueError): self.run_op("begin")
        self.assertFalse(list((self.root / "alice").glob(".gsm-upload-*")))

    def test_capacity_floor_and_shared_owner(self):
        self.run_op("begin")
        with patch.dict(os.environ, {"GSM_DATA_RESERVE_BYTES": str(2 ** 63)}):
            with self.assertRaises(ValueError): self.run_op("chunk", offset=0, data="YWJj")
        self.run_op("chunk", offset=0, data="YWJj")
        self.run_op("finish")
        self.assertEqual((self.root / "alice/result.bin").stat().st_uid, os.getuid())

    def test_private_directories_and_atomic_rename_without_overwrite(self):
        data.operate("bob", "mkdir", {"path": "private"})
        self.run_op("mkdir", path="experiments")
        (self.root / "alice/experiments/results.txt").write_text("only alice")
        self.run_op("rename", path="experiments", new_name="project-a")
        self.assertEqual((self.root / "alice/project-a/results.txt").read_text(), "only alice")
        self.assertEqual(data.operate("bob", "list", {})["entries"][0]["name"], "private")
        self.assertEqual(self.run_op("capacity", path="")["data_root"], str(self.root / "alice"))
        self.run_op("mkdir", path="existing")
        for old, new in [("project-a", "existing"), ("project-a", "../bob"), ("../bob/private", "stolen"), ("", "root")]:
            with self.subTest(old=old, new=new), self.assertRaises((ValueError, OSError)):
                self.run_op("rename", path=old, new_name=new)
        self.assertTrue((self.root / "alice/existing").is_dir())


if __name__ == "__main__":
    unittest.main()
