"""Private-chat iLink transport; business authorization remains in Central API.

Protocol reference: https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol_zh_CN.md
No Codex/OpenClaw execution layer is used here.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Any, Iterator
from urllib.parse import urlsplit

import httpx

DEFAULT_API_URL = "https://ilinkai.weixin.qq.com"
PROTOCOL_VERSION = "2.4.9"
BASE_INFO = {"channel_version": PROTOCOL_VERSION, "bot_agent": "GPUServerManager/0.3.0-rc.5"}


class IlinkError(RuntimeError):
    """A sanitized protocol error, never including response bodies or credentials."""

    def __init__(self, code: int | str):
        self.code = code
        super().__init__(f"WeChat iLink error: {code}")


def api_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise IlinkError("untrusted_api_origin") from exc
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or not host.endswith(".weixin.qq.com")
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise IlinkError("untrusted_api_origin")
    return f"https://{host}"


def state_directory() -> Path:
    default = (
        "/var/lib/gpu-server-manager/wechat-ilink"
        if os.environ.get("GSM_ENV") == "production"
        else "var/wechat-ilink"
    )
    return Path(os.environ.get("GSM_WECHAT_ILINK_STATE_DIR", default))


@dataclass(frozen=True)
class IlinkAccount:
    account_id: str
    user_id: str
    token: str = field(repr=False)
    base_url: str = DEFAULT_API_URL

    def __post_init__(self):
        for value, maximum in (
            (self.account_id, 128),
            (self.user_id, 256),
            (self.token, 4096),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise IlinkError("invalid_account")
        api_base_url(self.base_url)


class IlinkStore:
    """Shared by the receiver and API process; contains sensitive local credentials.

    Claims are committed BEFORE dispatch. A crash or lost response never causes
    automatic replay of a business action. Such messages need user reconciliation.
    """

    def __init__(self, directory: Path):
        if directory.is_symlink():
            raise IlinkError("invalid_state_path")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        self.path = directory / "ilink.sqlite3"
        if self.path.is_symlink():
            raise IlinkError("invalid_state_path")
        descriptor = os.open(
            self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        os.close(descriptor)
        os.chmod(self.path, 0o600)
        with self.connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    token TEXT NOT NULL, base_url TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cursors (
                    account_id TEXT PRIMARY KEY REFERENCES accounts ON DELETE CASCADE,
                    cursor TEXT NOT NULL DEFAULT '', last_poll REAL, status TEXT NOT NULL DEFAULT 'pending');
                CREATE TABLE IF NOT EXISTS routes (
                    route_key TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts ON DELETE CASCADE,
                    user_id TEXT NOT NULL, context_token TEXT NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS inbound (
                    account_id TEXT NOT NULL REFERENCES accounts ON DELETE CASCADE,
                    message_id TEXT NOT NULL, claimed_at REAL NOT NULL,
                    PRIMARY KEY (account_id, message_id));
                CREATE TABLE IF NOT EXISTS deliveries (
                    account_id TEXT NOT NULL REFERENCES accounts ON DELETE CASCADE,
                    delivery_key TEXT NOT NULL, payload_hash TEXT NOT NULL, status TEXT NOT NULL,
                    PRIMARY KEY (account_id, delivery_key));
                CREATE TABLE IF NOT EXISTS leases (
                    account_id TEXT PRIMARY KEY REFERENCES accounts ON DELETE CASCADE,
                    owner TEXT NOT NULL, expires_at REAL NOT NULL);
            """
            )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def save_account(self, account: IlinkAccount) -> None:
        with self.connection() as db:
            # Replacement invalidates reply contexts, but retains action deduplication.
            db.execute("DELETE FROM routes WHERE account_id = ?", (account.account_id,))
            db.execute(
                "INSERT INTO accounts VALUES (?, ?, ?, ?) ON CONFLICT(account_id) DO UPDATE SET user_id=excluded.user_id, token=excluded.token, base_url=excluded.base_url",
                (account.account_id, account.user_id, account.token, account.base_url),
            )
            db.execute(
                "INSERT INTO cursors(account_id) VALUES (?) ON CONFLICT(account_id) DO UPDATE SET cursor='', last_poll=NULL, status='pending'",
                (account.account_id,),
            )

    def accounts(self) -> list[IlinkAccount]:
        with self.connection() as db:
            return [
                IlinkAccount(**dict(row))
                for row in db.execute("SELECT * FROM accounts ORDER BY account_id")
            ]

    def account(self, account_id: str) -> IlinkAccount | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM accounts WHERE account_id=?", (account_id,)
            ).fetchone()
            return IlinkAccount(**dict(row)) if row else None

    def remove_account(self, account_id: str) -> None:
        with self.connection() as db:
            db.execute("DELETE FROM accounts WHERE account_id=?", (account_id,))

    def cursor(self, account_id: str) -> str:
        with self.connection() as db:
            row = db.execute(
                "SELECT cursor FROM cursors WHERE account_id=?", (account_id,)
            ).fetchone()
            return row[0] if row else ""

    def poll_result(
        self, account_id: str, *, cursor: str | None = None, status: str = "ok"
    ) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE cursors SET cursor=COALESCE(?, cursor), last_poll=?, status=? WHERE account_id=?",
                (cursor, time.time(), status, account_id),
            )

    def health(self) -> dict[str, Any]:
        with self.connection() as db:
            rows = db.execute("SELECT last_poll, status FROM cursors").fetchall()
        if not rows:
            return {"status": "degraded", "warnings": ["wechat_ilink_login_required"]}
        if any(row["status"] == "login_required" for row in rows):
            return {"status": "degraded", "warnings": ["wechat_ilink_login_required"]}
        if any(
            row["last_poll"] is None
            or time.time() - row["last_poll"] > 120
            or row["status"] != "ok"
            for row in rows
        ):
            return {
                "status": "degraded",
                "warnings": ["wechat_ilink_receiver_unavailable"],
            }
        return {"status": "ok", "warnings": []}

    def claim_message(self, account_id: str, message_id: str) -> bool:
        with self.connection() as db:
            return (
                db.execute(
                    "INSERT OR IGNORE INTO inbound VALUES (?, ?, ?)",
                    (account_id, message_id, time.time()),
                ).rowcount
                == 1
            )

    def save_route(
        self, route: str, account_id: str, user_id: str, context_token: str
    ) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO routes VALUES (?, ?, ?, ?, ?) ON CONFLICT(route_key) DO UPDATE SET context_token=excluded.context_token, updated_at=excluded.updated_at",
                (route, account_id, user_id, context_token, time.time()),
            )

    def destination(self, route: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM routes WHERE route_key=?", (route,)
            ).fetchone()
            return dict(row) if row else None

    def claim_delivery(self, account_id: str, key: str, payload_hash: str) -> str:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM deliveries WHERE account_id=? AND delivery_key=?",
                (account_id, key),
            ).fetchone()
            if row:
                if row["payload_hash"] != payload_hash:
                    raise IlinkError("idempotency_key_reused")
                if row["status"] != "failed":
                    return row["status"]
                db.execute(
                    "UPDATE deliveries SET status='pending' WHERE account_id=? AND delivery_key=?",
                    (account_id, key),
                )
            else:
                db.execute(
                    "INSERT INTO deliveries VALUES (?, ?, ?, 'pending')",
                    (account_id, key, payload_hash),
                )
            return "new"

    def delivery_result(self, account_id: str, key: str, status: str) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE deliveries SET status=? WHERE account_id=? AND delivery_key=?",
                (status, account_id, key),
            )

    def lease(self, account_id: str, owner: str) -> bool:
        with self.connection() as db:
            return (
                db.execute(
                    "INSERT INTO leases VALUES (?, ?, ?) ON CONFLICT(account_id) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at WHERE leases.owner=excluded.owner OR leases.expires_at < ?",
                    (account_id, owner, time.time() + 180, time.time()),
                ).rowcount
                == 1
            )

    def release(self, account_id: str, owner: str) -> None:
        with self.connection() as db:
            db.execute(
                "DELETE FROM leases WHERE account_id=? AND owner=?", (account_id, owner)
            )


