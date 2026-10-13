import pytest

from gsm_wechat_agent.conversation import ConversationState, simulate_conversation
from gsm_wechat_agent.planner import plan_message
from gsm_wechat_agent.llm_parser import _is_safe_plan
from gsm_api.provider_client import is_safe_action_plan


class Client:
    def __init__(self):
        self.calls = []

    def submit_route_action(self, route_key, tool, arguments):
        self.calls.append((route_key, tool, arguments))
        if tool == "restart_container":
            return {"result": {"requires_confirmation": True, "confirmation": {"id": "c1", "required_phrase": "确认重启 container train"}}}
        if tool == "list_containers":
            return {"result": {"containers": [{"id": "abc", "name": "train", "status": "running", "cpu_limit": 2, "memory_limit_gb": 4, "gpu_devices": [], "ssh_connection": "ssh researcher@10.0.0.1 -p 23001", "console_url": "https://manager.test/?workspace=containers"}]}}
        return {"result": {"container_id": arguments.get("container"), "logs": "epoch 1 complete"}}


@pytest.mark.parametrize("message", ["你有什么功能", "你能干什么？", "你能做什么", "帮助", "你好", "现在你还有什么功能啊"])
def test_help_is_available_without_model_or_api(message):
    client = Client()
    reply = simulate_conversation([message], client=client)["turns"][0]["reply"]
    assert "我的容器" in reply and "重启" in reply and "任务通知" in reply
    assert client.calls == []


def test_missing_target_followup_and_restart_confirmation():
    client = Client()
    result = simulate_conversation(["重启容器", "train", "确认重启 container train"], client=client)
    assert "哪个容器" in result["turns"][0]["reply"]
    assert "中断" in result["turns"][1]["reply"]
    assert [call[1] for call in client.calls] == ["restart_container", "confirm_action"]
    assert client.calls[-1][2]["phrase"] == "确认重启 container train"


def test_list_then_pronoun_preserves_exact_id():
    client = Client()
    result = simulate_conversation(["我的容器", "查看它的日志"], client=client)
    assert "ssh researcher" in result["turns"][0]["reply"]
    assert client.calls[-1][2]["container"] == "abc"
    assert "epoch 1" in result["turns"][1]["reply"]


def test_cancel_clears_target_prompt_and_confirmation():
    state = ConversationState(pending_tool="restart_container", pending_confirmation={"id": "c"})
    simulate_conversation(["取消"], client=Client(), state=state)
    assert state.pending_tool is None and state.pending_confirmation is None


def test_model_cannot_invent_confirmation_phrase():
    class Parser:
        def parse(self, message):
            return {"kind": "action_plan", "actions": [{"tool": "confirm_action", "arguments": {"phrase": "确认重启 container train"}}]}
    client = Client()
    state = ConversationState(pending_confirmation={"id": "c"})
    simulate_conversation(["随便"], client=client, llm_parser=Parser(), state=state)
    assert client.calls[-1][2]["phrase"] == "随便"


def test_result_logs_and_secrets_do_not_enter_model_context():
    state = ConversationState()
    client = Client()
    simulate_conversation(["查看train的日志", "密码是 secret-123"], client=client, state=state)
    assert "epoch" not in str(state.history) and "secret-123" not in str(state.history)


@pytest.mark.parametrize("message,tool", [("启动 test", "start_container"), ("重启 test", "restart_container"), ("查看 test 的日志", "container_logs"), ("开启 test 的任务通知", "watch_training"), ("关闭 test 的任务通知", "unwatch_training"), ("查看 test 的训练任务", "list_training_jobs")])
def test_tools_are_accepted_at_both_model_boundaries(message, tool):
    plan = plan_message(message)
    assert plan["actions"][0]["tool"] == tool
    assert is_safe_action_plan(plan) and _is_safe_plan(plan)


def test_model_can_explain_without_executing_any_tool():
    class Parser:
        def parse(self, message):
            return {"kind": "action_plan", "actions": [{"tool": "reply", "arguments": {"text": "你可以先选 PyTorch 环境，再告诉我资源需求。"}}]}
    client = Client()
    reply = simulate_conversation(["我是新生，怎么开始训练模型"], client=client, llm_parser=Parser())["turns"][0]["reply"]
    assert "PyTorch" in reply and not client.calls
