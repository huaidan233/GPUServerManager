from __future__ import annotations

import hashlib
import os
import ssl
from pathlib import Path
from typing import Any

import httpx


class PrintAgentProtocolError(RuntimeError):
    pass


class PrintAgentClient:
    def __init__(
        self,
        base_url: str,
        agent_token: str,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Central API URL must use HTTP or HTTPS")
        if not agent_token:
            raise ValueError("print agent token is required")
        self._base_url = base_url.rstrip("/")
        self._token = agent_token
        self._client = http_client or httpx.Client(
            timeout=30,
            verify=ssl.create_default_context(cafile=os.getenv("GSM_PRINT_CA_BUNDLE") or None),
            trust_env=False,
            follow_redirects=False,
        )

    def heartbeat(self, *, version: str, printers: list[dict]) -> dict:
        return self._post_json(
            "/api/print-agent/heartbeat",
            {"version": version, "printers": printers},
        )

    def claim(self) -> dict | None:
        response = self._client.post(
            self._url("/api/print-agent/jobs/claim"),
            headers=self._headers(),
            json={},
        )
        if response.status_code == 204:
            return None
        return self._json(response)

    def renew(self, job_id: str, lease_id: str) -> dict:
        return self._post_json(
            f"/api/print-agent/jobs/{job_id}/renew",
            {"lease_id": lease_id},
        )

    def dispatch_state(self, job_id: str, lease_id: str) -> dict:
        response = self._client.get(
            self._url(f"/api/print-agent/jobs/{job_id}/dispatch-state"),
            headers=self._headers(),
            params={"lease_id": lease_id},
        )
        return self._json(response)

    def download_artifact(
        self,
        job_id: str,
        lease_id: str,
        token: str,
        destination: Path,
    ) -> dict[str, Any]:
        destination = Path(destination)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            destination.parent.chmod(0o700)
        except OSError:
            pass
        digest = hashlib.sha256()
        size = 0
        try:
            with self._client.stream(
                "GET",
                self._url(f"/api/print-agent/jobs/{job_id}/artifact"),
                headers=self._headers(),
                params={"lease_id": lease_id, "token": token},
                follow_redirects=False,
            ) as response:
                self._raise_for_status(response)
                expected_digest = response.headers.get("X-Content-SHA256", "")
                expected_size = _parse_content_length(
                    response.headers.get("Content-Length")
                )
                with destination.open("xb") as output:
                    try:
                        destination.chmod(0o600)
                    except OSError:
                        pass
                    for chunk in response.iter_bytes(64 * 1024):
                        size += len(chunk)
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return {
            "size_bytes": size,
            "sha256": digest.hexdigest(),
            "expected_size_bytes": expected_size,
            "expected_sha256": expected_digest,
        }

    def event(
        self,
        job_id: str,
        lease_id: str,
        *,
        sequence: int,
        event_type: str,
        **details: Any,
    ) -> dict:
        return self._post_json(
            f"/api/print-agent/jobs/{job_id}/events",
            {
                "lease_id": lease_id,
                "sequence": sequence,
                "type": event_type,
                **details,
            },
        )

    def _post_json(self, path: str, payload: dict) -> dict:
        response = self._client.post(
            self._url(path),
            headers=self._headers(),
            json=payload,
        )
        return self._json(response)

    def _json(self, response: httpx.Response) -> dict:
        self._raise_for_status(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise PrintAgentProtocolError(
                "Central API returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise PrintAgentProtocolError(
                "Central API returned an invalid response shape"
            )
        return payload

    def _raise_for_status(self, response: httpx.Response) -> None:
        if not 200 <= response.status_code < 300:
            raise PrintAgentProtocolError(
                f"Central API request failed with status {response.status_code}"
            )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _url(self, path: str) -> str:
        return self._base_url + path


def _parse_content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise PrintAgentProtocolError(
            "Central API returned an invalid Content-Length"
        ) from exc
    if parsed < 0:
        raise PrintAgentProtocolError(
            "Central API returned an invalid Content-Length"
        )
    return parsed


__all__ = ["PrintAgentClient", "PrintAgentProtocolError"]