class IlinkAPI:
    def __init__(self, http_client: httpx.Client | None = None):
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=15, trust_env=False, follow_redirects=False
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        base_url: str = DEFAULT_API_URL,
        token: str | None = None,
        body: dict | None = None,
        params: dict | None = None,
        timeout: float = 15,
    ) -> dict[str, Any]:
        major, minor, patch = map(int, PROTOCOL_VERSION.split("."))
        headers = {
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": str((major << 16) | (minor << 8) | patch),
        }
        if method == "POST":
            headers.update(
                {
                    "Content-Type": "application/json",
                    "AuthorizationType": "ilink_bot_token",
                    "X-WECHAT-UIN": base64.b64encode(
                        str(secrets.randbits(32)).encode()
                    ).decode(),
                }
            )
        if token:
            headers["Authorization"] = f"Bearer {token}"
        with self._client.stream(
            method,
            api_base_url(base_url) + path,
            headers=headers,
            json=body,
            params=params,
            timeout=timeout,
            follow_redirects=False,
        ) as response:
            if response.status_code >= 300:
                raise IlinkError(f"http_{response.status_code}")
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > 2 * 1024 * 1024:
                    raise IlinkError("response_too_large")
        try:
            result = json.loads(data)
        except (ValueError, UnicodeError) as exc:
            raise IlinkError("invalid_response") from exc
        if not isinstance(result, dict):
            raise IlinkError("invalid_response")
        for name in ("ret", "errcode"):
            if name in result:
                code = result[name]
                if type(code) is not int:
                    raise IlinkError("invalid_response")
                if code != 0:
                    raise IlinkError(code)
        return result

    def get_qrcode(self, tokens: list[str]) -> dict[str, Any]:
        return self.request(
            "POST",
            "/ilink/bot/get_bot_qrcode",
            params={"bot_type": "3"},
            body={"local_token_list": tokens[-10:]},
        )

    def qr_status(
        self, qrcode: str, *, base_url: str, verify_code: str | None = None
    ) -> dict[str, Any]:
        params = {"qrcode": qrcode}
        if verify_code:
            params["verify_code"] = verify_code
        return self.request(
            "GET",
            "/ilink/bot/get_qrcode_status",
            base_url=base_url,
            params=params,
            timeout=35,
        )

    def updates(self, account: IlinkAccount, cursor: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "/ilink/bot/getupdates",
            base_url=account.base_url,
            token=account.token,
            body={"get_updates_buf": cursor, "base_info": BASE_INFO},
            timeout=35,
        )

    def send_text(
        self,
        account: IlinkAccount,
        *,
        user_id: str,
        context_token: str,
        text: str,
        client_id: str,
    ) -> None:
        result = self.request(
            "POST",
            "/ilink/bot/sendmessage",
            base_url=account.base_url,
            token=account.token,
            body={
                "base_info": BASE_INFO,
                "msg": {
                    "from_user_id": "",
                    "to_user_id": user_id,
                    "client_id": client_id,
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [{"type": 1, "text_item": {"text": text}}],
                },
            },
        )
        # The current SDK returns an optional uint64 server message ID and does
        # not require ret. Keep legacy ret=0 receipts, but require confirmation
        # and reject malformed fields rather than treating them as delivered.
        confirmed_id = False
        if "message_id" in result:
            message_id = result["message_id"]
            if type(message_id) is int:
                confirmed_id = 0 < message_id <= 2**64 - 1
            elif (
                isinstance(message_id, str)
                and 0 < len(message_id) <= 20
                and message_id.isascii()
                and message_id.isdigit()
            ):
                confirmed_id = 0 < int(message_id) <= 2**64 - 1
            if not confirmed_id:
                raise IlinkError("invalid_response")
        if result.get("ret") != 0 and not confirmed_id:
            raise IlinkError("unconfirmed_delivery")


