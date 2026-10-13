import os
import ast
import json
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.version import Version


ROOT = Path(__file__).resolve().parents[1]


def test_public_release_versions_match_update_checker_and_service_metadata():
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    expected = Version(package["version"])
    web = json.loads((ROOT / "apps/web/package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    assert Version(web["version"]) == expected
    assert Version(lock["version"]) == Version(lock["packages"][""]["version"]) == expected
    assert Version(lock["packages"]["apps/web"]["version"]) == expected
    for path in ["pyproject.toml", "services/print-agent/pyproject.toml"]:
        assert Version(tomllib.loads((ROOT / path).read_text(encoding="utf-8"))["project"]["version"]) == expected
    from gsm_api.releases import CURRENT_VERSION
    assert Version(CURRENT_VERSION) == expected
    for path in ROOT.glob("services/*/gsm_*/main.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in ast.walk(tree):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "FastAPI":
                for keyword in call.keywords:
                    if keyword.arg == "version":
                        assert Version(ast.literal_eval(keyword.value)) == expected, str(path)


def test_runtime_artifacts_declare_the_timezone_database():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    constraints = (ROOT / "requirements/constraints.txt").read_text(encoding="utf-8")
    dockerfile = (ROOT / "services/api/Dockerfile").read_text(encoding="utf-8")

    assert '"tzdata>=' in pyproject
    assert "tzdata==" in constraints
    assert '"tzdata>=' in dockerfile


def test_shanghai_timezone_works_without_a_system_timezone_database():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from datetime import datetime, timedelta; "
            "from zoneinfo import ZoneInfo; "
            "assert datetime(2026, 9, 8, tzinfo=ZoneInfo('Asia/Shanghai'))"
            ".utcoffset() == timedelta(hours=8)",
        ],
        env={**os.environ, "PYTHONTZPATH": ""},
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_required_workspace_files_exist():
    required = [
        "package.json",
        "package-lock.json",
        "pyproject.toml",
        ".env.example",
        "apps/web",
        "services/api",
        "services/server-agent",
        "services/wechat-agent",
        "services/codex-mcp",
        "services/test-agent",
        "scripts/deploy",
    ]
    missing = [path for path in required if not (ROOT / path).exists()]
    assert missing == []


def test_repository_uses_npm_workspace_conventions():
    package_json = (ROOT / "package.json").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert '"packageManager": "npm@' in package_json
    assert "npm ci" in readme
    assert "npm run dev" in readme
    assert not (ROOT / "pnpm-workspace.yaml").exists()
    assert "pnpm " not in readme


def test_production_artifacts_do_not_restore_legacy_human_secrets():
    paths = [
        ROOT / ".env.example",
        ROOT / "README.md",
        ROOT / "scripts/deploy/README.md",
        ROOT / "scripts/deploy/install_ubuntu.sh",
        ROOT / "scripts/deploy/docker-compose.yml",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    for name in [
        "GSM_WEB_API_TOKEN",
        "GSM_USER_ALICE_TOKEN",
        "GSM_USER_BOB_TOKEN",
        "GSM_ADMIN_TOKEN",
        "GSM_CODEX_USER_TOKEN",
    ]:
        assert name not in combined
    assert "change-me" not in (ROOT / ".env.example").read_text(encoding="utf-8")


def test_runtime_backup_ignore_does_not_hide_web_backup_routes(tmp_path):
    (tmp_path / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
    (tmp_path / "empty-excludes").write_bytes(b"")
    git = ["git", "-c", f"safe.directory={tmp_path.as_posix()}", "-c", f"core.excludesfile={tmp_path / 'empty-excludes'}"]
    subprocess.run([*git, "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    assert subprocess.run([*git, "check-ignore", "backups/control.json"], cwd=tmp_path, capture_output=True).returncode == 0
    assert subprocess.run([*git, "check-ignore", "apps/web/app/api/gsm/operations/backups/route.ts"], cwd=tmp_path, capture_output=True).returncode == 1
    routes = ROOT / "apps/web/app/api/gsm/operations/backups"
    assert len(list(routes.rglob("route.ts"))) == 5
