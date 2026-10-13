from gsm_codex_mcp.tools import get_tool, list_tools


def test_codex_tools_include_required_gpu_container_actions():
    tools = list_tools()
    names = {tool["name"] for tool in tools}

    assert {
        "list_gpus",
        "create_container",
        "stop_container",
        "get_ssh_connection",
        "attach_gpu",
    } <= names


def test_each_tool_exposes_mcp_metadata_shape():
    for tool in list_tools():
        assert set(tool) >= {"name", "description", "input_schema"}
        assert tool["input_schema"]["type"] == "object"
        assert "properties" in tool["input_schema"]


def test_stop_container_tool_requires_confirmation():
    tool = get_tool("stop_container")

    assert tool["requires_confirmation"] is True
    assert tool["input_schema"]["required"] == ["container"]


def test_attach_gpu_schema_names_container_and_gpu_devices():
    tool = get_tool("attach_gpu")

    assert tool["requires_confirmation"] is True
    assert tool["input_schema"]["required"] == ["container", "gpu_devices"]
    assert tool["input_schema"]["properties"]["gpu_devices"]["type"] == "array"


def test_create_container_schema_exposes_central_api_fields():
    tool = get_tool("create_container")
    properties = tool["input_schema"]["properties"]

    for field in [
        "server_id",
        "name",
        "image",
        "preset",
        "cpu_limit",
        "memory_limit_gb",
        "gpu_devices",
        "mount_profile_ids",
    ]:
        assert field in properties

    assert properties["gpu_devices"]["type"] == "array"
    assert properties["mount_profile_ids"]["type"] == "array"


def test_get_ssh_connection_schema_requires_container_id():
    tool = get_tool("get_ssh_connection")

    assert tool["requires_confirmation"] is False
    assert tool["input_schema"]["required"] == ["container"]
