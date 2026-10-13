from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Protocol

from gsm_wechat_agent.planner import plan_message
from gsm_wechat_agent.help import help_text


ALLOWED_TOOLS = {
    'help',
    'reply',
    'list_containers',
    'start_container',
    'restart_container',
    'container_logs',
    'watch_training',
    'unwatch_training',
    'list_training_jobs',

    "list_gpus",
    "create_container",
    "stop_container",
    "confirm_action",
    "cancel_pending",
    "recommend_gpus",
    "join_gpu_queue",
    "list_gpu_queue",
    "respond_idle",
    "decide_approval",
    "execute_approval",
    "list_approvals",
    "server_health",
    "diagnose_container",
    "update_print_job",
    "confirm_print",
    "cancel_print",
    "list_print_jobs",
}

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
        "name", "server_id", "image", "preset", "cpu_limit", "memory_limit_gb",
        "gpu_devices", "mount_profile_ids", "ssh_enabled", "ssh_public_key",
        "memory_tier", "exact_gpu_memory_gb", "estimated_duration_minutes", "workload_profile",
    },
    "stop_container": {"container"},
    "confirm_action": {"confirmation_id", "phrase"},
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


class UnsafeActionPlanError(ValueError):
    pass


class CentralApiClient(Protocol):
    def pair(self, code: str, route_key: str) -> dict[str, Any]:
        ...

    def get_wechat_binding(self, route_key: str) -> dict[str, Any] | None:
        ...

    def submit_route_action(
        self,
        route_key: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        ...


class ActionParser(Protocol):
    def parse(self, message: str) -> dict[str, Any]:
        ...


@dataclass
class ConversationState:
    route_key: str = "lab-default"
    server_id: str = "server1"
    pending_confirmation: dict[str, Any] | None = None
    pending_print_job_id: str | None = None
    pending_tool: str | None = None
    last_container: str | None = None
    history: list[dict[str, str]] = field(default_factory=list)


def simulate_conversation(
    messages: list[str],
    *,
    client: CentralApiClient,
    llm_parser: ActionParser | None = None,
    state: ConversationState | None = None,
) -> dict[str, Any]:
    active_state = state or ConversationState()
    turns = []
    for message in messages:
        plan = plan_message(message)
        # A single name answers our own targeted question; do not interpret a
        # new request or a confirmation as the missing resource identifier.
        if not plan.get("actions") and active_state.pending_tool and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", message.strip()):
            plan = {"kind": "action_plan", "actions": [{"tool": active_state.pending_tool, "arguments": {"container": message.strip()}}]}
        if not plan.get("actions") and llm_parser is not None:
            contextual = getattr(llm_parser, "parse_with_context", None)
            plan = contextual(message, active_state.history) if contextual else llm_parser.parse(message)
        # The model cannot manufacture approval or replay a historical phrase.
        for action in plan.get("actions", []):
            if action.get("tool") in {"confirm_action", "confirm_print"}:
                action["arguments"] = {**action.get("arguments", {}), "phrase": message.strip()}
        turn = execute_plan(plan, state=active_state, client=client)
        turns.append({"message": message, "plan": plan, **turn})
        # Keep intent context only. Resource results/logs/credentials never go
        # to the external model. Sensitive user text is omitted as well.
        if not re.search(r"sk-[A-Za-z0-9]|password|密码|/pair\b|token|secret", message, re.I):
            context = [{"role": "user", "content": message[:600]}]
            if turn["result"] is None:
                context.append({"role": "assistant", "content": turn["reply"][:1200]})
            active_state.history = (active_state.history + context)[-6:]

    return {
        "route_key": active_state.route_key,
        "turns": turns,
        "pending_confirmation": active_state.pending_confirmation,
        "pending_print_job_id": active_state.pending_print_job_id,
    }


def execute_plan(
    plan: dict[str, Any],
    *,
    state: ConversationState,
    client: CentralApiClient,
) -> dict[str, Any]:
    _validate_plan(plan)
    actions = plan.get("actions", [])
    if not actions:
        return {"reply": "这条消息暂时没能解析。你可以告诉我想操作哪个容器、希望做什么；例如“我的容器”“启动 test”“查看 test 的日志”。输入“帮助”查看完整功能。", "result": None}

    action = actions[0]
    tool = action["tool"]
    arguments = action.get("arguments", {})

    if tool == "help":
        return {"reply": help_text(arguments.get("topic")), "result": None}
    if tool == "reply":
        reply = arguments.get("text")
        return {"reply": reply[:2000] if isinstance(reply, str) and reply.strip() else help_text(), "result": None}
    target_tools = {"start_container", "restart_container", "stop_container", "container_logs", "watch_training", "unwatch_training", "list_training_jobs", "diagnose_container"}
    if tool in target_tools:
        arguments = dict(arguments)
        target = arguments.get("container")
        if target in {"它", "这个", "那个"}:
            target = state.last_container
        if not isinstance(target, str) or not target.strip():
            state.pending_tool = tool
            return {"reply": "要操作哪个容器？请回复容器名称或完整 ID。也可以先说“我的容器”查看列表。", "result": None}
        arguments["container"] = target
        state.pending_tool = None

    if tool in {"list_containers", "start_container", "restart_container", "container_logs", "watch_training", "unwatch_training", "list_training_jobs"}:
        result = _action_result(_submit_route_action(client, state, tool, arguments))
        if result.get("status") in {"ambiguous", "not_found"}:
            state.pending_tool = tool
            choices = "\n".join(f"{item['name']}（{item.get('server_id')}）ID: {item['id']}" for item in result.get("candidates", [])[:10])
            return {"reply": ("找到多个同名容器，请回复完整 ID：\n" + choices) if result["status"] == "ambiguous" else "没有找到有权访问的容器，请检查名称或回复完整 ID。", "result": None}
        if tool != "list_containers":
            state.last_container = result.get("container_id") or arguments["container"]
        confirmation = result.get("confirmation")
        if result.get("requires_confirmation") and confirmation:
            state.pending_confirmation = _sanitize_confirmation(confirmation)
            return {"reply": f"重启会中断容器内正在运行的任务。请回复：{confirmation['required_phrase']}", "result": {"requires_confirmation": True, "confirmation": state.pending_confirmation}}
        if tool == "list_containers":
            rows = result.get("containers", [])
            if len(rows) == 1:
                state.last_container = rows[0]["id"]
            labels = {"running": "运行中", "created": "待启动", "stopped": "已停止", "exited": "已退出"}
            lines = []
            for row in rows[:12]:
                lines.append(f"{row['name']} · {labels.get(row.get('status'), row.get('status', '未知'))}\nID: {row['id']}\n{row.get('cpu_limit')}核 / {row.get('memory_limit_gb')}GiB / GPU {','.join(row.get('gpu_devices', [])) or '无'}")
                if row.get("ssh_connection"):
                    lines.append("SSH: " + row["ssh_connection"])
                if row.get("console_url"):
                    lines.append("SSH密码 / Jupyter: " + row["console_url"])
            if len(rows) > 12:
                lines.append(f"共 {len(rows)} 个容器，更多请到网页查看。")
            return {"reply": "\n\n".join(lines) or "你目前没有容器。可以告诉我名称、镜像和资源需求来创建。", "result": _sanitize_result(result)}
        if tool == "container_logs":
            logs = str(result.get("logs", ""))
            reply = f"{arguments['container']} 最近的容器日志：\n{logs[-3000:]}" if logs else "暂无容器标准输出日志。SSH 中运行的训练请用任务记录查看；普通后台进程的输出不一定写入容器日志。"
            if result.get("truncated") or len(logs) > 3000:
                reply += "\n（已截取末尾，完整日志请到网页查看。）"
            return {"reply": reply, "result": _sanitize_result(result)}
        if tool == "watch_training":
            return {"reply": "已开启该容器的任务通知。以后在容器终端这样启动任务：\npython /workspace/.gsm/gsm-run.py python train.py\n可将后面的命令替换为你的原命令。结束后通知退出码和日志摘要；已有任务不会自动接管。", "result": _sanitize_result(result)}
        if tool == "unwatch_training":
            return {"reply": "已关闭该容器的任务通知，正在运行的任务不受影响。", "result": _sanitize_result(result)}
        if tool == "list_training_jobs":
            lines = [f"{j.get('name', '任务')} · { {'running':'运行中', 'succeeded':'完成', 'failed':'失败', 'interrupted':'已中断'}.get(j.get('status'), j.get('status'))} · 退出码 {j.get('exit_code') if j.get('exit_code') is not None else '待定'}\n{j.get('log_excerpt', '')}" for j in result.get("jobs", [])[:5]]
            return {"reply": "\n\n".join(lines)[-3500:] or "暂时没有记录。先开启任务通知，再使用 gsm-run.py 前缀启动训练。", "result": _sanitize_result(result)}
        return {"reply": f"已启动容器 {arguments['container']}。", "result": _sanitize_result(result)}

    if tool == "list_gpus":
        response = _submit_route_action(
            client,
            state,
            "list_gpus",
            {"server_id": arguments.get("server_id") or state.server_id},
        )
        gpus = _action_result(response).get("gpus", [])
        return {"reply": _format_gpu_reply(gpus), "result": {"gpus": gpus}}

    if tool == "create_container":
        payload = _container_payload(arguments, server_id=state.server_id)
        response = _submit_route_action(
            client,
            state,
            "create_container",
            payload,
        )
        result = _action_result(response)
        if result.get("requires_approval"):
            approval = result.get("approval", {})
            return {
                "reply": f"该容器申请需要管理员审批，申请编号：{approval.get('id', '未知')}。",
                "result": _sanitize_result(result),
            }
        name = result.get("container", {}).get("name", payload["name"])
        return {"reply": f"已创建容器 {name}。", "result": _sanitize_result(result)}

    if tool == "stop_container":
        target_name = arguments["container"]
        response = _submit_route_action(client, state, tool, {"container": target_name})
        result = _action_result(response)
        if result.get("status") == "ambiguous":
            return {"reply": f"找到多个名为 {target_name} 的容器，请使用容器 ID。", "result": None}
        if result.get("status") == "not_found":
            return {"reply": f"没有找到容器 {target_name}。", "result": None}
        state.last_container = target_name
        confirmation = result.get("confirmation")
        if result.get("requires_confirmation") and confirmation:
            state.pending_confirmation = _sanitize_confirmation(confirmation)
            return {
                "reply": f"停止容器需要确认，请回复：{state.pending_confirmation['required_phrase']}",
                "result": {
                    "requires_confirmation": True,
                    "confirmation": state.pending_confirmation,
                },
            }
        return {"reply": f"已停止容器 {target_name}。", "result": result}

    if tool == "confirm_action":
        if state.pending_confirmation is None:
            return {"reply": "没有待确认操作。", "result": None}
        phrase = arguments["phrase"]
        confirmation_id = state.pending_confirmation["id"]
        result = _action_result(
            _submit_route_action(
                client,
                state,
                tool,
                {"confirmation_id": confirmation_id, "phrase": phrase},
            )
        )
        state.pending_confirmation = None
        return {"reply": "已执行确认操作。", "result": _sanitize_result(result)}

    if tool == "cancel_pending":
        state.pending_confirmation = None
        state.pending_tool = None
        return {"reply": "已取消待确认操作。", "result": None}

    if tool in {
        "recommend_gpus",
        "join_gpu_queue",
        "list_gpu_queue",
        "respond_idle",
        "decide_approval",
        "execute_approval",
        "list_approvals",
    }:
        response = _submit_route_action(client, state, tool, arguments)
        result = _action_result(response)
        return _governance_reply(tool, result)

    if tool in {"server_health", "diagnose_container"}:
        response = _submit_route_action(client, state, tool, arguments)
        result = _action_result(response)
        if tool == "diagnose_container" and result.get("status") == "ambiguous":
            result.setdefault("container", arguments.get("container"))
        return _operations_reply(tool, result)

    if tool == "update_print_job":
        job_id = arguments.get("job_id") or state.pending_print_job_id
        if job_id is None:
            return {"reply": "没有待设置的打印任务。", "result": None}
        if not arguments.get("printer_id"):
            return {"reply": "请明确指定打印机。", "result": None}
        payload = {**arguments, "job_id": job_id}
        result = _action_result(
            _submit_route_action(client, state, tool, payload)
        )
        state.pending_print_job_id = job_id
        phrase = result.get("required_phrase", f"确认打印 {job_id}")
        settings = result.get("settings", payload)
        reply = (
            f"打印设置已更新：{settings.get('printer_id')}，"
            f"{settings.get('page_ranges') or '全部页'}，"
            f"{settings.get('copies', 1)} 份。请回复：{phrase}"
        )
        return {"reply": reply, "result": _sanitize_result(result)}

    if tool == "confirm_print":
        job_id = arguments.get("job_id")
        if not job_id:
            return {"reply": "确认短语缺少打印任务编号。", "result": None}
        if state.pending_print_job_id and state.pending_print_job_id != job_id:
            return {"reply": "确认短语与当前打印任务不匹配。", "result": None}
        result = _action_result(
            _submit_route_action(client, state, tool, arguments)
        )
        state.pending_print_job_id = None
        return {"reply": "打印任务已排队。", "result": _sanitize_result(result)}

    if tool == "cancel_print":
        job_id = arguments.get("job_id") or state.pending_print_job_id
        if not job_id:
            return {"reply": "没有待取消的打印任务。", "result": None}
        result = _action_result(
            _submit_route_action(
                client,
                state,
                tool,
                {"job_id": job_id},
            )
        )
        state.pending_print_job_id = None
        return {"reply": "打印任务已取消。", "result": _sanitize_result(result)}

    if tool == "list_print_jobs":
        result = _action_result(
            _submit_route_action(client, state, tool, {})
        )
        jobs = result.get("jobs", [])
        if not jobs:
            return {"reply": "目前没有打印任务。", "result": {"jobs": []}}
        reply = "；".join(
            f"{job.get('id')}：{job.get('status', 'unknown')}" for job in jobs
        )
        return {"reply": reply, "result": _sanitize_result(result)}

    raise UnsafeActionPlanError(f"unsupported tool: {tool}")


def _validate_plan(plan: dict[str, Any]) -> None:
    if plan.get("kind") != "action_plan":
        raise UnsafeActionPlanError("plan kind must be action_plan")
    if not isinstance(plan.get("actions"), list) or len(plan["actions"]) > 1:
        raise UnsafeActionPlanError("one action per turn is required")
    for action in plan.get("actions", []):
        if not isinstance(action, dict):
            raise UnsafeActionPlanError("invalid action")
        tool = action.get("tool")
        if tool != "reply" and any(term in repr(action).lower() for term in ("shell", "docker ", "powershell", "bash")):
            raise UnsafeActionPlanError("plan contains forbidden execution terms")
        if tool not in ALLOWED_TOOLS:
            raise UnsafeActionPlanError(f"unsupported tool: {action.get('tool')}")
        arguments = action.get("arguments", {})
        if not isinstance(arguments, dict) or set(arguments) - ACTION_ARGUMENT_KEYS[tool]:
            raise UnsafeActionPlanError("unsupported action arguments")


def _format_gpu_reply(gpus: list[dict[str, Any]]) -> str:
    if not gpus:
        return "暂时没有 GPU 监控数据，请检查服务器连接。"
    parts = []
    for gpu in gpus:
        index = gpu.get("index")
        containers = gpu.get("containers", [])
        if not containers:
            status = gpu.get("status")
            parts.append(f"GPU{index} 空闲" if status == "free" else f"GPU{index} 暂无关联容器，是否空闲请以实时利用率和显存为准")
            continue
        owners = [
            f"{container.get('user_id')}/{container.get('container_name')}"
            for container in containers
        ]
        parts.append(f"GPU{index} {', '.join(owners)}")
    return "；".join(parts) + "。"


def _sanitize_confirmation(confirmation: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {"id", "required_phrase", "expires_at"}
    return {
        key: confirmation[key]
        for key in allowed_keys
        if key in confirmation
    }


def _submit_route_action(
    client: CentralApiClient,
    state: ConversationState,
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    submit = getattr(client, "submit_route_action", None)
    if submit is None:
        raise UnsafeActionPlanError("route-scoped client is required")
    return submit(state.route_key, tool, arguments)


def _action_result(response: dict[str, Any]) -> dict[str, Any]:
    if isinstance(response, dict) and isinstance(response.get("result"), dict):
        return response["result"]
    return response if isinstance(response, dict) else {}


def _governance_reply(tool: str, result: dict[str, Any]) -> dict[str, Any]:
    if tool == "recommend_gpus":
        recommendations = result.get("recommendations", [])
        if not recommendations:
            return {"reply": "当前没有满足条件的 GPU 推荐。", "result": _sanitize_result(result)}
        lines = []
        for item in recommendations:
            free_gib = float(item.get("memory_free_mb", 0)) / 1024
            state = item.get("state", "unknown")
            state_text = "可安全共享" if state == "shareable" else state
            reasons = ", ".join(item.get("reason_codes", []))
            lines.append(
                f"GPU{item.get('index')}（{item.get('server_id')}）：{state_text}，"
                f"可用 {free_gib:.1f} GiB，原因：{reasons}"
            )
        return {"reply": "\n".join(lines), "result": _sanitize_result(result)}
    if tool == "join_gpu_queue":
        position = result.get("position", "已加入")
        return {"reply": f"已加入 GPU 候补，当前第 {position} 位。", "result": _sanitize_result(result)}
    if tool == "list_gpu_queue":
        position = result.get("position")
        if position is None:
            return {"reply": "你当前没有 GPU 候补请求。", "result": _sanitize_result(result)}
        status = {"waiting": "等待中", "notified": "已通知"}.get(
            result.get("status"), result.get("status", "未知")
        )
        return {
            "reply": f"你的 GPU 候补当前第 {position} 位，状态：{status}。",
            "result": _sanitize_result(result),
        }
    if tool == "respond_idle":
        response = result.get("response")
        text = {
            "shareable": "这张 GPU 可以共享",
            "continue": "继续使用",
            "releasing": "准备释放",
        }.get(response, str(response or "已响应"))
        return {"reply": f"已记录：{text}。", "result": _sanitize_result(result)}
    if tool == "decide_approval":
        approval_status = result.get("status")
        label = "批准" if approval_status == "approved" else "拒绝" if approval_status == "rejected" else approval_status
        return {"reply": f"申请 {result.get('id', '未知')} 已{label}。", "result": _sanitize_result(result)}
    if tool == "execute_approval":
        name = result.get("container", {}).get("name", "未知")
        return {"reply": f"已按批准参数创建容器 {name}。", "result": _sanitize_result(result)}
    if tool == "list_approvals":
        return {"reply": f"当前有 {len(result.get('approvals', []))} 条审批记录。", "result": _sanitize_result(result)}
    return {"reply": "操作已完成。", "result": _sanitize_result(result)}


def _operations_reply(tool: str, result: dict[str, Any]) -> dict[str, Any]:
    if tool == "server_health":
        servers = result.get("servers", [])
        if not servers:
            return {"reply": "没有可用的服务器健康数据。", "result": _sanitize_result(result)}
        item = servers[0]
        status = {"ok": "正常", "degraded": "降级", "failed": "故障"}.get(
            item.get("status"), item.get("status", "未知")
        )
        return {
            "reply": (
                f"{item.get('server_id', '服务器')} 当前{status}，"
                f"{item.get('warning_count', 0)} 个警告、{item.get('critical_count', 0)} 个严重问题。"
            ),
            "result": _sanitize_result(result),
        }
    if result.get("status") == "ambiguous":
        name = result.get("container", "该容器")
        return {"reply": f"找到多个名为 {name} 的容器，请使用容器 ID。", "result": None}
    if result.get("status") == "not_found":
        return {"reply": "没有找到对应容器。", "result": None}
    findings = result.get("findings", [])
    if not findings:
        return {
            "reply": f"诊断未发现确定原因，报告编号：{result.get('id', '未知')}。",
            "result": _sanitize_result(result),
        }
    finding = findings[0]
    source = "规则" if result.get("source") == "rules" else "模型"
    confidence = float(finding.get("confidence", 0))
    return {
        "reply": (
            f"{finding.get('cause', '未明确原因')}（{source}，置信度 {confidence:.0%}）。"
            f"报告编号：{result.get('id', '未知')}；"
            f"/api/diagnostics/{result.get('id', '未知')}"
        ),
        "result": _sanitize_result(result),
    }


def _sanitize_result(result: dict[str, Any]) -> dict[str, Any]:
    sensitive = {
        "metadata", "token", "authorization", "api_url", "host_path",
        "process_command_line", "ssh_command", "ssh_public_key", "action_payload",
        "frozen_container_spec", "evidence_excerpt", "excerpts", "redaction_counts",
    }

    def clean(value):
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items() if key not in sensitive}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return clean(result)


def _container_payload(arguments: dict[str, Any], *, server_id: str) -> dict[str, Any]:
    image = arguments.get("image", "gsm/pytorch-cuda-ssh:latest")
    if image == "pytorch":
        image = "gsm/pytorch-cuda-ssh:latest"
    return {
        "name": arguments.get("name", "debug"),
        "server_id": server_id,
        "image": image,
        "preset": arguments.get("preset", "Debug"),
        "cpu_limit": arguments.get("cpu_limit", 4),
        "memory_limit_gb": arguments.get("memory_limit_gb", 16),
        "gpu_devices": arguments.get("gpu_devices", []),
        "mount_profile_ids": arguments.get("mount_profile_ids", ["workspace-default"]),
    }
