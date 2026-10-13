from pathlib import Path

import pytest

from gsm_wechat_agent.conversation import (
    ConversationState,
    UnsafeActionPlanError,
    execute_plan,
    simulate_conversation,
)


class FakeCentralClient:
    def __init__(self):
        self.calls = []
        self.containers = [{"id": "c1", "name": "llama-debug", "user_id": "alice"}]

    def list_gpus(self, server_id="server1"):
        self.calls.append(("list_gpus", server_id))
        return [
            {"index": 0, "status": "free", "containers": []},
            {
                "index": 1,
                "status": "occupied",
                "containers": [{"user_id": "alice", "container_name": "llama-debug"}],
            },
        ]

    def list_containers(self):
        self.calls.append(("list_containers",))
        return list(self.containers)

    def create_container(self, payload):
        self.calls.append(("create_container", payload))
        return {
            "container": {
                "id": "c2",
                "name": payload["name"],
                "status": "created",
                "metadata": {"agent_container_id": "agent-c2"},
            },
            "warnings": [],
        }

    def stop_container_by_id(self, container_id):
        self.calls.append(("stop_container_by_id", container_id))
        return {
            "requires_confirmation": True,
            "confirmation": {
                "id": "confirm-1",
                "required_phrase": "确认停止 container llama-debug",
                "actor_user_id": "alice",
                "action_payload": {"container_id": "c1"},
            },
        }

    def confirm(self, confirmation_id, phrase):
        self.calls.append(("confirm", confirmation_id, phrase))
        return {
            "status": "executed",
            "container": {
                "id": "c1",
                "name": "llama-debug",
                "status": "stopped",
                "metadata": {"agent_container_id": "agent-c1"},
            },
        }

    def submit_route_action(self, route_key, tool, arguments):
        self.calls.append(("submit_route_action", route_key, tool, arguments))
        if tool == "list_gpus":
            return {"tool": tool, "result": {"gpus": [
                {"index": 0, "status": "free", "containers": []},
                {
                    "index": 1,
                    "status": "occupied",
                    "containers": [{"user_id": "alice", "container_name": "llama-debug"}],
                },
            ]}}
        if tool == "create_container":
            return {"tool": tool, "result": {
                "container": {"id": "c2", "name": arguments["name"], "status": "created"},
                "warnings": [],
            }}
        if tool == "stop_container":
            matches = [
                item for item in self.containers
                if item.get("id") == arguments["container"]
                or item.get("name") == arguments["container"]
            ]
            if len(matches) > 1:
                return {"tool": tool, "result": {"status": "ambiguous"}}
            if not matches:
                return {"tool": tool, "result": {"status": "not_found"}}
            return {"tool": tool, "result": {
                "requires_confirmation": True,
                "confirmation": {
                    "id": "confirm-1",
                    "required_phrase": "确认停止 container llama-debug",
                },
            }}
        if tool == "confirm_action":
            return {"tool": tool, "result": {"status": "executed"}}
        raise AssertionError(tool)


class FakeLlmParser:
    def parse(self, message):
        assert message == "帮我看看显卡"
        return {
            "kind": "action_plan",
            "actions": [
                {
                    "tool": "list_gpus",
                    "arguments": {},
                    "requires_confirmation": False,
                }
            ],
        }


def test_simulated_conversation_lists_gpu_usage_and_creates_debug_container():
    client = FakeCentralClient()

    transcript = simulate_conversation(
        [
            "现在谁在用 GPU",
            "给我建一个 Debug 容器，用 pytorch 镜像，4C16G，不要 GPU",
        ],
        client=client,
    )

    replies = [turn["reply"] for turn in transcript["turns"]]
    assert "GPU0 空闲" in replies[0]
    assert "GPU1 alice/llama-debug" in replies[0]
    assert replies[1] == "已创建容器 debug。"
    assert client.calls[-1] == (
        "submit_route_action",
        "lab-default",
        "create_container",
        {
            "name": "debug",
            "server_id": "server1",
            "image": "gsm/pytorch-cuda-ssh:latest",
            "preset": "Debug",
            "cpu_limit": 4,
            "memory_limit_gb": 16,
            "gpu_devices": [],
            "mount_profile_ids": ["workspace-default"],
        },
    )


