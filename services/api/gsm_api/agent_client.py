from __future__ import annotations

import hashlib
import os
from typing import Any
from uuid import uuid4
from urllib.parse import quote

import httpx


class ServerAgentError(RuntimeError):
    execution_outcome_unknown = True


class ServerAgentRejectedError(ServerAgentError):
    execution_outcome_unknown = False

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ContainerManagementError(ServerAgentError):
    def __init__(self, message: str, *, status_code: int, public_code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.public_code = public_code


class NoopServerAgentClient:
    def restart_container(self, *, server_id: str, agent_container_id: str, operation_id: str) -> dict[str, Any]:
        return {"agent_container_id": agent_container_id, "status": "running", "operation_receipt": _noop_operation_receipt(operation_id, action="restart_container", agent_container_id=agent_container_id)}

    def resource_metrics(self, *, server_id: str) -> dict[str, Any]:
        return {"host": {}, "containers": []}

    def container_management(self, *, server_id: str, agent_container_id: str,
                             operation: str, method: str = "GET", params=None, payload=None):
        if operation == "details":
            return {"runtime_available": False, "terminal_available": False}
        raise ContainerManagementError("terminal requires a real Docker runtime", status_code=503)

    def close(self) -> None:
        return None

    def list_gpus(self, *, server_id: str) -> list[dict[str, Any]]:
        return []

    def list_containers(self, *, server_id: str) -> list[dict[str, Any]]:
        return []

    def health_details(self, *, server_id: str) -> dict[str, Any]:
        return {"status": "ok", "checks": []}

    def container_evidence(
        self,
        *,
        server_id: str,
        agent_container_id: str,
    ) -> dict[str, Any]:
        return {
            "agent_container_id": agent_container_id,
            "status": "unknown",
            "exit_code": None,
            "oom_killed": False,
            "error": "",
            "restart_count": 0,
            "health": None,
        }

    def container_logs(
        self,
        *,
        server_id: str,
        agent_container_id: str,
        tail: int,
        max_bytes: int,
    ) -> dict[str, Any]:
        return {"agent_container_id": agent_container_id, "logs": "", "truncated": False}

    def create_container(self, *, server_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        mock_image_id = _noop_image_id("noop-managed-image")
        return {
            "agent_container_id": f"noop-{uuid4().hex[:12]}",
            "status": "created",
            "spec": payload,
            "ssh_authorized_keys_disabled": payload.get("ssh_port") is not None,
            "ssh_image_id": mock_image_id,
            "ssh_image_trusted": payload.get("ssh_port") is not None,
        }

    def start_container(self, *, server_id: str, agent_container_id: str) -> dict[str, Any]:
        return {"agent_container_id": agent_container_id, "status": "running"}

    def stop_container(
        self,
        *,
        server_id: str,
        agent_container_id: str,
        operation_id: str,
    ) -> dict[str, Any]:
        return {
            "agent_container_id": agent_container_id,
            "status": "stopped",
            "operation_receipt": _noop_operation_receipt(
                operation_id,
                action="stop_container",
                agent_container_id=agent_container_id,
            ),
        }

    def delete_container(
        self,
        *,
        server_id: str,
        agent_container_id: str,
        operation_id: str,
    ) -> dict[str, Any]:
        return {
            "agent_container_id": agent_container_id,
            "status": "deleted",
            "operation_receipt": _noop_operation_receipt(
                operation_id,
                action="delete_container",
                agent_container_id=agent_container_id,
            ),
        }

    def operation_receipt(
        self,
        *,
        server_id: str,
        operation_id: str,
    ) -> dict[str, Any]:
        del server_id
        raise ServerAgentRejectedError(
            "noop server agent has no durable operation receipt",
            status_code=404,
        )

    def update_container_ssh_access(
        self,
        *,
        server_id: str,
        agent_container_id: str,
        ssh_trusted_ca_public_key: str,
        ssh_owner_principal: str,
    ) -> dict[str, Any]:
        return {
            "agent_container_id": agent_container_id,
            "ssh_owner_principal": ssh_owner_principal,
            "ssh_authorized_keys_disabled": True,
            "ssh_image_id": _noop_image_id("noop-managed-image"),
            "ssh_image_trusted": True,
            "status": "updated",
        }

    def pull_image(self, *, server_id: str, image: str) -> dict[str, Any]:
        return {
            "image": image,
            "digest": f"noop-sha256:{image.replace('/', '-').replace(':', '-')}",
            "status": "pulled",
        }


class HttpServerAgentClient:
    def personal_data(self, *, username, operation, payload):
        return self._request("POST", "/agent/personal-data", expected_type=dict,
                             json={"username": username, "operation": operation, "payload": payload}, timeout=30)

    def restart_container(self, *, server_id: str, agent_container_id: str, operation_id: str) -> dict[str, Any]:
        result = self._request("POST", f"/agent/containers/{agent_container_id}/restart", expected_type=dict,
                               headers=self._operation_headers(operation_id), timeout=40)
        return _validate_mutation_receipt(result, operation_id=operation_id, action="restart_container",
                                          agent_container_id=agent_container_id, expected_status="running")

    def resource_metrics(self, *, server_id: str) -> dict[str, Any]:
        return self._request("GET", "/agent/resources", expected_type=dict)

    def container_management(self, *, server_id: str, agent_container_id: str,
                             operation: str, method: str = "GET", params=None, payload=None):
        try:
            response = self._client.request(
                method, f"{self._base_url}/agent/containers/{quote(agent_container_id, safe='')}/{operation}",
                headers=self._headers, params=params, json=payload,
                timeout=180 if operation == "configuration" else 300 if operation.startswith("applications/") else 35 if operation.startswith("python-environments") else 20,
            )
            response.raise_for_status()
            result = response.json()
        except httpx.HTTPStatusError as exc:
            public_code = None
            if operation.startswith("applications/"):
                try:
                    detail = exc.response.json().get("detail")
                    if isinstance(detail, str) and detail.startswith("app_") and len(detail) < 64:
                        public_code = detail
                except (ValueError, AttributeError):
                    pass
            raise ContainerManagementError("server agent rejected container management request", status_code=exc.response.status_code, public_code=public_code) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ServerAgentError("server agent container management request failed") from exc
        if not isinstance(result, dict):
            raise ServerAgentError("server agent returned invalid container management data")
        return result

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client = http_client or httpx.Client(timeout=10, trust_env=False)
        self._owns_client = http_client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def list_gpus(self, *, server_id: str) -> list[dict[str, Any]]:
        return self._request("GET", "/agent/gpus", expected_type=list)

    def list_containers(self, *, server_id: str) -> list[dict[str, Any]]:
        return self._request("GET", "/agent/containers", expected_type=list)

    def health_details(self, *, server_id: str) -> dict[str, Any]:
        return self._request("GET", "/agent/health/details", expected_type=dict)

    def container_evidence(
        self,
        *,
        server_id: str,
        agent_container_id: str,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/agent/containers/{agent_container_id}/evidence",
            expected_type=dict,
        )

    def container_logs(
        self,
        *,
        server_id: str,
        agent_container_id: str,
        tail: int,
        max_bytes: int,
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/agent/containers/{agent_container_id}/logs",
            expected_type=dict,
            params={"tail": tail, "max_bytes": max_bytes},
        )

    def create_container(self, *, server_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST", "/agent/containers", expected_type=dict, json=payload
        )

    def start_container(self, *, server_id: str, agent_container_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/agent/containers/{agent_container_id}/start",
            expected_type=dict,
        )

    def stop_container(
        self,
        *,
        server_id: str,
        agent_container_id: str,
        operation_id: str,
    ) -> dict[str, Any]:
        del server_id
        result = self._request(
            "POST",
            f"/agent/containers/{agent_container_id}/stop",
            expected_type=dict,
            headers=self._operation_headers(operation_id),
            # Docker may use its whole 10-second graceful-stop window. Allow
            # time for the forced stop and durable receipt to arrive afterwards.
            timeout=30,
        )
        return _validate_mutation_receipt(
            result,
            operation_id=operation_id,
            action="stop_container",
            agent_container_id=agent_container_id,
            expected_status="stopped",
        )

    def delete_container(
        self,
        *,
        server_id: str,
        agent_container_id: str,
        operation_id: str,
    ) -> dict[str, Any]:
        del server_id
        result = self._request(
            "DELETE",
            f"/agent/containers/{agent_container_id}",
            expected_type=dict,
            headers=self._operation_headers(operation_id),
        )
        return _validate_mutation_receipt(
            result,
            operation_id=operation_id,
            action="delete_container",
            agent_container_id=agent_container_id,
            expected_status="deleted",
        )

    def operation_receipt(
        self,
        *,
        server_id: str,
        operation_id: str,
    ) -> dict[str, Any]:
        del server_id
        _validate_operation_id(operation_id)
        result = self._request(
            "GET",
            f"/agent/operation-receipts/{operation_id}",
            expected_type=dict,
        )
        return _validate_queried_receipt(result, operation_id=operation_id)

    def update_container_ssh_access(
        self,
        *,
        server_id: str,
        agent_container_id: str,
        ssh_trusted_ca_public_key: str,
        ssh_owner_principal: str,
    ) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/agent/containers/{agent_container_id}/ssh-access",
            expected_type=dict,
            json={
                "ssh_trusted_ca_public_key": ssh_trusted_ca_public_key,
                "ssh_owner_principal": ssh_owner_principal,
            },
        )

    def pull_image(self, *, server_id: str, image: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/agent/images/pull",
            expected_type=dict,
            json={"image": image},
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected_type: type[dict] | type[list],
        **kwargs: Any,
    ) -> Any:
        try:
            headers = kwargs.pop("headers", self._headers)
            response = self._client.request(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                **kwargs,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            if 400 <= exc.response.status_code < 500:
                raise ServerAgentRejectedError(
                    f"server agent rejected the request with status {exc.response.status_code}",
                    status_code=exc.response.status_code,
                ) from exc
            raise ServerAgentError(
                f"server agent returned status {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ServerAgentError("server agent request failed") from exc
        except ValueError as exc:
            raise ServerAgentError("server agent returned invalid JSON") from exc
        if not isinstance(payload, expected_type):
            raise ServerAgentError(
                f"server agent returned an invalid {expected_type.__name__} response"
            )
        return payload

    def _operation_headers(self, operation_id: str) -> dict[str, str]:
        _validate_operation_id(operation_id)
        return {**self._headers, "X-GSM-Operation-ID": operation_id}


def default_server_agent_client() -> NoopServerAgentClient | HttpServerAgentClient:
    base_url = os.environ.get("GSM_SERVER_AGENT_URL")
    if not base_url and os.environ.get("GSM_ENV") == "production":
        raise RuntimeError("GSM_SERVER_AGENT_URL is required in production")
    if base_url:
        token = os.environ.get("GSM_AGENT_SHARED_TOKEN")
        if not token:
            if os.environ.get("GSM_ENV") == "production":
                raise RuntimeError(
                    "GSM_AGENT_SHARED_TOKEN is required when GSM_SERVER_AGENT_URL is configured"
                )
            token = "change-me-agent-token"
        return HttpServerAgentClient(
            base_url=base_url,
            token=token,
        )
    return NoopServerAgentClient()


def _noop_image_id(value: str) -> str:
    if os.environ.get("GSM_ENV") == "production":
        suffix = value.replace("/", "-").replace(":", "-")
        return f"mock-sha256:{suffix}"
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _validate_operation_id(value: str) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for character in value)
    ):
        raise ValueError("server agent operation id is invalid")


def _noop_operation_receipt(
    operation_id: str,
    *,
    action: str,
    agent_container_id: str,
) -> dict[str, Any]:
    _validate_operation_id(operation_id)
    return {
        "operation_id": operation_id,
        "action": action,
        "target": {"type": "container", "id": agent_container_id},
        "target_id": agent_container_id,
        "state": "succeeded",
        "outcome": "executed",
        "replayed": False,
    }


def _validate_mutation_receipt(
    result: dict[str, Any],
    *,
    operation_id: str,
    action: str,
    agent_container_id: str,
    expected_status: str,
) -> dict[str, Any]:
    receipt = result.get("operation_receipt")
    target = receipt.get("target") if isinstance(receipt, dict) else None
    if (
        result.get("agent_container_id") != agent_container_id
        or result.get("status") != expected_status
        or not isinstance(receipt, dict)
        or receipt.get("operation_id") != operation_id
        or receipt.get("action") != action
        or receipt.get("target_id") != agent_container_id
        or target != {"type": "container", "id": agent_container_id}
        or receipt.get("state") != "succeeded"
        or receipt.get("outcome") != "executed"
        or not isinstance(receipt.get("replayed"), bool)
    ):
        raise ServerAgentError("server agent returned an invalid operation receipt")
    return result


def _validate_queried_receipt(
    result: dict[str, Any],
    *,
    operation_id: str,
) -> dict[str, Any]:
    receipt = result.get("operation_receipt")
    target = receipt.get("target") if isinstance(receipt, dict) else None
    state = receipt.get("state") if isinstance(receipt, dict) else None
    outcome = receipt.get("outcome") if isinstance(receipt, dict) else None
    target_id = receipt.get("target_id") if isinstance(receipt, dict) else None
    expected_outcomes = {
        "in_progress": "inconclusive",
        "succeeded": "executed",
        "failed": "not_executed",
    }
    if (
        not isinstance(receipt, dict)
        or receipt.get("operation_id") != operation_id
        or receipt.get("action") not in {"stop_container", "delete_container", "restart_container"}
        or not isinstance(target_id, str)
        or not target_id
        or target != {"type": "container", "id": target_id}
        or expected_outcomes.get(state) != outcome
        or not isinstance(receipt.get("replayed"), bool)
    ):
        raise ServerAgentError("server agent returned an invalid operation receipt")
    return result
