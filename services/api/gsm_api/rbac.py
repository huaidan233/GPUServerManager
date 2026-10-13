from gsm_api.domain import Role
from gsm_api.member_domain import MemberStatus, authorize_capability


def can_manage_container(
    *,
    actor_role: Role,
    actor_user_id: str,
    owner_user_id: str,
    actor_status: MemberStatus = MemberStatus.ACTIVE,
) -> bool:
    if not authorize_capability(actor_status, actor_role, "container.stop"):
        return False
    if authorize_capability(
        actor_status,
        actor_role,
        "infrastructure.manage",
    ):
        return True
    return actor_user_id == owner_user_id


def can_use_admin_action(
    actor_role: Role,
    *,
    actor_status: MemberStatus = MemberStatus.ACTIVE,
) -> bool:
    return authorize_capability(
        actor_status,
        actor_role,
        "infrastructure.manage",
    )
