from pathlib import Path


def test_wechat_agent_package_has_no_direct_host_execution_primitives():
    root = Path("services/wechat-agent/gsm_wechat_agent")
    source = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    lowered = source.lower()

    for forbidden in ["subprocess", "os.system", "popen", "eval(", "exec("]:
        assert forbidden not in lowered
