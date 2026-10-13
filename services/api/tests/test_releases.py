import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gsm_api import main, releases

REPO = "example/gpu-server-manager"
VERSION = "0.3.0-rc.2"


@pytest.fixture(autouse=True)
def installed_version(monkeypatch):
    # Release discovery fixtures model a fixed upgrade, independent of this tree's version.
    monkeypatch.setattr(releases, "CURRENT_VERSION", "0.3.0-rc.1")


def fixture_release(version=VERSION, body=b"verified test source"):
    archive = f"gpu-server-manager-{version}-source.zip"
    base = f"https://github.com/{REPO}/releases/download/v{version}/"
    manifest = {"schema_version": 1, "product": "gpu-server-manager", "version": version,
                "archive": archive, "size": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                "install_mode": "docker-compose-source"}
    release = {"tag_name": "v" + version, "published_at": "2026-10-12T00:00:00Z", "draft": False,
               "prerelease": "-" in version, "assets": [
                   {"name": "release-manifest.json", "browser_download_url": base + "release-manifest.json"},
                   {"name": archive, "browser_download_url": base + archive, "size": len(body)}]}
    return release, manifest


def client_for(release, manifest, body=b"verified test source", *, endpoint="list"):
    def respond(request):
        if request.url.host == "api.github.com":
            if endpoint == "latest":
                assert request.url.path.endswith("/releases/latest")
                return httpx.Response(200, json=release)
            assert request.url.path.endswith("/releases")
            return httpx.Response(200, json=release if isinstance(release, list) else [release])
        if request.url.path.endswith("release-manifest.json"):
            return httpx.Response(200, json=manifest)
        return httpx.Response(200, content=body)
    return httpx.Client(transport=httpx.MockTransport(respond))


def test_prerelease_order_and_stable_channel():
    assert releases.version_key("0.3.0-alpha.9") < releases.version_key("0.3.0-beta.1") < releases.version_key("v0.3.0-rc.10") < releases.version_key("0.3.0")
    release, manifest = fixture_release("0.3.0")
    with client_for(release, manifest, endpoint="latest") as client:
        result = releases.check_release(REPO, client=client)
    assert result["status"] == "available"
    assert result["automatic_install"] is False
    assert result["release_url"].startswith(f"https://github.com/{REPO}/releases/tag/")


def test_prerelease_channel_chooses_highest_not_first_and_ignores_drafts():
    release, manifest = fixture_release()
    older, _ = fixture_release("0.3.0-rc.1")
    draft, _ = fixture_release("99.0.0")
    draft["draft"] = True
    with client_for([older, draft, release], manifest) as client:
        result = releases.check_release(REPO, "prerelease", client=client)
    assert result["latest_version"] == VERSION


@pytest.mark.parametrize("repo", ["../secrets", "https://github.com/example/repo", "a/b/c", "a/..", "user/repo.git"])
def test_invalid_repository_never_requests_network(repo):
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("unexpected request"))) as client:
        assert releases.check_release(repo, client=client)["status"] == "error"


def test_missing_configuration_never_requests_network():
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("unexpected request"))) as client:
        assert releases.check_release("", client=client)["status"] == "unconfigured"


@pytest.mark.parametrize("mode", ["missing", "wrong_repo", "wrong_version", "wrong_size", "bad_sha", "untrusted_redirect", "credentials_redirect"])
def test_malformed_or_untrusted_assets_fail_closed(mode):
    release, manifest = fixture_release()
    if mode == "missing": release["assets"] = []
    if mode == "wrong_repo": release["assets"][0]["browser_download_url"] = "https://github.com/attacker/other/releases/download/v0.3.0-rc.2/release-manifest.json"
    if mode == "wrong_version": manifest["version"] = "0.4.0"
    if mode == "wrong_size": manifest["size"] += 1
    if mode == "bad_sha": manifest["sha256"] = "invalid"
    if "redirect" in mode:
        requests = []
        def redirect(request):
            requests.append(request)
            return httpx.Response(302, headers={"location": "https://internal.example/secrets" if mode == "untrusted_redirect" else "https://secret:password@github.com/file"})
        with httpx.Client(transport=httpx.MockTransport(redirect)) as client:
            result = releases.check_release(REPO, "prerelease", client=client)
        assert len(requests) == 1
    else:
        with client_for(release, manifest) as client:
            result = releases.check_release(REPO, "prerelease", client=client)
    assert result["status"] == "error"
    assert "secret" not in json.dumps(result)
    assert "manifest" not in result


def test_network_failure_not_reported_as_current():
    def fail(_): raise httpx.ConnectError("private connection detail")
    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        result = releases.check_release(REPO, client=client)
    assert result["status"] == "error"
    assert "private connection" not in json.dumps(result)


def test_response_limit_applies_without_content_length():
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"too much"))) as client:
        with pytest.raises(releases.ReleaseError):
            releases._download(client, "https://api.github.com/test", limit=2)


def test_stage_verifies_before_exposing_and_refuses_to_replace(tmp_path):
    release, manifest = fixture_release()
    with client_for(release, manifest) as client:
        target = releases.stage_release(REPO, "prerelease", tmp_path, client=client)
        assert (target / manifest["archive"]).read_bytes() == b"verified test source"
        assert not list(tmp_path.glob(".gsm-download-*"))
        with pytest.raises(releases.ReleaseError, match="已存在"):
            releases.stage_release(REPO, "prerelease", tmp_path, client=client)
    assert len(list(target.iterdir())) == 2


def test_corrupt_download_is_removed_without_touching_other_data(tmp_path):
    release, manifest = fixture_release()
    existing = tmp_path / "keep.txt"
    existing.write_text("keep")
    with client_for(release, manifest, body=b"corrupted") as client:
        with pytest.raises(releases.ReleaseError, match="SHA256"):
            releases.stage_release(REPO, "prerelease", tmp_path, client=client)
    assert list(tmp_path.iterdir()) == [existing]
    assert existing.read_text() == "keep"


def test_only_administrators_can_check_release(monkeypatch):
    monkeypatch.setenv("GSM_ENV", "development")
    monkeypatch.delenv("GSM_UPDATE_REPOSITORY", raising=False)
    releases._cache.clear()
    client = TestClient(main.app)
    for token in [None, "user-alice-token", "power-user-token", "viewer-token"]:
        response = client.get("/api/admin/updates", headers={"Authorization": f"Bearer {token}"} if token else {})
        assert response.status_code in {401, 403}
    assert client.get("/api/admin/updates", headers={"Authorization": "Bearer admin-token"}).json()["status"] == "unconfigured"


def test_check_cache_bounds_repeated_browser_clicks(monkeypatch):
    releases._cache.clear()
    monkeypatch.setenv("GSM_UPDATE_REPOSITORY", REPO)
    calls = []
    def check(*args):
        calls.append(args)
        return {"status": "error"}
    monkeypatch.setattr(releases, "check_release", check)
    assert not releases.cached_release_check()["cached"]
    assert releases.cached_release_check()["cached"]
    assert len(calls) == 1
    releases._cache.clear()