def test_simulated_conversation_uses_llm_fallback_when_rules_do_not_match():
    client = FakeCentralClient()

    transcript = simulate_conversation(
        ["帮我看看显卡"],
        client=client,
        llm_parser=FakeLlmParser(),
    )

    assert transcript["turns"][0]["plan"]["actions"][0]["tool"] == "list_gpus"
    assert "GPU0 空闲" in transcript["turns"][0]["reply"]
    assert client.calls[0] == (
        "submit_route_action",
        "lab-default",
        "list_gpus",
        {"server_id": "server1"},
    )


def test_stop_container_requires_confirmation_then_exact_phrase_executes():
    client = FakeCentralClient()

    transcript = simulate_conversation(
        [
            "停止我的 llama-debug",
            "确认停止 container llama-debug",
        ],
        client=client,
    )

    assert transcript["turns"][0]["reply"] == "停止容器需要确认，请回复：确认停止 container llama-debug"
    assert transcript["turns"][1]["reply"] == "已执行确认操作。"
    assert client.calls == [
        ("submit_route_action", "lab-default", "stop_container", {"container": "llama-debug"}),
        (
            "submit_route_action",
            "lab-default",
            "confirm_action",
            {"confirmation_id": "confirm-1", "phrase": "确认停止 container llama-debug"},
        ),
    ]
    assert transcript["pending_confirmation"] is None
    rendered = str(transcript).lower()
    assert "actor_user_id" not in rendered
    assert "action_payload" not in rendered
    assert "agent_container_id" not in rendered
    assert "metadata" not in rendered


def test_cancel_clears_pending_confirmation_without_calling_confirm():
    client = FakeCentralClient()

    transcript = simulate_conversation(
        [
            "停止我的 llama-debug",
            "取消",
        ],
        client=client,
    )

    assert transcript["turns"][1]["reply"] == "已取消待确认操作。"
    assert not any(len(call) > 2 and call[2] == "confirm_action" for call in client.calls)
    assert transcript["pending_confirmation"] is None


def test_pending_confirmation_is_sanitized_in_transcript():
    client = FakeCentralClient()

    transcript = simulate_conversation(["停止我的 llama-debug"], client=client)

    assert transcript["pending_confirmation"] == {
        "id": "confirm-1",
        "required_phrase": "确认停止 container llama-debug",
    }
    rendered = str(transcript).lower()
    assert "actor_user_id" not in rendered
    assert "action_payload" not in rendered


def test_unknown_container_does_not_call_stop():
    client = FakeCentralClient()

    transcript = simulate_conversation(["停止我的 missing"], client=client)

    assert transcript["turns"][0]["reply"] == "没有找到容器 missing。"
    assert client.calls == [
        ("submit_route_action", "lab-default", "stop_container", {"container": "missing"})
    ]


def test_ambiguous_container_name_does_not_call_stop():
    client = FakeCentralClient()
    client.containers = [
        {"id": "c1", "name": "debug"},
        {"id": "c2", "name": "debug"},
    ]

    transcript = simulate_conversation(["停止我的 debug"], client=client)

    assert transcript["turns"][0]["reply"] == "找到多个名为 debug 的容器，请使用容器 ID。"
    assert client.calls == [
        ("submit_route_action", "lab-default", "stop_container", {"container": "debug"})
    ]


def test_unsafe_action_plan_is_rejected_before_execution():
    client = FakeCentralClient()
    state = ConversationState()

    with pytest.raises(UnsafeActionPlanError):
        execute_plan(
            {
                "kind": "action_plan",
                "actions": [
                    {
                        "tool": "run_shell",
                        "arguments": {"command": "docker ps"},
                        "requires_confirmation": False,
                    }
                ],
            },
            state=state,
            client=client,
        )

    assert client.calls == []


def test_conversation_source_has_no_direct_execution_or_docker_boundary():
    source = Path("services/wechat-agent/gsm_wechat_agent/conversation.py").read_text(encoding="utf-8")
    lowered = source.lower()

    for forbidden in ["subprocess", "os.system", "eval("]:
        assert forbidden not in lowered
