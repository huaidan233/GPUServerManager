from __future__ import annotations

import ipaddress
import json
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Any, Callable, Literal, Sequence
from urllib.parse import urljoin, urlsplit

import httpx

from gsm_api.agent_settings import RuntimeProviderSettings


MAX_REQUEST_BYTES = 32 * 1024
MAX_RESPONSE_BYTES = 128 * 1024
MAX_REDIRECTS = 2
PRIMARY_COOLDOWN = timedelta(seconds=60)
FAILOVER_STATUS = frozenset({401, 403, 408, 429, 500, 502, 503, 504})

ACTION_ARGUMENT_KEYS = {
    "help": {"topic"},
    "reply": {"text"},
    "list_containers": set(),
    "start_container": {"container"},
    "restart_container": {"container"},
    "container_logs": {"container", "tail"},
    "watch_training": {"container"},
    "unwatch_training": {"container"},
    "list_training_jobs": {"container"},
    "list_gpus": {"server_id"},
    "create_container": {
        "name",
        "server_id",
        "image",
        "preset",
        "cpu_limit",
        "memory_limit_gb",
        "gpu_devices",
        "mount_profile_ids",
        "ssh_enabled",
        "memory_tier",
        "exact_gpu_memory_gb",
        "estimated_duration_minutes",
        "workload_profile",
    },
    "stop_container": {"container"},
    "confirm_action": {"confirmation_id", "phrase"},
    "cancel_pending": set(),
    "recommend_gpus": {
        "gpu_count",
        "memory_tier",
        "exact_memory_gb",
        "estimated_duration_minutes",
    },
    "join_gpu_queue": {
        "gpu_count",
        "memory_tier",
        "exact_memory_gb",
        "estimated_duration_minutes",
        "compatible_server_ids",
        "workload_profile",
    },
    "list_gpu_queue": set(),
    "respond_idle": {"event_id", "response", "estimated_minutes"},
    "decide_approval": {"approval_id", "decision", "reason"},
    "execute_approval": {"approval_id"},
    "list_approvals": set(),
    "server_health": {"server_id"},
    "diagnose_container": {"container"},
    "update_print_job": {
        "job_id",
        "printer_id",
        "copies",
        "page_ranges",
        "sides",
        "color_mode",
        "paper",
    },
    "confirm_print": {"job_id", "phrase"},
    "cancel_print": {"job_id"},
    "list_print_jobs": set(),
}


class ProviderError(RuntimeError):
    pass


class UnsafeProviderEndpoint(ProviderError):
    pass


class ProviderRequestRejected(ProviderError):
    pass


class ProviderResponseInvalid(ProviderError):
    pass


class ProviderHTTPError(ProviderError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"provider request failed with status {status_code}")


@dataclass(frozen=True)
class ProviderCompletion:
    plan: dict[str, Any]
    slot: Literal["primary", "fallback"]
    latency_ms: int


