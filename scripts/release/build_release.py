#!/usr/bin/env python3
"""Build a deterministic, runtime-data-free GitHub source release from a clean tree."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_public_source import check_file


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-c", f"safe.directory={root.as_posix()}", *args], cwd=root).decode("utf-8").strip()


def committed_files(root: Path, commit: str) -> dict[str, bytes]:
    """Read canonical Git bytes; never follow links or extract onto the host."""
    data = subprocess.check_output(
        ["git", "-c", f"safe.directory={root.as_posix()}", "-c", "core.autocrlf=false",
         "-c", "core.eol=lf", "archive", "--format=tar", commit],
        cwd=root,
    )
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for member in archive:
            name = member.name.rstrip("/") if member.isdir() else member.name
            path = PurePosixPath(name)
            if (not name or path.is_absolute() or "\\" in name or ":" in name
                    or any(part in {"", ".", ".."} for part in name.split("/"))):
                raise ValueError("Unsafe path in committed source archive")
            if member.isdir():
                continue
            if not member.isfile() or name in files:
                raise ValueError("Committed source must contain regular files only; links are not allowed")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("Unreadable file in committed source archive")
            with stream:
                files[name] = stream.read()
    return dict(sorted(files.items()))


def build(root: Path, output: Path, tag: str | None = None) -> Path:
    if git(root, "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("Commit reviewed changes before building a release; the work tree must be clean")
    commit = git(root, "rev-parse", "HEAD")
    files = committed_files(root, commit)
    if "package.json" not in files:
        raise ValueError("Committed source archive must contain package.json")
    version = json.loads(files["package.json"])["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?", version):
        raise ValueError("Unsupported release version")
    if tag is not None and tag != "v" + version:
        raise ValueError("Release tag must match package.json version")
    findings = [finding for name, content in files.items() for finding in check_file(name, content)]
    if findings:
        raise ValueError("Public source scan failed: " + "; ".join(f"{v['path']}:{v['line']} ({v['rule']})" for v in findings))
    output.mkdir(parents=True, exist_ok=True)
    stem = f"gpu-server-manager-{version}"
    package = output / f"{stem}-source.zip"
    if any(path.exists() or path.is_symlink() for path in (
        package, output / "release-manifest.json", output / "SHA256SUMS"
    )):
        raise ValueError("Choose an empty output directory; existing release assets are never overwritten")
    with zipfile.ZipFile(package, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in files.items():
            info = zipfile.ZipInfo(f"{stem}/{name}", date_time=(2020, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (0o100755 if name.endswith(".sh") else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    manifest = {"schema_version": 1, "product": "gpu-server-manager", "version": version,
                "channel": "prerelease" if "-" in version else "stable", "archive": package.name,
                "sha256": digest, "size": package.stat().st_size, "source_commit": commit,
                "install_mode": "docker-compose-source", "automatic_install": True,
                "update_protocol": 1, "schema_revision": 1,
                "python_versions": ["3.11", "3.12"], "includes_runtime_data": False}
    manifest_path = output / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (output / "SHA256SUMS").write_text(f"{digest}  {package.name}\n{manifest_sha}  release-manifest.json\n", encoding="utf-8", newline="\n")
    return package


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tag")
    args = parser.parse_args()
    try:
        print(build(args.root.resolve(), args.output.resolve(), args.tag))
        return 0
    except (ValueError, OSError, tarfile.TarError, subprocess.CalledProcessError) as exc:
        print(f"Release build refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
