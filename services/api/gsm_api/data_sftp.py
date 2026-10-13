"""Provision isolated SFTPGo accounts against the same personal workspace."""
import hashlib
import os
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from gsm_api.auth import Actor, get_actor, require_capability
from gsm_api.domain import Role
from gsm_api.member_domain import MemberStatus, authorize_capability
from gsm_api.container_credentials import encrypt_password, decrypt_password
from gsm_api.personal_quota import allocation_boundary
from gsm_api import typed_records

router = APIRouter(prefix="/api/data-sftp", tags=["personal-data"])
ACCOUNT = "personal_sftp_account"
WRITE_PERMISSIONS = ["list", "download", "upload", "overwrite", "create_dirs", "rename", "delete"]
READ_PERMISSIONS = ["list", "download"]


class Access(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: SecretStr | None = None
    public_keys: list[str] = Field(default_factory=list, max_length=8)


def call(method, path, payload=None):
    url, key = os.getenv("GSM_SFTPGO_URL"), os.getenv("GSM_SFTPGO_API_KEY")
    if not url or not key:
        raise HTTPException(503, "独立 SFTP 服务尚未部署，请先使用网页上传。")
    try:
        with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
            response = client.request(method, url.rstrip("/") + "/api/v2" + path,
                                      headers={"X-SFTPGO-API-KEY": key}, json=payload)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json() if response.content else {}
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "SFTP 服务连接失败，请管理员检查服务状态。") from None


def account_name(member_id):
    return "gsm-" + hashlib.sha256(member_id.encode()).hexdigest()[:20]


@router.get("")
def info(actor: Actor = Depends(get_actor)):
    require_capability(actor, "container.read")
    record = typed_records.get(ACCOUNT, actor.member_id or actor.user_id) or {}
    return {"configured": bool(os.getenv("GSM_SFTPGO_URL")), "username": record.get("username"),
            "password": decrypt_password(record["password"]) if record.get("password") else None,
            "public_keys": record.get("public_keys", []), "host": os.getenv("GSM_PUBLIC_SSH_HOST", ""),
            "port": int(os.getenv("GSM_DATA_SFTP_PORT", "2222")), "container_path": "/workspace"}


@router.put("")
def save(payload: Access, actor: Actor = Depends(get_actor)):
    require_capability(actor, "container.create")
    from gsm_api.data_routes import agent_call
    import re
    password = payload.password.get_secret_value() if payload.password else None
    if password is not None and (not 12 <= len(password) <= 128 or any(c in password for c in "\0\r\n")):
        raise HTTPException(422, "SFTP 密码须为 12–128 位，不能包含换行。")
    if any(len(key) > 16000 or not re.fullmatch(r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/=]+(?: [^\r\n]*)?", key) for key in payload.public_keys):
        raise HTTPException(422, "SSH 公钥格式无效。")
    # Creates the workspace on the guarded data mount, with the image's UID/GID.
    agent_call(actor, "capacity", {"path": ""})
    key = actor.member_id or actor.user_id
    username = account_name(key)
    with allocation_boundary():
        record = typed_records.get(ACCOUNT, key) or {}
        if not password and not record.get("password") and not payload.public_keys:
            raise HTTPException(422, "请设置密码或 SSH 公钥。")
        existing = call("GET", "/users/" + quote(username, safe=""))
        root = os.environ.get("GSM_SFTPGO_USERS_ROOT", "/srv/gsm-users")
        value = {"username": username, "status": 1, "home_dir": root.rstrip("/") + "/" + actor.user_id,
                 "permissions": {"/": WRITE_PERMISSIONS},
                 "uid": int(os.getenv("GSM_DATA_UID", "10000")), "gid": int(os.getenv("GSM_DATA_GID", "10000")),
                 "public_keys": payload.public_keys, "max_sessions": 5,
                 "filters": {"denied_protocols": ["FTP", "HTTP", "DAV"], "denied_login_methods": []}}
        if password:
            value["password"] = password
        elif record.get("password"):
            value["password"] = decrypt_password(record["password"])
        if existing:
            # Keep the storage provider local and forbid virtual folders from an old config.
            value.update(id=existing["id"], filesystem={"provider": 0}, virtual_folders=[])
        call("PUT" if existing else "POST", "/users/" + quote(username, safe="") if existing else "/users", value)
        typed_records.save(ACCOUNT, key, {"id": key, "username": username, "owner_member_id": actor.member_id,
            "password": encrypt_password(password) if password else record.get("password"), "public_keys": payload.public_keys})
    return info(actor)


def reconcile_access(identity_store):
    if not os.getenv("GSM_SFTPGO_URL"):
        return
    for account in typed_records.list_kind(ACCOUNT):
        member = identity_store.get_member(account.get("owner_member_id"))
        try:
            member_status, role = MemberStatus(member["status"]), Role(member["role"])
            can_read = authorize_capability(member_status, role, "container.read")
            can_write = authorize_capability(member_status, role, "container.create")
        except (TypeError, KeyError, ValueError):
            can_read = can_write = False
        desired_status = int(can_read)
        desired_permissions = {"/": WRITE_PERMISSIONS if can_write else READ_PERMISSIONS}
        existing = call("GET", "/users/" + quote(account["username"], safe=""))
        if not existing:
            continue
        changed = existing.get("status") != desired_status or existing.get("permissions") != desired_permissions
        if changed:
            call("PUT", "/users/" + quote(account["username"], safe=""),
                 {**existing, "status": desired_status, "permissions": desired_permissions})
        # SFTP sessions retain the permissions captured at login. Disconnect
        # after a policy change so a pending-exit member cannot keep writing.
        if not changed and desired_status:
            continue
        connections = call("GET", "/connections") or []
        for connection in connections:
            if connection.get("username") == account["username"]:
                call("DELETE", "/connections/" + quote(connection["connection_id"], safe=""))
