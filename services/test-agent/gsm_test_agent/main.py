from __future__ import annotations

import argparse
import json
import os
from typing import Sequence

import httpx
from fastapi import FastAPI, Header, HTTPException, status

from gsm_test_agent.checks import health_check, run_smoke_checks


def create_app() -> FastAPI:
    app = FastAPI(title="GPU Server Manager Test Agent", version="0.3.0-rc.5")

    @app.get("/test-agent/health")
    def health():
        return health_check().to_dict()

    @app.post("/test-agent/smoke")
    def smoke(authorization: str | None = Header(default=None)):
        _require_test_agent_token(authorization)
        return run_smoke_checks()

    return app


app = create_app()


def _require_test_agent_token(authorization: str | None) -> None:
    expected = os.environ.get("GSM_TEST_AGENT_TOKEN")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GSM_TEST_AGENT_TOKEN is not configured",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gsm-test-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    smoke = subparsers.add_parser("smoke", help="Run black-box deployment smoke checks")
    smoke.add_argument("--api-url", default="http://api:8000")
    smoke.add_argument("--web-url", default=None)
    smoke.add_argument("--token", default=None)
    smoke.add_argument("--user-token", default=None)
    smoke.add_argument("--viewer-token", default=None)
    smoke.add_argument("--image-pull-token", default=None)
    smoke.add_argument("--agent-url", default=None)
    smoke.add_argument("--agent-token", default="change-me-agent-token")
    smoke.add_argument("--exercise-confirmation", action="store_true")
    smoke.add_argument("--exercise-image-pull", action="store_true")
    smoke.add_argument("--exercise-governance", action="store_true")
    smoke.add_argument("--exercise-health", action="store_true")
    smoke.add_argument("--exercise-backup", action="store_true")
    smoke.add_argument("--exercise-print", action="store_true")
    smoke.add_argument("--exercise-member-identity", action="store_true")
    smoke.add_argument(
        "--member-username",
        default=os.environ.get("GSM_SMOKE_MEMBER_USERNAME"),
    )
    smoke.add_argument(
        "--member-password",
        default=os.environ.get("GSM_SMOKE_MEMBER_PASSWORD"),
    )
    smoke.add_argument(
        "--member-totp",
        default=os.environ.get("GSM_SMOKE_MEMBER_TOTP"),
    )
    smoke.add_argument(
        "--wechat-route-key",
        default=os.environ.get("GSM_SMOKE_WECHAT_ROUTE_KEY"),
    )
    smoke.add_argument(
        "--wechat-service-token",
        default=os.environ.get("GSM_WECHAT_SERVICE_TOKEN"),
    )
    smoke.add_argument(
        "--print-agent-token",
        default=os.environ.get("GSM_TEST_PRINT_AGENT_TOKEN"),
    )
    smoke.add_argument("--json", action="store_true")

    serve = subparsers.add_parser("serve", help="Run the Test Agent HTTP service")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client: httpx.Client | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "smoke":
        report = run_smoke_checks(
            api_url=args.api_url,
            web_url=args.web_url,
            token=args.token,
            user_token=args.user_token,
            viewer_token=args.viewer_token,
            image_pull_token=args.image_pull_token,
            agent_url=args.agent_url,
            agent_token=args.agent_token,
            exercise_confirmation=args.exercise_confirmation,
            exercise_image_pull=args.exercise_image_pull,
            exercise_governance=args.exercise_governance,
            exercise_health=args.exercise_health,
            exercise_backup=args.exercise_backup,
            exercise_print=args.exercise_print,
            print_agent_token=args.print_agent_token,
            exercise_member_identity=args.exercise_member_identity,
            member_username=args.member_username,
            member_password=args.member_password,
            member_totp=args.member_totp,
            wechat_route_key=args.wechat_route_key,
            wechat_service_token=args.wechat_service_token,
            client=client,
        )
        output = json.dumps(report, ensure_ascii=False, indent=2) if args.json else _format_report(report)
        print(output)
        return 0 if report["status"] == "ok" else 1

    if args.command == "serve":
        import uvicorn

        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    parser.error(f"unknown command {args.command}")
    return 2


def _format_report(report: dict) -> str:
    lines = [f"Test Agent smoke: {report['status']} ({report['passed']}/{report['total']} passed)"]
    for check in report["checks"]:
        mark = "PASS" if check["ok"] else "FAIL"
        lines.append(f"- {mark} {check['name']}: {check['message']}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