class IlinkChannelAdapter:
    def __init__(self, store: IlinkStore, api: IlinkAPI | None = None):
        self.store = store
        self.api = api or IlinkAPI()
        self._owns_api = api is None

    def close(self) -> None:
        if self._owns_api:
            self.api.close()

    def send(self, *, route_key: str, text: str, idempotency_key: str) -> None:
        destination = self.store.destination(route_key)
        account = self.store.account(destination["account_id"]) if destination else None
        if not destination or not account or destination["user_id"] != account.user_id:
            raise IlinkError("private_route_not_found")
        key = hashlib.sha256(idempotency_key.encode()).hexdigest()
        payload_hash = hashlib.sha256(
            json.dumps([route_key, text], ensure_ascii=False).encode()
        ).hexdigest()
        status = self.store.claim_delivery(account.account_id, key, payload_hash)
        if status == "sent":
            return
        if status in ("pending", "unknown"):
            raise httpx.TransportError(
                "Previous WeChat delivery has an unknown outcome"
            )
        if (
            not destination["context_token"]
            or time.time() - destination["updated_at"] > 24 * 60 * 60
        ):
            self.store.delivery_result(account.account_id, key, "failed")
            raise IlinkError("reply_context_expired")
        client_id = (
            "gsm-" + hashlib.sha256(f"{account.account_id}:{key}".encode()).hexdigest()
        )
        try:
            self.api.send_text(
                account,
                user_id=account.user_id,
                context_token=destination["context_token"],
                text=text,
                client_id=client_id,
            )
        except IlinkError as exc:
            if exc.code in (-14, "http_401", "http_403"):
                self.store.poll_result(account.account_id, status="login_required")
            # Malformed success responses and server errors have ambiguous outcomes.
            unknown = exc.code in (
                "unconfirmed_delivery",
                "invalid_response",
                "response_too_large",
            ) or str(exc.code).startswith("http_5")
            self.store.delivery_result(
                account.account_id, key, "unknown" if unknown else "failed"
            )
            if unknown:
                raise httpx.TransportError(
                    "WeChat delivery could not be confirmed"
                ) from exc
            raise
        except httpx.HTTPError:
            self.store.delivery_result(account.account_id, key, "unknown")
            raise
        self.store.delivery_result(account.account_id, key, "sent")
