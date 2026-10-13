"""Read public GitHub releases and stage verified source packages; never install them."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit

import httpx

CURRENT_VERSION = "0.3.0-rc.6"
PRODUCT = "gpu-server-manager"
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
_VERSION = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(alpha|beta|rc)\.(0|[1-9]\d*))?")
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
_HOSTS = {"api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_lock = threading.Lock()


class ReleaseError(ValueError):
    pass


def version_key(version: str) -> tuple[int, ...]:
    match = _VERSION.fullmatch(version)
    if not match:
        raise ReleaseError("版本号必须使用 x.y.z 或 x.y.z-rc.N / beta.N / alpha.N。")
    major, minor, patch, label, serial = match.groups()
    return (int(major), int(minor), int(patch), {"alpha": 0, "beta": 1, "rc": 2, None: 3}[label], int(serial or 0))


def validate_repo(repo: str) -> str:
    if not _REPO.fullmatch(repo) or any(part in {".", ".."} or part.endswith(".git") for part in repo.split("/")):
        raise ReleaseError("更新仓库必须使用 GitHub owner/repository 格式。")
    return repo


def _trusted_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme == "https" and parsed.hostname in _HOSTS and parsed.port in (None, 443) and not parsed.username and not parsed.password and not parsed.fragment
    except ValueError:
        valid = False
    if not valid:
        raise ReleaseError("更新下载地址不在允许的 GitHub HTTPS 域名中。")


def _download(client: httpx.Client, url: str, *, limit: int, sink=None) -> bytes:
    """Bound both compressed and decoded response size, and revalidate every redirect."""
    for _ in range(6):
        _trusted_url(url)
        with client.stream("GET", url, follow_redirects=False, headers={
            "Accept": "application/vnd.github+json" if urlsplit(url).hostname == "api.github.com" else "application/octet-stream",
            "Accept-Encoding": "identity", "User-Agent": "GPU-Server-Manager-update-check",
            "X-GitHub-Api-Version": "2026-03-10",
        }) as response:
            if response.is_redirect:
                location = response.headers.get("location", "")
                if not location:
                    raise ReleaseError("GitHub 返回了无效重定向。")
                url = str(response.url.join(location))
                continue
            response.raise_for_status()
            if int(response.headers.get("content-length", "0")) > limit:
                raise ReleaseError("发布文件超过允许大小。")
            result = bytearray()
            total = 0
            for chunk in response.iter_bytes(chunk_size=65536):
                total += len(chunk)
                if total > limit:
                    raise ReleaseError("发布文件超过允许大小。")
                if sink is None:
                    result.extend(chunk)
                else:
                    sink.write(chunk)
            return bytes(result)
    raise ReleaseError("GitHub 重定向次数过多。")


def _asset(release: dict, repo: str, name: str) -> dict:
    expected = f"https://github.com/{repo}/releases/download/{release['tag_name']}/{name}"
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        raise ReleaseError("发布附件格式错误。")
    matches = [item for item in assets if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1 or matches[0].get("browser_download_url") != expected:
        raise ReleaseError("发布附件缺失或下载地址与仓库不符。")
    return matches[0]


def release_manifest(client: httpx.Client, repo: str, release: dict) -> dict:
    version = release["tag_name"].removeprefix("v")
    version_key(version)
    manifest_asset = _asset(release, repo, "release-manifest.json")
    manifest = json.loads(_download(client, manifest_asset["browser_download_url"], limit=32768))
    name = f"gpu-server-manager-{version}-source.zip"
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("product") != PRODUCT
            or manifest.get("version") != version or manifest.get("archive") != name
            or manifest.get("install_mode") != "docker-compose-source"
            or not re.fullmatch(r"[a-f0-9]{64}", str(manifest.get("sha256", "")))
            or type(manifest.get("size")) is not int or not 0 < manifest["size"] <= MAX_ARCHIVE_BYTES):
        raise ReleaseError("发布清单内容不合法或版本不一致。")
    archive = _asset(release, repo, name)
    if archive.get("size") != manifest["size"]:
        raise ReleaseError("发布包大小与清单不一致。")
    return {**manifest, "download_url": archive["browser_download_url"]}


def _latest_release(client: httpx.Client, repo: str, channel: str) -> dict | None:
    # /latest deliberately excludes prereleases; the opt-in channel lists both.
    endpoint = "latest" if channel == "stable" else "?per_page=100"
    url = f"https://api.github.com/repos/{repo}/releases" + ("/" + endpoint if channel == "stable" else endpoint)
    try:
        payload = json.loads(_download(client, url, limit=2 * 1024 * 1024))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise ReleaseError("仓库不可访问或尚无可用 Release；请检查仓库是否公开及发布通道。") from None
        raise
    candidates = [payload] if channel == "stable" else payload
    if not isinstance(candidates, list):
        raise ReleaseError("GitHub 返回了无效的发布列表。")
    valid = []
    for item in candidates:
        if not isinstance(item, dict) or item.get("draft") or not item.get("published_at"):
            continue
        if channel == "stable" and item.get("prerelease"):
            continue
        try:
            key = version_key(item.get("tag_name", ""))
        except (ReleaseError, TypeError):
            continue
        if channel == "stable" and key[3] != 3:
            continue
        valid.append((key, item))
    return max(valid, key=lambda pair: pair[0])[1] if valid else None


def check_release(repo: str, channel: str = "stable", *, client: httpx.Client | None = None) -> dict:
    base = {"current_version": CURRENT_VERSION, "repository": repo, "channel": channel, "automatic_install": False}
    if not repo:
        return {**base, "status": "unconfigured", "message": "发布仓库尚未配置。管理员可在 api.env 设置 GSM_UPDATE_REPOSITORY。"}
    try:
        validate_repo(repo)
        if channel not in {"stable", "prerelease"}:
            raise ReleaseError("更新通道必须是 stable 或 prerelease。")
        if client is None:
            with httpx.Client(timeout=httpx.Timeout(15, connect=5), trust_env=False) as owned:
                return check_release(repo, channel, client=owned)
        release = _latest_release(client, repo, channel)
        if release is None:
            return {**base, "status": "no_release", "message": "所选通道暂无支持的已发布版本。"}
        version = release["tag_name"].removeprefix("v")
        manifest = release_manifest(client, repo, release)
        return {**base, "status": "available" if version_key(version) > version_key(CURRENT_VERSION) else "current",
                "latest_version": version, "published_at": release["published_at"],
                "release_url": f"https://github.com/{repo}/releases/tag/{release['tag_name']}",
                "manifest": manifest, "message": "发布包已具备校验信息。升级前请完成备份并阅读发布说明。"}
    except (ReleaseError, httpx.HTTPError, ValueError, TypeError, KeyError):
        # Do not send upstream bodies, proxy credentials, or signed URLs to browsers/logs.
        return {**base, "status": "error", "message": "无法确认新版本：请检查公开仓库、网络、发布通道和附件完整性。稍后重试。"}


def cached_release_check() -> dict:
    repo = os.environ.get("GSM_UPDATE_REPOSITORY", "").strip()
    channel = os.environ.get("GSM_UPDATE_CHANNEL", "stable").strip()
    key = (repo, channel)
    # Single flight: repeated browser clicks cannot create a burst of GitHub requests.
    with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            return dict(cached[1], cached=True)
        value = check_release(repo, channel)
        _cache.clear()
        _cache[key] = (time.monotonic(), value)
        return dict(value, cached=False)


def stage_release(repo: str, channel: str, destination: Path, *, client: httpx.Client | None = None) -> Path:
    """Download to a new private directory. Never unpack, run, or overwrite existing files."""
    if client is None:
        with httpx.Client(timeout=httpx.Timeout(60, connect=10), trust_env=False) as owned:
            return stage_release(repo, channel, destination, client=owned)
    result = check_release(repo, channel, client=client)
    if result["status"] != "available":
        raise ReleaseError("没有可暂存的新版本，或发布校验未通过。")
    manifest = result["manifest"]
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = destination / f"gpu-server-manager-{manifest['version']}"
    if target.exists():
        raise ReleaseError("目标版本目录已存在；请检查已下载内容或指定新的暂存目录。")
    with tempfile.TemporaryDirectory(prefix=".gsm-download-", dir=destination) as temporary:
        folder = Path(temporary)
        package = folder / manifest["archive"]
        with package.open("xb") as stream:
            _download(client, manifest["download_url"], limit=manifest["size"], sink=stream)
        digest = hashlib.sha256()
        with package.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if package.stat().st_size != manifest["size"] or digest.hexdigest() != manifest["sha256"]:
            raise ReleaseError("下载包的 SHA256 或大小校验失败；暂存文件已移除。")
        (folder / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        # mkdir is exclusive; concurrent downloads must not overwrite one another.
        target.mkdir(mode=0o700)
        try:
            package.rename(target / package.name)
            (folder / "release-manifest.json").rename(target / "release-manifest.json")
        except OSError:
            # Keep an incomplete target for operator inspection, never remove prior data.
            raise ReleaseError("暂存写入失败；请检查目标目录和磁盘空间。") from None
    return target
