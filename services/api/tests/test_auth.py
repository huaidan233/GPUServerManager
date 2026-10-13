import pytest
from fastapi import HTTPException

from gsm_api.auth import Actor, admin_user_ids, resolve_bearer_actor
from gsm_api.domain import Role
from gsm_api.identity_store import IdentityStore
from gsm_api.member_domain import MemberStatus
from gsm_api.store import reset_store, save_wechat_binding


def test_development_tokens_are_available_by_default(monkeypatch):
    monkeypatch.delenv("GSM_ENV", raising=False)

    actor = resolve_bearer_actor("Bearer admin-token")

    assert actor.user_id == "admin"
    assert actor.role == Role.ADMIN


def test_production_rejects_builtin_development_tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.setenv("GSM_ADMIN_TOKEN", "prod-admin-token")
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")

    with pytest.raises(HTTPException) as exc:
        resolve_bearer_actor("Bearer admin-token", store=store)

    assert exc.value.status_code == 401
    store.close()


@pytest.mark.parametrize(
    ("environment_name", "token"),
    [
        ("GSM_ADMIN_TOKEN", "prod-admin-token"),
        ("GSM_USER_ALICE_TOKEN", "prod-alice-token"),
        ("GSM_CODEX_USER_TOKEN", "prod-codex-token"),
        ("GSM_WEB_API_TOKEN", "prod-web-token"),
    ],
)
def test_production_rejects_environment_human_tokens(
    monkeypatch,
    tmp_path,
    environment_name,
    token,
):
    monkeypatch.setenv("GSM_ENV", "production")
    monkeypatch.setenv(environment_name, token)
    store = IdentityStore(f"sqlite:///{tmp_path / 'identity.sqlite'}")

    with pytest.raises(HTTPException) as exc:
        resolve_bearer_actor(f"Bearer {token}", store=store)

    assert exc.value.status_code == 401
    store.close()


def test_admin_recipients_ignore_legacy_binding_role_snapshots(monkeypatch):
    monkeypatch.delenv("GSM_ENV", raising=False)
    reset_store()
    save_wechat_binding(
        {"route_key": "admin-route", "user_id": "lab-admin", "role": "admin", "status": "active"}
    )
    save_wechat_binding(
        {"route_key": "inactive-route", "user_id": "old-admin", "role": "admin", "status": "inactive"}
    )
    save_wechat_binding(
        {"route_key": "user-route", "user_id": "alice", "role": "user", "status": "active"}
    )

    assert admin_user_ids() == ["admin"]


def test_pending_exit_admin_is_not_an_effective_admin_recipient(monkeypatch):
    monkeypatch.setattr(
        "gsm_api.auth.configured_actors",
        lambda: [
            Actor(
                user_id="leaving-admin",
                role=Role.ADMIN,
                member_id="member-leaving-admin",
                status=MemberStatus.PENDING_EXIT,
            )
        ],
    )
    monkeypatch.setattr("gsm_api.store.list_wechat_bindings", lambda: [])

    assert admin_user_ids() == []

