from __future__ import annotations

from typing import Protocol

import httpx


class WeChatChannelAdapter(Protocol):
    def send(self, *, route_key: str, text: str, idempotency_key: str) -> None:
        ...


class WebhookWeChatChannelAdapter:
    def __init__(
        self,
        *,
        url: str,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._url = url
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=10, trust_env=False)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def send(self, *, route_key: str, text: str, idempotency_key: str) -> None:
        response = self._client.post(
            self._url,
            headers={"Idempotency-Key": idempotency_key},
            json={"route_key": route_key, "text": text},
        )
        response.raise_for_status()
