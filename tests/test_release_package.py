import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "release"
sys.path.insert(0, str(SCRIPTS))
from build_release import build
from check_public_source import check_file, scan


def git(root, *args):
    return subprocess.run(["git", "-c", f"safe.directory={root.as_posix()}", *args], cwd=root, check=True, capture_output=True)


def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "core.autocrlf", "true")
    (root / "package.json").write_text(json.dumps({"version": "0.3.0-rc.1"}))
    (root / "README.md").write_bytes(b"Public source\n")
    (root / ".gitignore").write_text("var/\n")
    (root / "var").mkdir()
    (root / "var" / "private.txt").write_text("private runtime data")
    (root / "install.sh").write_text("#!/bin/sh\nexit 0\n")
    commit(root)
    return root


def commit(root):
    git(root, "add", "-A")
    git(root, "-c", "user.name=Release test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")


def test_release_contains_only_public_source_with_verified_checksums(tmp_path):
    root = repository(tmp_path)
    output = tmp_path / "artifacts"
    archive = build(root, output, "v0.3.0-rc.1")
    manifest = json.loads((output / "release-manifest.json").read_text())
    assert manifest["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert manifest["size"] == archive.stat().st_size
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        assert len(names) == 4
        assert not any("/var/" in name or "/.git/" in name for name in names)
        assert package.getinfo("gpu-server-manager-0.3.0-rc.1/install.sh").external_attr >> 16 & 0o111
    second = build(root, tmp_path / "repeat")
    assert archive.read_bytes() == second.read_bytes()
    with pytest.raises(ValueError, match="never overwritten"):
        build(root, output)


def test_dirty_tree_and_mismatched_tag_are_refused(tmp_path):
    root = repository(tmp_path)
    with pytest.raises(ValueError, match="tag"):
        build(root, tmp_path / "out", "v0.3.0")
    (root / "README.md").write_text("unreviewed edit")
    with pytest.raises(ValueError, match="clean"):
        build(root, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_existing_checksum_file_is_never_overwritten(tmp_path):
    root = repository(tmp_path)
    output = tmp_path / "out"
    output.mkdir()
    checksums = output / "SHA256SUMS"
    original = b"previous reviewed release checksums\n"
    checksums.write_bytes(original)
    with pytest.raises(ValueError, match="never overwritten"):
        build(root, output)
    assert list(output.iterdir()) == [checksums]
    assert checksums.read_bytes() == original


def test_release_uses_identical_committed_bytes_for_lf_and_crlf_worktrees(tmp_path):
    root = repository(tmp_path)
    assert (root / "README.md").read_bytes() == b"Public source\n"
    first = build(root, tmp_path / "lf")
    git(root, "config", "core.autocrlf", "true")
    # Let Git update its index stat cache while producing a real CRLF checkout.
    (root / "README.md").unlink()
    git(root, "checkout", "--", "README.md")
    assert (root / "README.md").read_bytes() == b"Public source\r\n"
    assert not git(root, "status", "--porcelain").stdout.strip()
    second = build(root, tmp_path / "crlf")
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(second) as package:
        assert package.read("gpu-server-manager-0.3.0-rc.1/README.md") == b"Public source\n"


def test_committed_symlink_is_refused_even_when_checkout_uses_a_regular_file(tmp_path):
    root = repository(tmp_path)
    git(root, "config", "core.symlinks", "false")
    target = subprocess.run(
        ["git", "-c", f"safe.directory={root.as_posix()}", "hash-object", "-w", "--stdin"],
        cwd=root, input=b"README.md", check=True, capture_output=True,
    ).stdout.decode().strip()
    git(root, "update-index", "--add", "--cacheinfo", f"120000,{target},alias.md")
    git(root, "-c", "user.name=Release test", "-c", "user.email=test@example.invalid", "commit", "-m", "link fixture")
    git(root, "checkout", "--", "alias.md")
    assert (root / "alias.md").is_file()
    assert not (root / "alias.md").is_symlink()
    assert not git(root, "status", "--porcelain").stdout.strip()
    with pytest.raises(ValueError, match="regular files only"):
        build(root, tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("filename,content", [
    (".env", b"PASSWORD=private"),
    ("backup.sqlite", b"database"),
    ("settings.py", ("api_key = 'sk-" + "1234567890abcdef" * 3 + "'").encode()),
    ("var/private.txt", b"sensitive runtime data"),
])
def test_accidentally_tracked_sensitive_files_block_release(tmp_path, filename, content):
    root = repository(tmp_path)
    (root / filename).write_bytes(content)
    git(root, "add", "-f", filename)
    git(root, "-c", "user.name=Release test", "-c", "user.email=test@example.invalid", "commit", "-m", "bad fixture")
    with pytest.raises(ValueError, match="scan failed") as error:
        build(root, tmp_path / "out")
    assert content.decode() not in str(error.value)
    assert not (tmp_path / "out").exists()


def test_scan_allows_examples_but_does_not_follow_missing_paths(tmp_path):
    assert check_file(".env.example", b"GSM_API_KEY=\n") == []
    assert check_file("apps/web/app/api/gsm/operations/backups/route.ts", b"export {};") == []
    assert check_file("backups/control.json", b"private data")[0]["rule"] == "private-or-generated-file"
    assert scan(tmp_path, ["missing.py"])[0]["rule"] == "missing-or-symlink"
