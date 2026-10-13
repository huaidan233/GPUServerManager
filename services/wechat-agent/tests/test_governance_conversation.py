from gsm_wechat_agent.conversation import ConversationState, simulate_conversation
from gsm_wechat_agent.planner import plan_message


def tool(message):
    actions = plan_message(message)["actions"]
    return actions[0]["tool"] if actions else None


def test_governance_intents_are_parsed_without_guessing_ids():
    assert tool("给我推荐一张24G显存的卡") == "recommend_gpus"
    assert tool("加入24G候补，预计用4小时") == "join_gpu_queue"
    assert tool("我的候补排第几") == "list_gpu_queue"
    assert tool("可以共享") == "respond_idle"
    assert tool("批准申请 a1") == "decide_approval"
    assert tool("执行已批准申请 a1") == "execute_approval"
    assert tool("服务器1正常吗") == "server_health"
    assert tool("我的 llama-debug 为什么退出") == "diagnose_container"

    assert plan_message("批准申请")["actions"] == []
    assert plan_message("批准申请 ???")["actions"] == []
    assert plan_message("执行已批准申请")["actions"] == []


class RouteActionClient:
    def __init__(self):
        self.calls = []

    def submit_route_action(self, route_key, tool_name, arguments):
        self.calls.append((route_key, tool_name, arguments))
        results = {
            "recommend_gpus": {
                "recommendations": [
                    {
                        "server_id": "server1",
                        "index": 0,
                        "state": "shareable",
                        "memory_free_mb": 32768,
                        "reason_codes": ["mounted_without_process", "advisory_only"],
                    }
                ]
            },
            "join_gpu_queue": {"id": "q1", "status": "waiting", "position": 2},
            "list_gpu_queue": {"position": 2, "status": "waiting"},
            "respond_idle": {"id": "idle1", "status": "responded", "response": "shareable"},
            "decide_approval": {
                "id": "a1",
                "status": "approved",
                "reviewer_user_id": "admin",
            },
            "execute_approval": {
                "container": {"id": "c1", "name": "large-job", "status": "created"},
                "warnings": [],
            },
            "server_health": {
                "servers": [
                    {
                        "server_id": "server1",
                        "status": "degraded",
                        "warning_count": 1,
                        "critical_count": 0,
                        "stale": False,
                    }
                ]
            },
            "diagnose_container": {
                "id": "report-1",
                "container_id": "c1",
                "source": "rules",
                "findings": [
                    {
                        "code": "cuda_oom",
                        "cause": "GPU 显存不足",
                        "confidence": 0.98,
                        "excerpts": ["CUDA out of memory raw detail"],
                    }
                ],
                "evidence_excerpt": "raw logs must not be returned",
            },
        }
        return {"tool": tool_name, "result": results[tool_name]}


def test_governance_conversation_uses_only_bound_route_actions_and_formats_results():
    client = RouteActionClient()
    state = ConversationState(route_key="account:chat:alice")

    transcript = simulate_conversation(
        [
            "给我推荐一张24G显存的卡",
            "加入24G候补，预计用4小时",
            "我的候补排第几",
            "可以共享",
        ],
        client=client,
        state=state,
    )

    replies = [turn["reply"] for turn in transcript["turns"]]
    assert "GPU0" in replies[0]
    assert "32.0 GiB" in replies[0]
    assert "可安全共享" in replies[0]
    assert replies[1] == "已加入 GPU 候补，当前第 2 位。"
    assert replies[2] == "你的 GPU 候补当前第 2 位，状态：等待中。"
    assert replies[3] == "已记录：这张 GPU 可以共享。"
    assert all(call[0] == "account:chat:alice" for call in client.calls)
    assert client.calls[1][2]["estimated_duration_minutes"] == 240


def test_admin_approval_conversation_keeps_route_identity_at_central_boundary():
    client = RouteActionClient()
    transcript = simulate_conversation(
        ["批准申请 a1", "执行已批准申请 a1"],
        client=client,
        state=ConversationState(route_key="account:chat:admin"),
    )

    assert transcript["turns"][0]["reply"] == "申请 a1 已批准。"
    assert transcript["turns"][1]["reply"] == "已按批准参数创建容器 large-job。"
    assert client.calls == [
        (
            "account:chat:admin",
            "decide_approval",
            {"approval_id": "a1", "decision": "approved", "reason": "微信审批"},
        ),
        (
            "account:chat:admin",
            "execute_approval",
            {"approval_id": "a1"},
        ),
    ]


def test_governance_reply_does_not_expose_sensitive_result_fields():
    class SensitiveClient(RouteActionClient):
        def submit_route_action(self, route_key, tool_name, arguments):
            return {
                "tool": tool_name,
                "result": {
                    "recommendations": [],
                    "token": "secret",
                    "host_path": "/srv/users/alice",
                    "process_command_line": "python train.py --key secret",
                },
            }

    transcript = simulate_conversation(
        ["给我推荐一张24G显存的卡"],
        client=SensitiveClient(),
        state=ConversationState(route_key="account:chat:alice"),
    )

    rendered = repr(transcript).lower()
    for forbidden in ["secret", "host_path", "/srv/users", "process_command_line"]:
        assert forbidden not in rendered


def test_health_and_diagnosis_replies_are_short_and_do_not_expose_logs():
    client = RouteActionClient()
    transcript = simulate_conversation(
        ["服务器1正常吗", "我的 llama-debug 为什么退出"],
        client=client,
        state=ConversationState(route_key="account:chat:alice"),
    )

    health_reply = transcript["turns"][0]["reply"]
    diagnosis_turn = transcript["turns"][1]
    assert health_reply == "server1 当前降级，1 个警告、0 个严重问题。"
    assert "GPU 显存不足" in diagnosis_turn["reply"]
    assert "规则" in diagnosis_turn["reply"]
    assert "98%" in diagnosis_turn["reply"]
    assert "report-1" in diagnosis_turn["reply"]
    assert "/api/diagnostics/report-1" in diagnosis_turn["reply"]
    rendered = repr(diagnosis_turn)
    assert "raw logs" not in rendered
    assert "CUDA out of memory raw detail" not in rendered


def test_ambiguous_container_diagnosis_requests_an_id():
    class AmbiguousClient(RouteActionClient):
        def submit_route_action(self, route_key, tool_name, arguments):
            return {"tool": tool_name, "result": {"status": "ambiguous"}}

    transcript = simulate_conversation(
        ["我的 llama-debug 为什么退出"],
        client=AmbiguousClient(),
        state=ConversationState(route_key="account:chat:alice"),
    )

    assert transcript["turns"][0]["reply"] == "找到多个名为 llama-debug 的容器，请使用容器 ID。"
