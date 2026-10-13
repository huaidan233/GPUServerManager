from __future__ import annotations

from typing import Any
import json

import httpx

from gsm_wechat_agent.conversation import ALLOWED_TOOLS


EMPTY_PLAN = {"kind": "action_plan", "actions": []}
ARGUMENT_KEYS = {
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
        "image",
        "preset",
        "cpu_limit",
        "memory_limit_gb",
        "gpu_devices",
        "mount_profile_ids",
        "server_id",
    },
    "stop_container": {"container"},
    "confirm_action": {"phrase"},
    "cancel_pending": set(),
    "recommend_gpus": {"gpu_count", "memory_tier", "exact_memory_gb", "estimated_duration_minutes"},
    "join_gpu_queue": {
        "gpu_count", "memory_tier", "exact_memory_gb", "estimated_duration_minutes",
        "compatible_server_ids", "workload_profile",
    },
    "list_gpu_queue": set(),
    "respond_idle": {"event_id", "response", "estimated_minutes"},
    "decide_approval": {"approval_id", "decision", "reason"},
    "execute_approval": {"approval_id"},
    "list_approvals": set(),
    "server_health": {"server_id"},
    "diagnose_container": {"container"},
    "update_print_job": {
        "job_id", "printer_id", "copies", "page_ranges", "sides",
        "color_mode", "paper",
    },
    "confirm_print": {"job_id", "phrase"},
    "cancel_print": {"job_id"},
    "list_print_jobs": set(),
}


class CentralGatewayActionParser:
    def __init__(
        self,
        *,
        api_url: str,
        service_token: str,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not service_token:
            raise ValueError("WeChat service token is required")
        self._url = f"{api_url.rstrip('/')}/api/internal/agent-llm/parse"
        self._headers = {
            "Authorization": f"Bearer {service_token}",
        }
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=35,
            follow_redirects=False,
            trust_env=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def parse(self, message: str) -> dict[str, Any]:
        try:
            response = self._client.post(
                self._url,
                headers=self._headers,
                json={"message": message},
            )
            response.raise_for_status()
            plan = response.json()
        except Exception:
            return {"kind": "action_plan", "actions": [{"tool": "reply", "arguments": {"text": "智能问答服务暂时连接不上。你仍可使用“帮助”“我的容器”“查看 test 的日志”等直接指令，稍后再试自然语言问答。"}}]}

        return plan if _is_safe_plan(plan) else dict(EMPTY_PLAN)

    def parse_with_context(self, message: str, history: list[dict[str, str]]) -> dict[str, Any]:
        if not history:
            return self.parse(message)
        envelope = json.dumps({"recent_messages": history[-6:], "current_message": message}, ensure_ascii=False)
        return self.parse(envelope if len(envelope) <= 8192 else message)


def _is_safe_plan(plan: Any) -> bool:
    if (
        not isinstance(plan, dict)
        or set(plan) != {"kind", "actions"}
        or plan.get("kind") != "action_plan"
    ):
        return False
    actions = plan.get("actions")
    if not isinstance(actions, list):
        return False
    if len(actions) > 1:
        return False
    for action in actions:
        if not isinstance(action, dict):
            return False
        tool = action.get("tool")
        if tool not in ALLOWED_TOOLS:
            return False
        if tool != "reply" and any(term in repr(action).lower() for term in ("shell", "docker ", "powershell", "bash")):
            return False
        if set(action) - {"tool", "arguments", "requires_confirmation"}:
            return False
        arguments = action.get("arguments", {})
        if not isinstance(arguments, dict):
            return False
        if set(arguments) - ARGUMENT_KEYS[tool]:
            return False
        if "requires_confirmation" in action and not isinstance(
            action["requires_confirmation"], bool
        ):
            return False
    return True
