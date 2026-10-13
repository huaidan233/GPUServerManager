"""Per-member ServerChan delivery; credentials never leave the server in reads."""
from datetime import datetime, timezone
import hashlib
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import InvalidToken

from gsm_api import typed_records
from gsm_api.container_credentials import encrypt_password, decrypt_password
from gsm_api.identity_store import default_identity_store

BINDING = "serverchan_binding"
ATTEMPT = "serverchan_attempt"


def resolve_member(user_id):
    store = default_identity_store()
    return store.get_member(user_id) or store.get_member_by_username(user_id)


def binding_summary(member_id):
    value = typed_records.get(BINDING, member_id) or {}
    return {"configured": bool(value.get("encrypted_key")), "enabled": bool(value.get("enabled")),
            "daily_limit": value.get("daily_limit", 5), "key_hint": value.get("key_hint", "")}


def save_binding(member_id, *, sendkey, enabled, daily_limit):
    record = typed_records.get(BINDING, member_id) or {}
    if sendkey:
        record.update(encrypted_key=encrypt_password(sendkey), key_hint="SCT…" + sendkey[-4:])
    if enabled and not record.get("encrypted_key"):
        raise ValueError("请先填写 Server酱 SendKey。")
    record.update(enabled=enabled, daily_limit=daily_limit)
    typed_records.save(BINDING, member_id, record)
    return binding_summary(member_id)


class MemberNotificationDispatcher:
    def __init__(self, fallback=None, client=None):
        self.fallback = fallback
        self.client = client or httpx.Client(timeout=10, trust_env=False, follow_redirects=False)
        self.owns_client = client is None

    def close(self):
        if self.owns_client:
            self.client.close()
        if self.fallback:
            self.fallback.close()

    def send(self, *, user_id, kind, payload, idempotency_key):
        member = resolve_member(user_id)
        resolved = member["id"] if member else user_id
        if member and member["status"] not in {"active", "pending_exit"}:
            return "cancelled"
        binding = typed_records.get(BINDING, resolved) or {}
        if binding.get("enabled") and binding.get("encrypted_key"):
            return self.send_serverchan(resolved, binding, kind, payload, idempotency_key)
        if self.fallback:
            return self.fallback.send(user_id=resolved, kind=kind, payload=payload, idempotency_key=idempotency_key)
        # Every notification remains readable in the personal notification inbox.
        return "sent"

    def send_serverchan(self, member_id, binding, kind, payload, event_id):
        from gsm_api.personal_quota import allocation_boundary
        from gsm_api.notification import _notification_text
        day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        record_id = hashlib.sha256(f"{member_id}:{event_id}".encode()).hexdigest()
        with allocation_boundary():
            previous = typed_records.get(ATTEMPT, record_id)
            if previous and previous["status"] in {"sent", "sending", "unknown"}:
                return "sent" if previous["status"] == "sent" else "unknown"
            daily = [r for r in typed_records.list_kind(ATTEMPT) if r["member_id"] == member_id and r["day"] == day]
            if sum(r.get("attempts", 1) for r in daily) >= binding.get("daily_limit", 5):
                return "failed"
            typed_records.save(ATTEMPT, record_id, {"id": record_id, "member_id": member_id, "day": day,
                "status": "sending", "attempts": (previous.get("attempts", 0) if previous and previous["day"] == day else 0) + 1})
        text = _notification_text(kind, payload)
        try:
            key = decrypt_password(binding["encrypted_key"])
            response = self.client.post(f"https://sctapi.ftqq.com/{key}.send",
                data={"title": text.splitlines()[0][:80], "desp": text, "noip": "1"})
            body = response.json()
            outcome = "sent" if response.is_success and isinstance(body, dict) and body.get("code") == 0 else "failed"
        except InvalidToken:
            outcome = "failed"
        except (httpx.HTTPError, ValueError):
            outcome = "unknown"
        typed_records.transition(ATTEMPT, record_id, "sending", {"status": outcome, "updated_at": datetime.now(timezone.utc).isoformat()})
        return outcome
