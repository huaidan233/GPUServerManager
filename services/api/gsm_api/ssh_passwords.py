"""Revoke container passwords when member access is withdrawn."""
import logging
from gsm_api.domain import Role
from gsm_api.member_domain import MemberStatus, authorize_capability
from gsm_api.store import container_ownership_boundary, list_container_records, update_container_record
from gsm_api.container_credentials import forget_container_password

logger = logging.getLogger(__name__)


def revoke_ineligible_passwords(store, agent_client) -> None:
    with container_ownership_boundary():
        for container in list_container_records():
            metadata = dict(container.get("metadata") or {})
            if not metadata.get("ssh_password_enabled"):
                continue
            member = store.get_member(container.get("owner_member_id", ""))
            if member and authorize_capability(MemberStatus(member["status"]), Role(member["role"]), "ssh.issue"):
                continue
            forget_container_password(container["id"])
            try:
                agent_client.container_management(server_id=container["server_id"],
                    agent_container_id=metadata["agent_container_id"], operation="ssh-password",
                    method="POST", payload={"password": None})
                metadata["ssh_password_enabled"] = False
                update_container_record(container["id"], {"metadata": metadata})
            except Exception:
                logger.exception("SSH password revocation will retry for container %s", container["id"])
