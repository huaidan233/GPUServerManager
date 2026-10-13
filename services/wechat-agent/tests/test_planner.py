from gsm_wechat_agent.planner import plan_message


def assert_no_shell_payload(value):
    rendered = repr(value).lower()
    assert "shell" not in rendered
    assert "docker " not in rendered
    assert "powershell" not in rendered
    assert "bash" not in rendered


def test_list_gpus_message_returns_structured_action_plan_only():
    plan = plan_message("show gpu status")

    assert plan == {
        "kind": "action_plan",
        "actions": [
            {
                "tool": "list_gpus",
                "arguments": {},
                "requires_confirmation": False,
            }
        ],
    }
    assert_no_shell_payload(plan)


def test_stop_container_requires_confirmation_and_no_shell_command():
    plan = plan_message("stop container llama-debug")

    assert plan["kind"] == "action_plan"
    assert plan["actions"] == [
        {
            "tool": "stop_container",
            "arguments": {"container": "llama-debug"},
            "requires_confirmation": True,
        }
    ]
    assert_no_shell_payload(plan)


def test_create_container_message_returns_create_container_action():
    plan = plan_message("create container train image gsm/pytorch-cuda-ssh:latest gpu 0,1")

    assert plan["actions"] == [
        {
            "tool": "create_container",
            "arguments": {
                "name": "train",
                "image": "gsm/pytorch-cuda-ssh:latest",
                "gpu_devices": ["0", "1"],
            },
            "requires_confirmation": False,
        }
    ]
    assert_no_shell_payload(plan)


def test_chinese_stop_container_requires_confirmation():
    plan = plan_message("停止我的 llama-debug")

    assert plan["actions"] == [
        {
            "tool": "stop_container",
            "arguments": {"container": "llama-debug"},
            "requires_confirmation": True,
        }
    ]
    assert_no_shell_payload(plan)


def test_chinese_gpu_usage_message_lists_gpus():
    plan = plan_message("现在谁在用 GPU")

    assert plan["actions"] == [
        {
            "tool": "list_gpus",
            "arguments": {},
            "requires_confirmation": False,
        }
    ]
    assert_no_shell_payload(plan)


def test_chinese_create_debug_container_extracts_resource_parameters_without_shell():
    plan = plan_message("给我建一个 Debug 容器，用 pytorch 镜像，4C16G，不要 GPU")

    assert plan["actions"] == [
        {
            "tool": "create_container",
            "arguments": {
                "name": "debug",
                "image": "pytorch",
                "preset": "Debug",
                "cpu_limit": 4,
                "memory_limit_gb": 16,
                "gpu_devices": [],
            },
            "requires_confirmation": False,
        }
    ]
    assert_no_shell_payload(plan)


def test_exact_confirmation_phrase_returns_confirm_action():
    plan = plan_message("确认停止 container llama-debug")

    assert plan["actions"] == [
        {
            "tool": "confirm_action",
            "arguments": {"phrase": "确认停止 container llama-debug"},
            "requires_confirmation": False,
        }
    ]
    assert_no_shell_payload(plan)


def test_cancel_message_returns_cancel_pending_action():
    for message in ["取消", "cancel"]:
        plan = plan_message(message)

        assert plan["actions"] == [
            {
                "tool": "cancel_pending",
                "arguments": {},
                "requires_confirmation": False,
            }
        ]
        assert_no_shell_payload(plan)
