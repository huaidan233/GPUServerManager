from gsm_api.domain import Role
from gsm_api.member_domain import MemberStatus
from gsm_api.rbac import can_manage_container, can_use_admin_action


def test_user_can_manage_own_container():
    assert can_manage_container(
        actor_role=Role.USER,
        actor_user_id="alice",
        owner_user_id="alice",
    )


def test_user_cannot_manage_other_container():
    assert not can_manage_container(
        actor_role=Role.USER,
        actor_user_id="alice",
        owner_user_id="bob",
    )


def test_admin_can_manage_other_container():
    assert can_manage_container(
        actor_role=Role.ADMIN,
        actor_user_id="root",
        owner_user_id="bob",
    )


def test_only_admin_can_use_admin_action():
    assert can_use_admin_action(Role.ADMIN)
    assert not can_use_admin_action(Role.POWER_USER)
    assert not can_use_admin_action(Role.USER)
    assert not can_use_admin_action(Role.VIEWER)


def test_pending_exit_status_removes_admin_actions_and_viewer_management():
    assert not can_use_admin_action(
        Role.ADMIN,
        actor_status=MemberStatus.PENDING_EXIT,
    )
    assert not can_manage_container(
        actor_role=Role.VIEWER,
        actor_status=MemberStatus.PENDING_EXIT,
        actor_user_id="alice",
        owner_user_id="alice",
    )
