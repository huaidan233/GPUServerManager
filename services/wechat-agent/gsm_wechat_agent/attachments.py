from __future__ import annotations

from typing import BinaryIO, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator


class InboundAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_id: str = Field(min_length=1, max_length=512)
    filename: str = Field(min_length=1, max_length=255)
    media_type: Literal["application/pdf", "image/jpeg", "image/png"]

    @field_validator("media_id")
    @classmethod
    def media_id_is_not_a_url(cls, value: str) -> str:
        if "://" in value or "\0" in value:
            raise ValueError("attachment media id must not be a URL")
        return value


class HTTPAttachmentFetcher:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        http_client: httpx.Client | None = None,
    ) -> None:
        parsed = httpx.URL(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise ValueError("attachment provider URL is invalid")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("attachment provider URL must not contain credentials or query")
        if not token:
            raise ValueError("attachment provider token is required")
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=30,
            trust_env=False,
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch_to(
        self,
        media_id: str,
        destination: BinaryIO,
        *,
        max_bytes: int,
    ) -> int:
        validated = InboundAttachment(
            media_id=media_id,
            filename="attachment.pdf",
            media_type="application/pdf",
        ).media_id
        if max_bytes < 1:
            raise ValueError("attachment size limit must be positive")
        url = self._base_url + "/" + quote(validated, safe="")
        total = 0
        with self._client.stream(
            "GET",
            url,
            headers=self._headers,
            follow_redirects=False,
        ) as response:
            if response.status_code != 200:
                raise RuntimeError(
                    f"attachment provider returned status {response.status_code}"
                )
            for chunk in response.iter_bytes(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("attachment exceeds size limit")
                destination.write(chunk)
        return total


__all__ = ["HTTPAttachmentFetcher", "InboundAttachment"]
