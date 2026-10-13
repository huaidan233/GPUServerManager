import httpx

from gsm_wechat_agent.llm_parser import CentralGatewayActionParser


def test_parser_uses_central_gateway_without_provider_key():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_valid_plan())

    parser = CentralGatewayActionParser(
        api_url="https://central.example",
        service_token="wechat-service",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert parser.parse("show gpu") == _valid_plan()
    request = requests[0]
    assert str(request.url) == "https://central.example/api/internal/agent-llm/parse"
    assert request.headers["authorization"] == "Bearer wechat-service"
    assert request.read() == b'{"message":"show gpu"}'


def test_parser_explains_gateway_failure_without_executing_an_action():
    parser = CentralGatewayActionParser(
        api_url="https://central.example",
        service_token="wechat-service",
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(503))
        ),
    )

    action = parser.parse("show gpu")["actions"][0]
    assert action["tool"] == "reply" and "连接不上" in action["arguments"]["text"]


def test_parser_revalidates_gateway_plan():
    parser = CentralGatewayActionParser(
        api_url="https://central.example",
        service_token="wechat-service",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={
                        "kind": "action_plan",
                        "actions": [
                            {
                                "tool": "run_shell",
                                "arguments": {"command": "docker ps"},
                                "requires_confirmation": False,
                            }
                        ],
                    },
                )
            )
        ),
    )

    assert parser.parse("run it") == {"kind": "action_plan", "actions": []}


def test_parser_rejects_multiple_actions_and_unknown_arguments():
    parser = CentralGatewayActionParser(
        api_url="https://central.example",
        service_token="wechat-service",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={
                        "kind": "action_plan",
                        "actions": [
                            {
                                "tool": "list_gpus",
                                "arguments": {},
                                "requires_confirmation": False,
                            },
                            {
                                "tool": "create_container",
                                "arguments": {"path": "/api/audit-logs"},
                                "requires_confirmation": False,
                            },
                        ],
                    },
                )
            )
        ),
    )

    assert parser.parse("multi action") == {
        "kind": "action_plan",
        "actions": [],
    }


def _valid_plan():
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
