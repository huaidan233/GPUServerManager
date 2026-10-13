from __future__ import annotations

import re
from typing import Any


def plan_message(message: str) -> dict[str, Any]:
    text = message.strip()
    lowered = text.lower()

    compact = re.sub(r"[\s，。！？?！,.]+", "", lowered)
    if compact in {"你好", "您好", "嗨", "hello", "hi", "在吗", "帮助", "help", "/help", "菜单", "功能", "你是谁"} or re.fullmatch(
        r"(?:请问|那|现在|目前)?你(?:都|还|现在|目前)?(?:有(?:哪些|什么)功能|能(?:做|干|帮我做)(?:什么|啥)|会(?:做)?(?:什么|啥))(?:呢|啊|呀|吗)?", compact
    ):
        return _action_plan("help", {})
    if compact in {"谢谢", "谢谢你", "好的", "收到", "ok", "thanks"}:
        return _action_plan("reply", {"text": "好的，有需要直接告诉我。输入“帮助”可以查看功能和例子。"})
    if compact in {"我的容器", "查看我的容器", "容器列表", "列出容器", "我有哪些容器", "看看我的容器"}:
        return _action_plan("list_containers", {})
    if compact in {"查看待审批申请", "待审批", "审批列表", "我的申请"}:
        return _action_plan("list_approvals", {})
    if compact in {"训练通知", "任务通知", "怎么接收训练通知", "怎么用训练通知"}:
        return _action_plan("help", {"topic": "training"})

    target = r"([A-Za-z0-9_.-]+|它|这个|那个)"
    for verb, tool in (("启动|开启|开机", "start_container"), ("重启|重新启动", "restart_container")):
        match = re.fullmatch(r"(?:请|帮我)?(?:" + verb + r")(?:我的)?(?:容器)?\s*" + target + r"[。！!]?", text)
        if match:
            return _action_plan(tool, {"container": match.group(1)})
    log_match = re.fullmatch(r"(?:查看|看看|看|获取)?\s*" + target + r"\s*(?:的)?(?:最近)?(?:日志|报错)[。？?]?", text)
    if not log_match:
        log_match = re.fullmatch(r"(?:查看|看看|看)?(?:容器)?日志\s*" + target, text)
    if log_match:
        return _action_plan("container_logs", {"container": log_match.group(1)})
    for prefix, tool in (("开启|启用|订阅", "watch_training"), ("关闭|取消", "unwatch_training"), ("查看|看看|查询", "list_training_jobs")):
        match = re.fullmatch(r"(?:" + prefix + r")\s*" + target + r"\s*(?:的)?(?:训练|任务|训练任务)(?:通知|提醒)?", text)
        if match:
            return _action_plan(tool, {"container": match.group(1)})
    missing_target = {"启动容器": "start_container", "重启容器": "restart_container", "停止容器": "stop_container", "查看日志": "container_logs", "看看日志": "container_logs", "开启任务通知": "watch_training", "查看训练任务": "list_training_jobs"}
    if compact in missing_target:
        return _action_plan(missing_target[compact], {})

    print_confirmation = re.fullmatch(
        r"确认打印\s+([A-Za-z0-9_-]+)", text
    )
    if print_confirmation:
        return _action_plan(
            "confirm_print",
            {
                "job_id": print_confirmation.group(1),
                "phrase": text,
            },
        )

    if text in {"取消打印", "取消当前打印"}:
        return _action_plan("cancel_print", {})

    if text in {"打印状态", "我的打印", "打印任务"}:
        return _action_plan("list_print_jobs", {})

    if "打印" in text and any(
        term in text for term in ("打印机", "单面", "双面", "黑白", "彩色", "份", "页码")
    ):
        arguments: dict[str, Any] = {
            "copies": 1,
            "page_ranges": "",
            "sides": "simplex",
            "color_mode": "monochrome",
            "paper": "A4",
        }
        printer_match = re.search(r"打印机\s*([A-Za-z0-9_.-]+)", text)
        if printer_match:
            arguments["printer_id"] = printer_match.group(1)
        copies_match = re.search(r"(\d+)\s*份", text)
        if copies_match:
            arguments["copies"] = int(copies_match.group(1))
        pages_match = re.search(r"页码\s*([0-9,-]+)", text)
        if pages_match:
            arguments["page_ranges"] = pages_match.group(1)
        if "双面" in text:
            arguments["sides"] = "duplex_long_edge"
        if "彩色" in text:
            arguments["color_mode"] = "color"
        if "自动纸张" in text:
            arguments["paper"] = "auto"
        return _action_plan("update_print_job", arguments)

    if lowered in {"cancel", "取消"}:
        return _action_plan("cancel_pending", {})

    if text.startswith("确认"):
        return _action_plan("confirm_action", {"phrase": text})

    approval_match = re.fullmatch(r"批准申请\s+([A-Za-z0-9_-]+)", text)
    if approval_match:
        return _action_plan(
            "decide_approval",
            {
                "approval_id": approval_match.group(1),
                "decision": "approved",
                "reason": "微信审批",
            },
        )
    reject_match = re.fullmatch(r"拒绝申请\s+([A-Za-z0-9_-]+)(?:\s+(.+))?", text)
    if reject_match:
        return _action_plan(
            "decide_approval",
            {
                "approval_id": reject_match.group(1),
                "decision": "rejected",
                "reason": reject_match.group(2) or "微信审批",
            },
        )
    execute_match = re.fullmatch(r"执行已批准申请\s+([A-Za-z0-9_-]+)", text)
    if execute_match:
        return _action_plan("execute_approval", {"approval_id": execute_match.group(1)})

    if "候补排" in text or "排第几" in text:
        return _action_plan("list_gpu_queue", {})
    if "可以共享" in text or "可共享" in text:
        return _action_plan("respond_idle", {"response": "shareable"})
    if "准备释放" in text or "即将释放" in text:
        return _action_plan("respond_idle", {"response": "releasing"})
    if "继续使用" in text or "继续跑" in text:
        return _action_plan("respond_idle", {"response": "continue"})

    server_health_match = re.search(
        r"(?:服务器|server)\s*([0-9]+|[A-Za-z][A-Za-z0-9_-]*)\s*(?:正常|健康|状态|怎么样|如何|吗)",
        text,
        flags=re.IGNORECASE,
    )
    if server_health_match:
        value = server_health_match.group(1)
        server_id = f"server{value}" if value.isdigit() else value
        return _action_plan("server_health", {"server_id": server_id})

    diagnosis_match = re.search(
        r"(?:我的\s*)?([A-Za-z0-9_.-]+)\s*(?:为什么|为何).*(?:退出|停止|失败|报错|挂)",
        text,
    )
    if diagnosis_match:
        return _action_plan("diagnose_container", {"container": diagnosis_match.group(1)})

    recommend_match = re.search(r"推荐.*?(\d+)\s*[Gg]显存", text)
    if recommend_match:
        return _action_plan(
            "recommend_gpus",
            {
                "gpu_count": 1,
                "memory_tier": _memory_tier(int(recommend_match.group(1))),
                "estimated_duration_minutes": 120,
            },
        )
    queue_match = re.search(
        r"加入\s*(\d+)\s*[Gg](?:显存)?\s*候补|加入.*?候补.*?(\d+)\s*[Gg]",
        text,
    )
    if queue_match:
        hours_match = re.search(r"(\d+)\s*小时", text)
        requested_memory = int(queue_match.group(1) or queue_match.group(2))
        return _action_plan(
            "join_gpu_queue",
            {
                "gpu_count": 1,
                "memory_tier": _memory_tier(requested_memory),
                "estimated_duration_minutes": int(hours_match.group(1)) * 60
                if hours_match
                else 120,
                "compatible_server_ids": ["server1"],
                "workload_profile": "standard",
            },
        )

    chinese_stop_match = re.search(r"停止(?:我的)?\s*([A-Za-z0-9_.-]+)", text)
    if chinese_stop_match:
        return _action_plan(
            "stop_container",
            {"container": chinese_stop_match.group(1)},
            requires_confirmation=True,
        )

    stop_match = re.search(r"\bstop\s+container\s+([A-Za-z0-9_.-]+)\b", lowered)
    if stop_match:
        return _action_plan(
            "stop_container",
            {"container": stop_match.group(1)},
            requires_confirmation=True,
        )

    create_match = re.search(
        r"\bcreate\s+container\s+([A-Za-z0-9_.-]+)\s+image\s+(\S+)\s+gpu\s+([A-Za-z0-9,._-]+)\b",
        lowered,
    )
    if create_match:
        return _action_plan(
            "create_container",
            {
                "name": create_match.group(1),
                "image": create_match.group(2),
                "gpu_devices": [
                    device
                    for device in create_match.group(3).split(",")
                    if device
                ],
            },
        )

    if "建" in text and "容器" in text:
        arguments: dict[str, Any] = {}
        if re.search(r"\bdebug\b", text, flags=re.IGNORECASE):
            arguments["name"] = "debug"
            arguments["preset"] = "Debug"

        image_match = re.search(r"用\s*([A-Za-z0-9_.:/-]+)\s*镜像", text)
        if image_match:
            arguments["image"] = image_match.group(1).lower()

        resources_match = re.search(r"(\d+)\s*C\s*(\d+)\s*G", text, flags=re.IGNORECASE)
        if resources_match:
            arguments["cpu_limit"] = int(resources_match.group(1))
            arguments["memory_limit_gb"] = int(resources_match.group(2))

        if "不要 gpu" in lowered or "不要gpu" in lowered:
            arguments["gpu_devices"] = []

        if arguments:
            return _action_plan("create_container", arguments)

    if "gpu" in lowered and not any(word in text for word in ("怎么", "如何", "什么", "功能", "能否", "能做", "创建", "删除", "停止", "重启")):
        return _action_plan("list_gpus", {})

    return {"kind": "action_plan", "actions": []}


def _action_plan(
    tool: str,
    arguments: dict[str, Any],
    *,
    requires_confirmation: bool = False,
) -> dict[str, Any]:
    return {
        "kind": "action_plan",
        "actions": [
            {
                "tool": tool,
                "arguments": arguments,
                "requires_confirmation": requires_confirmation,
            }
        ],
    }


def _memory_tier(memory_gb: int) -> str:
    if memory_gb < 8:
        return "lt_8"
    if memory_gb <= 24:
        return "8_24"
    if memory_gb <= 48:
        return "24_48"
    return "full_card"
