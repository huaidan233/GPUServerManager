from __future__ import annotations

from typing import Any, BinaryIO
from urllib.parse import quote

import httpx


class WeChatCentralApiClient:
    def __init__(
        self,
        *,
        api_url: str,
        token: str,
        upload_timeout_seconds: float = 150,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=10, trust_env=False)
        self._headers = {"Authorization": f"Bearer {token}"}
        self._upload_timeout_seconds = upload_timeout_seconds

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def pair(self, code: str, route_key: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/wechat/pair",
            json={"code": code, "route_key": route_key},
        )

    def get_wechat_binding(self, route_key: str) -> dict[str, Any] | None:
        response = self._client.request(
            "GET",
            self._url(f"/api/wechat/routes/{quote(route_key, safe='')}"),
            headers=self._headers,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def get_active_route(self, user_id: str) -> dict[str, Any] | None:
        response = self._client.request(
            "GET",
            self._url(f"/api/wechat/users/{quote(user_id, safe='')}/active-route"),
            headers=self._headers,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def submit_route_action(
        self,
        route_key: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/wechat/routes/{quote(route_key, safe='')}/actions",
            json={"tool": tool, "arguments": arguments},
            timeout=50 if tool == "confirm_action" else 35 if tool in {"start_container", "watch_training", "list_training_jobs"} else 20,
        )

    def upload_print_job(
        self,
        route_key: str,
        source: BinaryIO,
        *,
        filename: str,
        media_type: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/wechat/routes/{quote(route_key, safe='')}/print-jobs",
            files={"file": (filename, source, media_type)},
            timeout=self._upload_timeout_seconds,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._client.request(
            method,
            self._url(path),
            headers=self._headers,
            **kwargs,
        )
        response.raise_for_status()
        return response.json()

    def _url(self, path: str) -> str:
        if self._api_url.endswith("/api") and path.startswith("/api/"):
            path = path.removeprefix("/api")
        return f"{self._api_url}{path}"
