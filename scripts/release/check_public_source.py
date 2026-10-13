#!/usr/bin/env python3
"""Fail closed on private files/obvious credentials; print locations, never values.

This is a release guard, not a guarantee that no sensitive information exists.
Review the candidate and publish a fresh history when the private repo had secrets.
"""
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import re
import subprocess

PRIVATE_PARTS = {".git", ".worktrees", ".ace-tool", ".release-private", "var", "node_modules", ".next", "__pycache__", ".pytest_cache", ".venv", "venv", "dist", "uploads", "服务器配置"}
PRIVATE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".pfx", ".p12", ".kdbx", ".ovpn", ".log", ".zip", ".7z", ".pyc"}
PATTERNS = {
    "provider-api-key": re.compile(rb"\bsk-[A-Za-z0-9_-]{32,}\b"),
    "github-token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "aws-access-id": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private-key-material": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s+[A-Za-z0-9+/=\r\n]{80,}"),
    "proxy-credential-uri": re.compile(rb"\b(?:vless|vmess|trojan|ss)://[A-Za-z0-9+/=@._-]{12,}"),
}


def git_files(root: Path) -> list[str]:
    result = subprocess.run(["git", "-c", f"safe.directory={root.as_posix()}", "ls-files", "-z"], cwd=root, check=True, capture_output=True)
    return sorted(set(path for path in result.stdout.decode("utf-8").split("\0") if path))


def check_file(path: str, content: bytes) -> list[dict]:
    normalized = PurePosixPath(path)
    findings = []
    if (normalized.is_absolute() or ".." in normalized.parts or normalized.parts[0] == "backups" or any(part in PRIVATE_PARTS for part in normalized.parts)
            or normalized.suffix.lower() in PRIVATE_SUFFIXES
            or (normalized.name.startswith(".env") and normalized.name != ".env.example")):
        findings.append({"path": path, "line": 1, "rule": "private-or-generated-file"})
    for rule, pattern in PATTERNS.items():
        for match in pattern.finditer(content):
            findings.append({"path": path, "line": content[:match.start()].count(b"\n") + 1, "rule": rule})
    return findings


def scan(root: Path, files: list[str]) -> list[dict]:
    findings = []
    for name in files:
        path = root / name
        if path.is_symlink() or not path.is_file():
            findings.append({"path": name, "line": 1, "rule": "missing-or-symlink"})
            continue
        findings.extend(check_file(name, path.read_bytes()))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    files = git_files(args.root)
    findings = scan(args.root, files)
    for item in findings:
        print(f"{item['path']}:{item['line']}: {item['rule']}")
    print(f"Scanned {len(files)} tracked files; {len(findings)} findings.")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
