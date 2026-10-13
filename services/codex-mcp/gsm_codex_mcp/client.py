from __future__ import annotations

from typing import Any

import httpx


class CodexCentralApiClient:
    def __init__(
        self,
        *,
        api_url: str,
        token: str,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._api_url = _normalize_api_url(api_url)
        self._client = http_client or httpx.Client(timeout=10, trust_env=False)
        self._headers = {"Authorization": f"Bearer {token}"}

    def list_gpus(self, server_id: str | None = None) -> dict[str, Any]:
        if server_id is None:
            return self._request("GET", "/servers")
        return self._request("GET", f"/servers/{server_id}/gpus")

    def list_containers(self) -> list[dict[str, Any]]:
        return self._request("GET", "/containers")

    def get_container(self, container_id: str) -> dict[str, Any]:
        return self._request("GET", f"/containers/{container_id}")

    def create_container(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/containers", json=payload)

    def stop_container(
        self,
        container_id: str,
        confirmation_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if confirmation_id is not None:
            payload["confirmation_id"] = confirmation_id
        return self._request(
            "POST",
            f"/containers/{container_id}/stop",
            json=payload or None,
        )

    def update_container_gpus(
        self,
        container_id: str,
        gpu_devices: list[str],
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/containers/{container_id}/gpus",
            json={"gpu_devices": gpu_devices},
        )

    def confirm(self, confirmation_id: str, phrase: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/confirmations/{confirmation_id}/confirm",
            json={"phrase": phrase},
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._client.request(
            method,
            f"{self._api_url}{path}",
            headers=self._headers,
            **kwargs,
        )
        response.raise_for_status()
        return response.json()


def _normalize_api_url(api_url: str) -> str:
    normalized = api_url.rstrip("/")
    if normalized.endswith("/api"):
        return normalized
    return f"{normalized}/api"
