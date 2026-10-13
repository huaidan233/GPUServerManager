from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "message": self.message,
            "details": dict(self.details),
        }


def summarize(checks: list[CheckResult]) -> dict[str, Any]:
    passed = sum(1 for check in checks if check.ok)
    failed = len(checks) - passed
    return {
        "status": "ok" if failed == 0 else "failed",
        "total": len(checks),
        "passed": passed,
        "failed": failed,
        "checks": [check.to_dict() for check in checks],
    }


def health_check() -> CheckResult:
    return CheckResult("health", True, "test-agent ready")


def run_smoke_checks(
    *,
    api_url: str | None = None,
    web_url: str | None = None,
    token: str | None = None,
    user_token: str | None = None,
    viewer_token: str | None = None,
    image_pull_token: str | None = None,
    agent_url: str | None = None,
    agent_token: str = "change-me-agent-token",
    exercise_confirmation: bool = False,
    exercise_image_pull: bool = False,
    exercise_governance: bool = False,
    exercise_health: bool = False,
    exercise_backup: bool = False,
    exercise_print: bool = False,
    print_agent_token: str | None = None,
    exercise_member_identity: bool = False,
    member_username: str | None = None,
    member_password: str | None = None,
    member_totp: str | None = None,
    wechat_route_key: str | None = None,
    wechat_service_token: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    checks = [health_check()]
    owns_client = client is None
    http_client = client or httpx.Client(timeout=10)

    try:
        if web_url:
            checks.append(check_web_health(http_client, web_url))
        if exercise_member_identity:
            if not all(
                (
                    api_url,
                    web_url,
                    member_username,
                    member_password,
                    wechat_route_key,
                    wechat_service_token,
                )
            ):
                checks.append(
                    CheckResult(
                        "member-identity",
                        False,
                        "member identity smoke configuration is incomplete",
                    )
                )
            else:
                checks.append(
                    check_member_identity(
                        http_client,
                        api_url=api_url,
                        web_url=web_url,
                        username=member_username,
                        password=member_password,
                        totp_code=member_totp,
                        wechat_route_key=wechat_route_key,
                        wechat_service_token=wechat_service_token,
                    )
                )
        if api_url:
            checks.append(check_api_health(http_client, api_url))
            if any((token, user_token, viewer_token)):
                checks.append(check_image_catalog(http_client, api_url, token=token))
            if user_token:
                checks.extend(
                    [
                        check_mount_profiles(http_client, api_url, token=user_token),
                        check_user_cannot_create_for_other(
                            http_client, api_url, user_token
                        ),
                    ]
                )
            if viewer_token:
                checks.append(
                    check_viewer_cannot_create(http_client, api_url, viewer_token)
                )
            if exercise_image_pull:
                checks.append(check_image_pull(http_client, api_url, image_pull_token))
            if exercise_confirmation:
                checks.append(check_confirmation_flow(http_client, api_url, user_token))
            if exercise_governance:
                checks.extend(
                    [
                        check_gpu_recommendation(http_client, api_url, user_token),
                        check_gpu_queue(http_client, api_url, user_token),
                        check_oversized_approval(
                            http_client,
                            api_url,
                            user_token=user_token,
                            admin_token=token,
                        ),
                        check_governance_no_lifecycle_actions(http_client, api_url),
                    ]
                )
            if exercise_health:
                checks.append(check_health_diagnostics(http_client, api_url, user_token))
            if exercise_backup:
                checks.append(check_backup_operations(http_client, api_url, token))
            if exercise_print:
                checks.append(
                    check_print_operations(
                        http_client,
                        api_url,
                        user_token=user_token,
                        admin_token=token,
                        print_agent_token=print_agent_token,
                    )
                )
        if agent_url:
            checks.extend(
                [
                    check_server_agent_health(http_client, agent_url),
                    check_agent_token(http_client, agent_url, agent_token),
                    check_gpu_metrics(http_client, agent_url, agent_token),
                ]
            )
    finally:
        if owns_client:
            http_client.close()

    return summarize(checks)


def check_api_health(client: httpx.Client, api_url: str) -> CheckResult:
    try:
        response = client.get(_url(api_url, "/api/health"))
        if response.status_code == 200 and response.json().get("status") == "ok":
            return CheckResult("api-health", True, "api health ok")
        return CheckResult(
            "api-health",
            False,
            "api health failed",
            {"status_code": response.status_code, "body": _safe_json(response)},
        )
    except Exception as exc:  # pragma: no cover - exercised in integration runs.
        return CheckResult("api-health", False, str(exc))


def check_web_health(client: httpx.Client, web_url: str) -> CheckResult:
    try:
        response = client.get(_url(web_url, "/"))
        return CheckResult(
            "web-health",
            response.status_code == 200,
            "web dashboard responded" if response.status_code == 200 else "web dashboard did not respond",
            {"status_code": response.status_code},
        )
    except Exception as exc:  # pragma: no cover - exercised in integration runs.
        return CheckResult("web-health", False, str(exc))


def check_member_identity(
    client: httpx.Client,
    *,
    api_url: str,
    web_url: str,
    username: str,
    password: str,
    wechat_route_key: str,
    wechat_service_token: str,
    totp_code: str | None = None,
) -> CheckResult:
    try:
        login_payload = {"username": username, "password": password}
        if totp_code:
            login_payload["totp_code"] = totp_code
        login = client.post(
            _url(web_url, "/api/gsm/auth/login"),
            headers={"Origin": web_url.rstrip("/")},
            json=login_payload,
        )
        if login.status_code != 200:
            return CheckResult(
                "member-identity",
                False,
                "Web member login failed",
                {"status_code": login.status_code},
            )
        login_body = _safe_json(login)
        login_member = (
            login_body.get("member") if isinstance(login_body, dict) else None
        )
        login_member_id = (
            login_member.get("id") if isinstance(login_member, dict) else None
        )
        if not isinstance(login_member_id, str):
            return CheckResult(
                "member-identity",
                False,
                "Web login response did not include a member identity",
            )
        account = client.get(_url(web_url, "/api/gsm/account"))
        account_body = _safe_json(account)
        member_id = account_body.get("id") if isinstance(account_body, dict) else None
        if account.status_code != 200 or not isinstance(member_id, str):
            return CheckResult(
                "member-identity",
                False,
                "signed-in account identity was unavailable",
                {"status_code": account.status_code},
            )
        if member_id != login_member_id:
            return CheckResult(
                "member-identity",
                False,
                "signed-in account identity did not match login",
            )

        service_headers = _auth_headers(wechat_service_token)
        active_route = client.get(
            _url(api_url, f"/api/wechat/users/{quote(member_id, safe='')}/active-route"),
            headers=service_headers,
        )
        route_body = _safe_json(active_route)
        route_bound = (
            active_route.status_code == 200
            and isinstance(route_body, dict)
            and route_body.get("member_id") == member_id
            and route_body.get("route_key") == wechat_route_key
        )
        if not route_bound:
            return CheckResult(
                "member-identity",
                False,
                "WeChat route is not bound to the signed-in member",
                {"member_id": member_id, "route_bound": False},
            )
        action = client.post(
            _url(
                api_url,
                f"/api/wechat/routes/{quote(wechat_route_key, safe='')}/actions",
            ),
            headers=service_headers,
            json={"tool": "list_gpus", "arguments": {}},
        )
        ok = action.status_code == 200
        return CheckResult(
            "member-identity",
            ok,
            "Web and WeChat resolve the same live member"
            if ok
            else "WeChat action was rejected for the signed-in member",
            {"member_id": member_id, "route_bound": True},
        )
    except Exception:
        return CheckResult(
            "member-identity",
            False,
            "member identity smoke request failed",
        )


def check_server_agent_health(client: httpx.Client, agent_url: str) -> CheckResult:
    try:
        response = client.get(_url(agent_url, "/agent/health"))
        body = _safe_json(response)
        return CheckResult(
            "server-agent-health",
            response.status_code == 200 and body.get("status") == "ok",
            "server agent health ok"
            if response.status_code == 200 and body.get("status") == "ok"
            else "server agent health failed",
            {"status_code": response.status_code, "body": body},
        )
    except Exception as exc:  # pragma: no cover - exercised in integration runs.
        return CheckResult("server-agent-health", False, str(exc))


def check_image_catalog(
    client: httpx.Client,
    api_url: str,
    *,
    token: str | None = None,
) -> CheckResult:
    try:
        headers = _auth_headers(token)
        response = client.get(_url(api_url, "/api/images"), headers=headers)
        images = response.json() if response.status_code == 200 else []
        has_pytorch = any(image.get("name") == "gsm/pytorch-cuda-ssh" for image in images)
        return CheckResult(
            "image-catalog",
            response.status_code == 200 and has_pytorch,
            "pytorch gpu image is present" if has_pytorch else "pytorch gpu image missing",
            {"status_code": response.status_code},
        )
    except Exception as exc:  # pragma: no cover - exercised in integration runs.
        return CheckResult("image-catalog", False, str(exc))


def check_image_pull(
    client: httpx.Client,
    api_url: str,
    token: str,
    *,
    server_id: str = "server1",
    image: str = "pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime",
) -> CheckResult:
    try:
        response = client.post(
            _url(api_url, "/api/images/pull"),
            headers=_auth_headers(token),
            json={"server_id": server_id, "image": image},
        )
        body = _safe_json(response)
        if response.status_code != 201:
            return CheckResult(
                "image-pull",
                False,
                "image pull failed",
                {"status_code": response.status_code, "body": body},
            )

        repository, tag = _split_image_reference(image)
        catalog = client.get(_url(api_url, "/api/images")).json()
        catalog_has_image = any(
            item.get("name") == repository
            and item.get("tag", "latest") == tag
            and item.get("digest")
            for item in catalog
        )
        return CheckResult(
            "image-pull",
            catalog_has_image,
            "image pull recorded digest in catalog"
            if catalog_has_image
            else "pulled image was not recorded in catalog",
            {"status_code": response.status_code, "body": body},
        )
    except Exception as exc:  # pragma: no cover - exercised in integration runs.
        return CheckResult("image-pull", False, str(exc))


def check_mount_profiles(
    client: httpx.Client,
    api_url: str,
    *,
    token: str,
) -> CheckResult:
    try:
        response = client.get(_url(api_url, "/api/mount-profiles"), headers=_auth_headers(token))
        profiles = response.json() if response.status_code == 200 else []
        has_workspace = any(profile.get("id") == "workspace-default" for profile in profiles)
        return CheckResult(
            "mount-profiles",
            response.status_code == 200 and has_workspace,
            "workspace mount profile is present" if has_workspace else "workspace mount profile missing",
            {"status_code": response.status_code},
        )
    except Exception as exc:  # pragma: no cover - exercised in integration runs.
        return CheckResult("mount-profiles", False, str(exc))


def check_user_cannot_create_for_other(
    client: httpx.Client,
    api_url: str,
    user_token: str,
) -> CheckResult:
    response = client.post(
        _url(api_url, "/api/containers"),
        headers=_auth_headers(user_token),
        json=_container_payload(user_id="bob"),
    )
    return CheckResult(
        "rbac-user-cannot-create-for-other",
        response.status_code == 403,
        "user cannot create for another user"
        if response.status_code == 403
        else "user was allowed to create for another user",
        {"status_code": response.status_code},
    )


def check_viewer_cannot_create(
    client: httpx.Client,
    api_url: str,
    viewer_token: str,
) -> CheckResult:
    response = client.post(
        _url(api_url, "/api/containers"),
        headers=_auth_headers(viewer_token),
        json=_container_payload(),
    )
    return CheckResult(
        "rbac-viewer-cannot-create",
        response.status_code == 403,
        "viewer cannot create containers"
        if response.status_code == 403
        else "viewer was allowed to create containers",
        {"status_code": response.status_code},
    )


def check_confirmation_flow(
    client: httpx.Client,
    api_url: str,
    user_token: str,
) -> CheckResult:
    headers = _auth_headers(user_token)
    created = client.post(
        _url(api_url, "/api/containers"),
        headers=headers,
        json=_container_payload(name="gsm-smoke-confirm", preset="Debug", image="gsm/pytorch-cuda-ssh:latest", cpu_limit=4, memory_limit_gb=16),
    )
    if created.status_code != 201:
        return CheckResult(
            "confirmation-flow",
            False,
            "could not create smoke container",
            {"status_code": created.status_code, "body": _safe_json(created)},
        )
    container = created.json()["container"]
    pending = client.post(_url(api_url, f"/api/containers/{container['id']}/stop"), headers=headers)
    if pending.status_code != 202 or not pending.json().get("requires_confirmation"):
        cleanup = _confirmed_delete_container(client, api_url, container["id"], user_token)
        return CheckResult(
            "confirmation-flow",
            False,
            "stop did not request confirmation",
            {
                "status_code": pending.status_code,
                "body": _safe_json(pending),
                "cleanup_status": cleanup["status_code"],
            },
        )
    confirmation = pending.json()["confirmation"]
    confirmed = client.post(
        _url(api_url, f"/api/confirmations/{confirmation['id']}/confirm"),
        headers=headers,
        json={"phrase": confirmation["required_phrase"]},
    )
    confirmed_body = _safe_json(confirmed)
    cleanup = _confirmed_delete_container(client, api_url, container["id"], user_token)
    ok = (
        confirmed.status_code == 200
        and isinstance(confirmed_body, dict)
        and confirmed_body.get("status") == "executed"
        and cleanup["ok"]
    )
    return CheckResult(
        "confirmation-flow",
        ok,
        "dangerous action required confirmation and test container was deleted"
        if ok
        else "confirmation execution or cleanup failed",
        {
            "status_code": confirmed.status_code,
            "body": confirmed_body,
            "cleanup_status": cleanup["status_code"],
        },
    )


def check_health_diagnostics(
    client: httpx.Client,
    api_url: str,
    user_token: str,
) -> CheckResult:
    headers = _auth_headers(user_token)
    created = client.post(
        _url(api_url, "/api/containers"),
        headers=headers,
        json=_container_payload(name="gsm-test-health"),
    )
    created_body = _safe_json(created)
    container = created_body.get("container", {}) if isinstance(created_body, dict) else {}
    container_id = container.get("id") if isinstance(container, dict) else None
    if created.status_code != 201 or not container_id:
        return CheckResult(
            "health-diagnostics",
            False,
            "could not create health smoke container",
            {"create_status": created.status_code, "body": created_body},
        )

    health_status = None
    server_count = 0
    cleanup = {"ok": False, "status_code": None}
    try:
        health = client.get(
            _url(api_url, "/api/operations/health"),
            headers=headers,
        )
        health_status = health.status_code
        health_body = _safe_json(health)
        servers = health_body.get("servers", []) if isinstance(health_body, dict) else []
        server_count = len(servers) if isinstance(servers, list) else 0
    finally:
        cleanup = _confirmed_delete_container(client, api_url, container_id, user_token)

    ok = health_status == 200 and server_count > 0 and cleanup["ok"]
    return CheckResult(
        "health-diagnostics",
        ok,
        "health summary is populated and test container was deleted"
        if ok
        else "health summary or confirmed cleanup failed",
        {
            "health_status": health_status,
            "server_count": server_count,
            "cleanup_status": cleanup["status_code"],
        },
    )


def check_backup_operations(
    client: httpx.Client,
    api_url: str,
    admin_token: str | None,
) -> CheckResult:
    if not admin_token:
        return CheckResult("backup-operations", False, "admin token is required")
    headers = _auth_headers(admin_token)
    expected_members = {
        "control.sqlite",
        "config-public.json",
        "config-secrets.fernet",
    }
    try:
        status_response = client.get(
            _url(api_url, "/api/operations/backups"),
            headers=headers,
        )
        created = client.post(
            _url(api_url, "/api/operations/backups"),
            headers=headers,
            json={"kind": "manual"},
        )
        created_body = _safe_json(created)
        key = created_body.get("key") if isinstance(created_body, dict) else None
        if created.status_code != 201 or not key:
            return CheckResult(
                "backup-operations",
                False,
                "manual backup creation failed",
                {"status_code": created.status_code},
            )
        encoded_key = quote(str(key), safe="")
        verified = client.post(
            _url(api_url, f"/api/operations/backups/{encoded_key}/verify"),
            headers=headers,
        )
        manifest_response = client.get(
            _url(api_url, f"/api/operations/backups/{encoded_key}/manifest"),
            headers=headers,
        )
        drilled = client.post(
            _url(api_url, f"/api/operations/backups/{encoded_key}/drill"),
            headers=headers,
        )
        manifest = _safe_json(manifest_response)
        verified_body = _safe_json(verified)
        drilled_body = _safe_json(drilled)
        members_value = manifest.get("members", {}) if isinstance(manifest, dict) else {}
        members = set(members_value) if isinstance(members_value, dict) else set()
        serialized = str(manifest).lower()
        excluded = all(
            value not in serialized
            for value in ("telemetry.sqlite", "analytics.sqlite", "/data/users", "/scratch/users")
        )
        ok = (
            status_response.status_code == 200
            and verified.status_code == 200
            and isinstance(verified_body, dict)
            and verified_body.get("status") == "valid"
            and manifest_response.status_code == 200
            and members == expected_members
            and excluded
            and drilled.status_code == 200
            and isinstance(drilled_body, dict)
            and drilled_body.get("status") == "success"
        )
        return CheckResult(
            "backup-operations",
            ok,
            "backup create, verify, manifest scope, and drill passed"
            if ok
            else "backup verification flow failed",
            {
                "key": key,
                "members": sorted(members),
                "status_code": status_response.status_code,
            },
        )
    except Exception as exc:  # pragma: no cover - exercised in integration runs.
        return CheckResult("backup-operations", False, str(exc))


def check_print_operations(
    client: httpx.Client,
    api_url: str,
    *,
    user_token: str,
    admin_token: str | None,
    print_agent_token: str | None,
) -> CheckResult:
    if not admin_token:
        return CheckResult("print-dry-run", False, "admin token is required")
    if not print_agent_token:
        return CheckResult(
            "print-dry-run", False, "temporary Print Agent token is required"
        )

    user_headers = _auth_headers(user_token)
    agent_headers = _auth_headers(print_agent_token)
    printer_id = "gsm-test-agent-dry-run"
    pdf = _one_page_pdf()
    job_id = None
    spooling_started = False
    try:
        heartbeat = client.post(
            _url(api_url, "/api/print-agent/heartbeat"),
            headers=agent_headers,
            json={
                "version": "test-agent-dry-run",
                "printers": [
                    {
                        "id": printer_id,
                        "display_name": "Test Agent Dry Run",
                        "status": "online",
                        "capabilities": {
                            "sides": ["simplex"],
                            "color_modes": ["monochrome"],
                            "papers": ["A4"],
                            "max_copies": 1,
                        },
                    }
                ],
            },
        )
        if heartbeat.status_code != 200:
            raise ValueError("temporary Print Agent heartbeat failed")

        uploaded = client.post(
            _url(api_url, "/api/print/jobs"),
            headers=user_headers,
            files={"file": ("gsm-print-smoke.pdf", pdf, "application/pdf")},
        )
        uploaded_body = _safe_json(uploaded)
        job_id = uploaded_body.get("id") if isinstance(uploaded_body, dict) else None
        if uploaded.status_code != 201 or not job_id:
            raise ValueError("print smoke upload failed")

        job_path = f"/api/print/jobs/{quote(str(job_id), safe='')}"
        updated = client.patch(
            _url(api_url, job_path),
            headers=user_headers,
            json={
                "printer_id": printer_id,
                "copies": 1,
                "page_ranges": "",
                "sides": "simplex",
                "color_mode": "monochrome",
                "paper": "A4",
            },
        )
        updated_body = _safe_json(updated)
        phrase = updated_body.get("required_phrase") if isinstance(updated_body, dict) else None
        if updated.status_code != 200 or not phrase:
            raise ValueError("print smoke settings update failed")

        confirmed = client.post(
            _url(api_url, job_path + "/confirm"),
            headers=user_headers,
            json={"phrase": phrase},
        )
        confirmed_body = _safe_json(confirmed)
        if (
            confirmed.status_code != 200
            or not isinstance(confirmed_body, dict)
            or confirmed_body.get("status") != "queued"
        ):
            raise ValueError("print smoke confirmation failed")

        claimed = client.post(
            _url(api_url, "/api/print-agent/jobs/claim"),
            headers=agent_headers,
            json={},
        )
        claimed_body = _safe_json(claimed)
        claimed_job = claimed_body.get("job", {}) if isinstance(claimed_body, dict) else {}
        lease_id = claimed_body.get("lease_id") if isinstance(claimed_body, dict) else None
        artifact_token = claimed_body.get("artifact_token") if isinstance(claimed_body, dict) else None
        if (
            claimed.status_code != 200
            or claimed_job.get("id") != job_id
            or not lease_id
            or not artifact_token
        ):
            raise ValueError("print smoke claim failed or claimed another job")

        agent_job_path = f"/api/print-agent/jobs/{quote(str(job_id), safe='')}"
        artifact = client.get(
            _url(api_url, agent_job_path + "/artifact"),
            headers=agent_headers,
            params={"lease_id": lease_id, "token": artifact_token},
        )
        digest = hashlib.sha256(artifact.content).hexdigest()
        if (
            artifact.status_code != 200
            or artifact.content != pdf
            or digest != claimed_job.get("sha256")
            or digest != artifact.headers.get("x-content-sha256")
            or len(artifact.content) != claimed_job.get("plaintext_size_bytes")
        ):
            raise ValueError("print smoke artifact validation failed")

        event_url = _url(api_url, agent_job_path + "/events")
        for sequence, event_type, windows_job_id in (
            (1, "spooling", None),
            (2, "submitted", f"dry-run-{job_id}"),
            (3, "completed", None),
        ):
            payload = {
                "lease_id": lease_id,
                "sequence": sequence,
                "type": event_type,
            }
            if windows_job_id:
                payload["windows_job_id"] = windows_job_id
            response = client.post(event_url, headers=agent_headers, json=payload)
            if response.status_code != 200:
                raise ValueError(f"print smoke {event_type} event failed")
            if event_type == "spooling":
                spooling_started = True

        public_response = client.get(_url(api_url, job_path), headers=user_headers)
        public_job = _safe_json(public_response)
        audit_response = client.get(
            _url(api_url, "/api/audit-logs"),
            headers=_auth_headers(admin_token),
        )
        audit_items = _safe_json(audit_response)
        matching_audit = [
            item
            for item in audit_items
            if isinstance(item, dict) and item.get("target_id") == job_id
        ] if isinstance(audit_items, list) else []
        serialized = repr({"job": public_job, "audit": matching_audit})
        redacted = all(
            value not in serialized
            for value in (
                "blob_key",
                "confirmation_hash",
                "lease_id_hash",
                "artifact_token_hash",
                "local_path",
                print_agent_token,
            )
        )
        ok = (
            public_response.status_code == 200
            and isinstance(public_job, dict)
            and public_job.get("status") == "completed"
            and audit_response.status_code == 200
            and bool(matching_audit)
            and redacted
        )
        return CheckResult(
            "print-dry-run",
            ok,
            "one-page print protocol dry-run completed without a renderer"
            if ok
            else "print final state or redaction check failed",
            {
                "job_id": job_id,
                "status": public_job.get("status") if isinstance(public_job, dict) else None,
                "submission_count": 1,
                "artifact_bytes": len(pdf),
            },
        )
    except Exception as exc:
        return CheckResult(
            "print-dry-run",
            False,
            "print protocol dry-run failed",
            {
                "job_id": job_id,
                "spooling_started": spooling_started,
                "error_type": type(exc).__name__,
            },
        )
    finally:
        if job_id and not spooling_started:
            try:
                client.post(
                    _url(api_url, f"/api/print/jobs/{quote(str(job_id), safe='')}/cancel"),
                    headers=user_headers,
                )
            except Exception:
                pass
        try:
            client.post(
                _url(api_url, "/api/print-agent/heartbeat"),
                headers=agent_headers,
                json={"version": "test-agent-dry-run", "printers": []},
            )
        except Exception:
            pass


def _confirmed_delete_container(
    client: httpx.Client,
    api_url: str,
    container_id: str,
    user_token: str,
) -> dict[str, Any]:
    headers = _auth_headers(user_token)
    pending = client.delete(
        _url(api_url, f"/api/containers/{container_id}"),
        headers=headers,
    )
    pending_body = _safe_json(pending)
    confirmation = pending_body.get("confirmation", {}) if isinstance(pending_body, dict) else {}
    confirmation_id = confirmation.get("id") if isinstance(confirmation, dict) else None
    phrase = confirmation.get("required_phrase") if isinstance(confirmation, dict) else None
    if pending.status_code != 202 or not confirmation_id or not phrase:
        return {"ok": False, "status_code": pending.status_code}
    confirmed = client.post(
        _url(api_url, f"/api/confirmations/{confirmation_id}/confirm"),
        headers=headers,
        json={"phrase": phrase},
    )
    confirmed_body = _safe_json(confirmed)
    return {
        "ok": confirmed.status_code == 200
        and isinstance(confirmed_body, dict)
        and confirmed_body.get("status") == "executed",
        "status_code": confirmed.status_code,
    }


def check_gpu_recommendation(
    client: httpx.Client,
    api_url: str,
    user_token: str,
) -> CheckResult:
    try:
        response = client.post(
            _url(api_url, "/api/governance/recommendations"),
            headers=_auth_headers(user_token),
            json={
                "gpu_count": 1,
                "memory_tier": "lt_8",
                "estimated_duration_minutes": 30,
            },
        )
        body = _safe_json(response)
        recommendations = body.get("recommendations") if isinstance(body, dict) else None
        required_fields = {
            "server_id",
            "gpu_uuid",
            "index",
            "state",
            "safe_to_share",
            "reason_codes",
        }
        valid_shape = isinstance(recommendations, list) and all(
            isinstance(item, dict) and required_fields <= item.keys()
            for item in recommendations
        )
        ok = response.status_code == 200 and valid_shape
        return CheckResult(
            "gpu-recommendation",
            ok,
            "GPU recommendation response is valid" if ok else "GPU recommendation response is invalid",
            {"status_code": response.status_code, "count": len(recommendations or [])},
        )
    except Exception as exc:  # pragma: no cover - exercised in integration runs.
        return CheckResult("gpu-recommendation", False, str(exc))


def check_gpu_queue(
    client: httpx.Client,
    api_url: str,
    user_token: str,
) -> CheckResult:
    request_id = None
    try:
        headers = _auth_headers(user_token)
        created = client.post(
            _url(api_url, "/api/governance/queue"),
            headers=headers,
            json={
                "gpu_count": 1,
                "memory_tier": "lt_8",
                "estimated_duration_minutes": 30,
                "compatible_server_ids": ["server1"],
                "workload_profile": "standard",
            },
        )
        created_body = _safe_json(created)
        if isinstance(created_body, dict):
            request_id = created_body.get("id")
        listed = client.get(_url(api_url, "/api/governance/queue"), headers=headers)
        listed_body = _safe_json(listed)
        listed_ids = {
            item.get("id")
            for item in listed_body
            if isinstance(item, dict)
        } if isinstance(listed_body, list) else set()
        withdrawn = (
            client.delete(
                _url(api_url, f"/api/governance/queue/{request_id}"),
                headers=headers,
            )
            if request_id
            else None
        )
        withdrawn_body = _safe_json(withdrawn) if withdrawn is not None else {}
        ok = (
            created.status_code == 201
            and request_id in listed_ids
            and listed.status_code == 200
            and withdrawn is not None
            and withdrawn.status_code == 200
            and withdrawn_body.get("status") == "withdrawn"
        )
        return CheckResult(
            "gpu-queue",
            ok,
            "advisory GPU queue create/list/withdraw passed"
            if ok
            else "advisory GPU queue flow failed",
            {
                "create_status": created.status_code,
                "list_status": listed.status_code,
                "withdraw_status": withdrawn.status_code if withdrawn is not None else None,
            },
        )
    except Exception as exc:  # pragma: no cover - exercised in integration runs.
        return CheckResult("gpu-queue", False, str(exc), {"request_id": request_id})


def check_oversized_approval(
    client: httpx.Client,
    api_url: str,
    *,
    user_token: str,
    admin_token: str | None,
) -> CheckResult:
    if not admin_token:
        return CheckResult("oversized-approval", False, "admin token is required")
    try:
        created = client.post(
            _url(api_url, "/api/containers"),
            headers=_auth_headers(user_token),
            json=_container_payload(
                name="gsm-smoke-approval",
                memory_limit_gb=64,
            ),
        )
        created_body = _safe_json(created)
        approval = created_body.get("approval", {}) if isinstance(created_body, dict) else {}
        approval_id = approval.get("id") if isinstance(approval, dict) else None
        deferred = (
            created.status_code == 202
            and created_body.get("requires_approval") is True
            and "container" not in created_body
            and bool(approval_id)
        )
        decision = (
            client.post(
                _url(api_url, f"/api/governance/approvals/{approval_id}/decision"),
                headers=_auth_headers(admin_token),
                json={"decision": "rejected", "reason": "Test Agent smoke cleanup"},
            )
            if approval_id
            else None
        )
        decision_body = _safe_json(decision) if decision is not None else {}
        ok = (
            deferred
            and decision is not None
            and decision.status_code == 200
            and decision_body.get("status") == "rejected"
        )
        return CheckResult(
            "oversized-approval",
            ok,
            "oversized create was deferred and cleaned up by rejection"
            if ok
            else "oversized create approval flow failed",
            {
                "create_status": created.status_code,
                "decision_status": decision.status_code if decision is not None else None,
            },
        )
    except Exception as exc:  # pragma: no cover - exercised in integration runs.
        return CheckResult("oversized-approval", False, str(exc))


def check_governance_no_lifecycle_actions(
    client: httpx.Client,
    api_url: str,
) -> CheckResult:
    try:
        response = client.get(_url(api_url, "/openapi.json"))
        body = _safe_json(response)
        paths = body.get("paths", {}) if isinstance(body, dict) else {}
        governance_operations = []
        forbidden = []
        for path, methods in paths.items():
            if not (
                path.startswith("/api/governance/queue")
                or path.startswith("/api/governance/idle-events")
            ):
                continue
            for method, operation in methods.items():
                operation_id = operation.get("operationId", "") if isinstance(operation, dict) else ""
                action = f"{method} {path} {operation_id}".lower()
                governance_operations.append(action)
                if any(term in action for term in ("stop_container", "delete_container", "/stop")):
                    forbidden.append(action)
        ok = response.status_code == 200 and bool(governance_operations) and not forbidden
        return CheckResult(
            "governance-no-lifecycle-actions",
            ok,
            "queue and idle routes expose no container lifecycle actions"
            if ok
            else "governance routes expose an unsafe lifecycle action",
            {"operations": governance_operations, "forbidden": forbidden},
        )
    except Exception as exc:  # pragma: no cover - exercised in integration runs.
        return CheckResult("governance-no-lifecycle-actions", False, str(exc))


def check_agent_token(
    client: httpx.Client,
    agent_url: str,
    agent_token: str,
) -> CheckResult:
    response = client.get(
        _url(agent_url, "/agent/gpus"),
        headers=_auth_headers(agent_token),
    )
    return CheckResult(
        "agent-token",
        response.status_code == 200,
        "agent token accepted" if response.status_code == 200 else "agent token rejected",
        {"status_code": response.status_code},
    )


def check_gpu_metrics(
    client: httpx.Client,
    agent_url: str,
    agent_token: str,
) -> CheckResult:
    response = client.get(
        _url(agent_url, "/agent/gpus"),
        headers=_auth_headers(agent_token),
    )
    body = _safe_json(response)
    gpu_count = len(body) if isinstance(body, list) else 0
    if response.status_code != 200:
        return CheckResult(
            "gpu-metrics",
            False,
            "GPU metrics endpoint failed",
            {"status_code": response.status_code, "body": body},
        )
    message = (
        f"GPU metrics endpoint reported {gpu_count} GPU(s)"
        if gpu_count
        else "GPU metrics endpoint reachable; no GPU reported"
    )
    return CheckResult(
        "gpu-metrics",
        True,
        message,
        {"status_code": response.status_code, "gpu_count": gpu_count},
    )


def _container_payload(
    *,
    user_id: str | None = None,
    name: str = "gsm-smoke-rbac",
    image: str = "gsm/coding-cpu-ssh:latest",
    preset: str = "Coding",
    cpu_limit: int = 2,
    memory_limit_gb: int = 8,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "server_id": "server1",
        "image": image,
        "preset": preset,
        "cpu_limit": cpu_limit,
        "memory_limit_gb": memory_limit_gb,
        "gpu_devices": [],
        "mount_profile_ids": ["workspace-default"],
    }
    if user_id is not None:
        payload["user_id"] = user_id
    return payload


def _one_page_pdf() -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << >> /Contents 4 0 R >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, value in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii"))
        output.extend(value)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


def _auth_headers(token: str | None) -> dict[str, str]:
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def _url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}"


def _split_image_reference(image: str) -> tuple[str, str]:
    if ":" in image.rsplit("/", 1)[-1]:
        repository, tag = image.rsplit(":", 1)
        return repository, tag
    return image, "latest"


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text