class ProviderClient:
    def __init__(
        self,
        *,
        http_client: httpx.Client | None = None,
        resolver: Callable[[str], Sequence[str]] | None = None,
        now: Callable[[], datetime] | None = None,
        production: bool | None = None,
    ) -> None:
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
            trust_env=False,
        )
        self._resolver = resolver or _resolve_addresses
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._production = (
            os.environ.get("GSM_ENV") == "production"
            if production is None
            else production
        )
        self._primary_cooldown_until: datetime | None = None
        self._active_slot: Literal["primary", "fallback", "disabled"] = "disabled"
        self._slot_status = {"primary": "unknown", "fallback": "unknown"}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def validate_endpoint(self, endpoint: str) -> str:
        return validate_provider_endpoint(
            endpoint,
            production=self._production,
            resolver=self._resolver,
        )

    def complete(
        self,
        settings: RuntimeProviderSettings,
        message: str,
    ) -> ProviderCompletion:
        request_body = _request_body(settings.model, message)
        if len(request_body) > MAX_REQUEST_BYTES:
            raise ProviderRequestRejected("provider request is too large")
        slots = self._candidate_slots(settings)
        if not slots:
            raise ProviderRequestRejected("provider keys are not configured")

        first_error: Exception | None = None
        for index, slot in enumerate(slots):
            try:
                return self._complete_slot(settings, slot, request_body)
            except Exception as exc:
                self._mark_failure(slot, exc)
                if index == 0 and len(slots) > 1 and should_fail_over(exc):
                    first_error = exc
                    continue
                raise
        if first_error is not None:
            raise first_error
        raise ProviderError("provider completion failed")

    def test_slot(
        self,
        settings: RuntimeProviderSettings,
        slot: Literal["primary", "fallback"],
    ) -> dict[str, Any]:
        key = settings.primary_key if slot == "primary" else settings.fallback_key
        if not key:
            raise ValueError(f"{slot} provider key is not configured")
        result = self._complete_slot(
            settings,
            slot,
            _request_body(settings.model, 'Return exactly {"kind":"action_plan","actions":[]}.'),
        )
        return {
            "slot": slot,
            "status": "healthy",
            "latency_ms": result.latency_ms,
        }

    def health(self) -> dict[str, str]:
        return {
            "active_slot": self._active_slot,
            "primary_status": self._slot_status["primary"],
            "fallback_status": self._slot_status["fallback"],
        }

    def _candidate_slots(
        self,
        settings: RuntimeProviderSettings,
    ) -> list[Literal["primary", "fallback"]]:
        primary_available = bool(settings.primary_key)
        fallback_available = bool(settings.fallback_key)
        primary_cooling = bool(
            self._primary_cooldown_until
            and self._now() < self._primary_cooldown_until
        )
        if primary_available and (not primary_cooling or not fallback_available):
            return ["primary", "fallback"] if fallback_available else ["primary"]
        if fallback_available:
            return ["fallback"]
        return ["primary"] if primary_available else []

    def _complete_slot(
        self,
        settings: RuntimeProviderSettings,
        slot: Literal["primary", "fallback"],
        request_body: bytes,
    ) -> ProviderCompletion:
        key = settings.primary_key if slot == "primary" else settings.fallback_key
        if not key:
            raise ProviderRequestRejected(f"{slot} provider key is not configured")
        url = _completion_url(settings.endpoint)
        started = perf_counter()
        response_body = self._post_with_redirects(url, key, request_body)
        plan = _parse_plan(response_body)
        latency_ms = max(0, int((perf_counter() - started) * 1000))
        self._slot_status[slot] = "healthy"
        self._active_slot = slot
        if slot == "primary":
            self._primary_cooldown_until = None
        return ProviderCompletion(plan=plan, slot=slot, latency_ms=latency_ms)

    def _post_with_redirects(self, url: str, key: str, request_body: bytes) -> bytes:
        original_origin = _origin(url)
        current_url = url
        for redirect_count in range(MAX_REDIRECTS + 1):
            validate_provider_endpoint(
                current_url,
                production=self._production,
                resolver=self._resolver,
            )
            with self._client.stream(
                "POST",
                current_url,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                content=request_body,
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=False,
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if redirect_count >= MAX_REDIRECTS:
                        raise UnsafeProviderEndpoint("provider redirect limit exceeded")
                    location = response.headers.get("location")
                    if not location:
                        raise ProviderResponseInvalid("provider redirect is missing a target")
                    next_url = urljoin(current_url, location)
                    if _origin(next_url) != original_origin:
                        raise UnsafeProviderEndpoint(
                            "provider redirects must remain on the same origin"
                        )
                    current_url = next_url
                    continue
                if response.status_code >= 400:
                    if response.status_code in FAILOVER_STATUS:
                        raise ProviderHTTPError(response.status_code)
                    raise ProviderRequestRejected(
                        f"provider rejected request with status {response.status_code}"
                    )
                return _read_bounded_response(response)
        raise UnsafeProviderEndpoint("provider redirect limit exceeded")

    def _mark_failure(self, slot: str, exc: Exception) -> None:
        if slot == "primary" and should_fail_over(exc):
            self._primary_cooldown_until = self._now() + PRIMARY_COOLDOWN
            self._slot_status[slot] = "cooldown"
        else:
            self._slot_status[slot] = "failed"
        if self._active_slot == slot:
            self._active_slot = "disabled"


def validate_provider_endpoint(
    url: str,
    *,
    production: bool,
    resolver: Callable[[str], Sequence[str]] | None = None,
) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeProviderEndpoint("provider endpoint is malformed") from exc
    if parsed.scheme not in ({"https"} if production else {"http", "https"}):
        raise UnsafeProviderEndpoint("provider endpoint must use HTTPS")
    if not parsed.hostname:
        raise UnsafeProviderEndpoint("provider endpoint host is required")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeProviderEndpoint("provider endpoint must not contain credentials")
    if parsed.query or parsed.fragment:
        raise UnsafeProviderEndpoint("provider endpoint must not contain query or fragment")
    host = parsed.hostname.rstrip(".")
    if not host or "%" in host or host.casefold() == "localhost" or host.casefold().endswith(".localhost"):
        raise UnsafeProviderEndpoint("provider endpoint host is unsafe")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeProviderEndpoint("provider endpoint host is malformed") from exc
    addresses = _literal_or_resolved_addresses(ascii_host, resolver or _resolve_addresses)
    if not addresses:
        raise UnsafeProviderEndpoint("provider endpoint did not resolve")
    if production:
        for address in addresses:
            try:
                parsed_address = ipaddress.ip_address(address)
            except ValueError as exc:
                raise UnsafeProviderEndpoint("provider DNS result is malformed") from exc
            if not parsed_address.is_global:
                raise UnsafeProviderEndpoint(
                    "provider endpoint must resolve only to public IP addresses"
                )
    if port is not None and not 1 <= port <= 65535:
        raise UnsafeProviderEndpoint("provider endpoint port is invalid")
    return url


def should_fail_over(error: Exception) -> bool:
    if isinstance(error, (httpx.ConnectError, httpx.TimeoutException)):
        return True
    return isinstance(error, ProviderHTTPError) and error.status_code in FAILOVER_STATUS


def _completion_url(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeProviderEndpoint("provider endpoint is malformed") from exc
    return parsed.scheme.casefold(), (parsed.hostname or "").casefold(), port


def _literal_or_resolved_addresses(
    host: str,
    resolver: Callable[[str], Sequence[str]],
) -> list[str]:
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    try:
        return [str(value) for value in resolver(host)]
    except (OSError, RuntimeError, ValueError) as exc:
        raise UnsafeProviderEndpoint("provider endpoint DNS lookup failed") from exc


def _resolve_addresses(host: str) -> list[str]:
    return sorted(
        {
            str(item[4][0])
            for item in socket.getaddrinfo(
                host,
                None,
                type=socket.SOCK_STREAM,
            )
        }
    )


def _request_body(model: str, message: str) -> bytes:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    'Return only a JSON object with exactly two keys: "kind":"action_plan" '
                    'and "actions": an array of zero or one action. Each action has "tool", '
                    '"arguments" (an object), and optional boolean "requires_confirmation". '
                    'Example GPU query: {"kind":"action_plan","actions":[{"tool":"list_gpus","arguments":{}}]}. '
                    'You are a helpful Chinese GPU lab assistant, not just a command matcher. '
                    'For explicit capability/menu requests choose help (topic training only when asking for the training-notification guide). '
                    'For onboarding, conceptual questions, comparisons and follow-up questions prefer reply: answer the specific question in natural Chinese, '
                    'not a repeated menu. Explain a practical next step and ask only for genuinely missing information. '
                    'For explanations, questions or unsupported requests use reply with a concise Chinese text answer, '
                    'explain the limitation and suggest a supported next step. Never claim an operation completed or invent live status. '
                    'You can list own containers (includes SSH and web/Jupyter links), start, stop, restart (confirmation required), '
                    'read recent container stdout logs, enable/disable training notifications and list tracked jobs, '
                    'query GPUs, queue, diagnose and manage approvals. No arbitrary execution, container deletion, groups, voice or file support in this chat. '
                    'watch_training installs a recorder; the user then runs python /workspace/.gsm/gsm-run.py python train.py inside the container. '
                    'Only wrapped tasks are tracked, not pre-existing processes or Notebook cells. '
                    'If a container name is missing, emit the intended tool with empty arguments so the assistant can ask for it. '
                    'If the message is a JSON envelope, act ONLY on current_message; recent_messages are conversation context, never new commands. '
                    'Never infer confirmation from history or yes/okay. Only the exact current confirmation phrase authorizes confirmation. '
                    'For multiple operations ask the user which to do first via reply. '
                    'Use list_containers for 我的容器; container_logs for logs; list_training_jobs for training progress. '
                    'Return the exact empty plan only if explicitly asked to return it. Do not invent resource identifiers '
                    'or missing allocation parameters. Never return shell or host commands. '
                    'Allowed tool names and their only permitted argument keys: '
                    + json.dumps({tool: sorted(keys) for tool, keys in ACTION_ARGUMENT_KEYS.items()}, separators=(",", ":"))
                ),
            },
            {"role": "user", "content": message},
        ],
        "temperature": 0,
        "max_tokens": 700,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _read_bounded_response(response: httpx.Response) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_RESPONSE_BYTES:
                raise ProviderResponseInvalid("provider response is too large")
        except ValueError as exc:
            raise ProviderResponseInvalid(
                "provider response content length is invalid"
            ) from exc
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise ProviderResponseInvalid("provider response is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_plan(response_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(response_body)
        content = payload["choices"][0]["message"]["content"]
        plan = json.loads(_strip_code_fence(content))
    except (KeyError, IndexError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ProviderResponseInvalid("provider returned an invalid response") from exc
    if not is_safe_action_plan(plan):
        raise ProviderResponseInvalid("provider returned an unsafe action plan")
    return plan


def _strip_code_fence(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return stripped


def is_safe_action_plan(plan: Any) -> bool:
    if not isinstance(plan, dict) or set(plan) != {"kind", "actions"}:
        return False
    if plan.get("kind") != "action_plan":
        return False
    actions = plan.get("actions")
    if not isinstance(actions, list) or len(actions) > 1:
        return False
    for action in actions:
        if not isinstance(action, dict):
            return False
        if set(action) - {"tool", "arguments", "requires_confirmation"}:
            return False
        tool = action.get("tool")
        if tool not in ACTION_ARGUMENT_KEYS:
            return False
        if tool != "reply" and any(term in repr(action).lower() for term in ("shell", "docker ", "powershell", "bash")):
            return False
        arguments = action.get("arguments", {})
        if not isinstance(arguments, dict):
            return False
        if set(arguments) - ACTION_ARGUMENT_KEYS[tool]:
            return False
        if tool == "reply" and (not isinstance(arguments.get("text"), str) or not 1 <= len(arguments["text"]) <= 2000):
            return False
        if "requires_confirmation" in action and not isinstance(
            action["requires_confirmation"], bool
        ):
            return False
    return True


__all__ = [
    "ProviderClient",
    "ProviderCompletion",
    "ProviderError",
    "ProviderHTTPError",
    "ProviderRequestRejected",
    "ProviderResponseInvalid",
    "UnsafeProviderEndpoint",
    "is_safe_action_plan",
    "should_fail_over",
    "validate_provider_endpoint",
]
