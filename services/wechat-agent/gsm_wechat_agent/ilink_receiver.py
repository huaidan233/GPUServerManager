from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from threading import Event
from typing import Any
from urllib.parse import urlsplit

import httpx

from gsm_wechat_agent.ilink import (
    IlinkAccount,
    IlinkAPI,
    IlinkChannelAdapter,
    IlinkError,
    IlinkStore,
)
from gsm_wechat_agent.routing import route_key

logger = logging.getLogger(__name__)
_ERROR_REPLIES = {
    400: "配对码或请求无效，请检查后重新发送。",
    403: "当前账号没有执行这项操作的权限，请检查账号状态或联系管理员。",
    404: "绑定或目标资源不存在，请在面板检查后重试。",
    409: "请求发生冲突，请先在面板检查当前状态。",
    410: "配对码已过期或已使用，请在面板重新生成。",
}


def inbound_url() -> str:
    value = os.environ.get(
        "GSM_WECHAT_ILINK_INBOUND_URL", "http://wechat-agent:8000/wechat-agent/inbound"
    )
    parsed = urlsplit(value)
    # The inbound secret must never be sent to an arbitrary external endpoint.
    if (
        parsed.scheme not in ("http", "https")
        or parsed.hostname not in ("wechat-agent", "localhost", "127.0.0.1", "::1")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/wechat-agent/inbound"
    ):
        raise IlinkError("invalid_inbound_url")
    return value


class IlinkReceiver:
    def __init__(
        self,
        store: IlinkStore,
        *,
        api: IlinkAPI | None = None,
        http_client: httpx.Client | None = None,
        url: str | None = None,
        secret: str | None = None,
    ):
        self.store = store
        self.api = api or IlinkAPI()
        self._owns_api = api is None
        self._client = http_client or httpx.Client(
            timeout=90, trust_env=False, follow_redirects=False
        )
        self._owns_client = http_client is None
        self._url = url or inbound_url()
        self._secret = secret or os.environ.get("GSM_WECHAT_INBOUND_SECRET", "")
        if not self._secret:
            self.close()
            raise IlinkError("inbound_secret_required")
        self.adapter = IlinkChannelAdapter(store, self.api)
        self.owner = secrets.token_hex(16)

    def close(self) -> None:
        if self._owns_api:
            self.api.close()
        if self._owns_client:
            self._client.close()

    def forward(self, payload: dict[str, Any]) -> str:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(24)
        signature = hmac.new(
            self._secret.encode(),
            timestamp.encode() + b"." + nonce.encode() + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-GSM-WeChat-Timestamp": timestamp,
            "X-GSM-WeChat-Nonce": nonce,
            "X-GSM-WeChat-Signature": "sha256=" + signature,
        }
        try:
            with self._client.stream(
                "POST",
                self._url,
                content=body,
                headers=headers,
                timeout=90,
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    return _ERROR_REPLIES.get(
                        response.status_code,
                        "系统暂时无法确认处理结果，请到面板查看操作状态后再决定是否重发。",
                    )
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 1024 * 1024:
                        raise IlinkError("inbound_response_too_large")
            result = json.loads(data)
            reply = result.get("reply") if isinstance(result, dict) else None
            if not isinstance(reply, str) or not reply or len(reply) > 8192:
                raise IlinkError("invalid_inbound_reply")
            return reply
        except (httpx.HTTPError, IlinkError, ValueError):
            # Never repeat dispatch: the business action may already have executed.
            return (
                "连接中断，暂时无法确认处理结果。请到面板查看操作状态后再决定是否重发。"
            )

    def process_message(self, account: IlinkAccount, message: dict[str, Any]) -> bool:
        if (
            not isinstance(message, dict)
            or message.get("group_id")
            or message.get("message_type") != 1
            or message.get("message_state") not in (None, 0, 2)
            or message.get("from_user_id") != account.user_id
            or message.get("to_user_id") not in (None, "", account.account_id)
        ):
            return False
        message_id = message.get("message_id")
        context = message.get("context_token")
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, (str, int))
            or not str(message_id)
            or len(str(message_id)) > 128
            or not isinstance(context, str)
            or not context
            or len(context) > 4096
        ):
            return False
        items = message.get("item_list")
        if not isinstance(items, list) or not items or len(items) > 100:
            return False
        text_parts = []
        supported = True
        for item in items:
            if not isinstance(item, dict) or item.get("type") != 1:
                supported = False
                continue
            text_item = item.get("text_item")
            text = text_item.get("text") if isinstance(text_item, dict) else None
            if not isinstance(text, str):
                supported = False
            else:
                text_parts.append(text)
        text = "\n".join(text_parts).strip()
        if not self.store.claim_message(account.account_id, str(message_id)):
            return False
        route = route_key(account.account_id, account.user_id, account.user_id)
        self.store.save_route(route, account.account_id, account.user_id, context)
        if supported and 0 < len(text) <= 8192:
            reply = self.forward(
                {
                    "account_id": account.account_id,
                    "conversation_id": account.user_id,
                    "sender_id": account.user_id,
                    "conversation_kind": "private",
                    "message": text,
                }
            )
        else:
            reply = "当前仅支持文字私聊。请发送不超过 8192 字符的文字；图片、语音和文件暂未接入。"
        self.adapter.send(
            route_key=route,
            text=reply,
            idempotency_key=f"reply:{account.account_id}:{message_id}",
        )
        return True

    def poll_once(self, account: IlinkAccount, stop: Event | None = None) -> bool:
        if not self.store.lease(account.account_id, self.owner):
            return False
        result = self.api.updates(account, self.store.cursor(account.account_id))
        # The CLI may replace credentials or remove an account while polling.
        if (stop and stop.is_set()) or self.store.account(
            account.account_id
        ) != account:
            return False
        messages = result.get("msgs", [])
        if not isinstance(messages, list) or len(messages) > 500:
            raise IlinkError("invalid_updates")
        self.store.poll_result(account.account_id)
        for message in messages:
            if stop and stop.is_set():
                return False
            if not self.store.lease(account.account_id, self.owner):
                return False
            try:
                self.process_message(account, message)
            except (IlinkError, httpx.HTTPError) as exc:
                # Logs intentionally omit text, sender IDs, context tokens and exception bodies.
                logger.warning(
                    "WeChat reply failed (%s); dispatched message will not be replayed",
                    type(exc).__name__,
                )
        cursor = result.get("get_updates_buf")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 65536):
            raise IlinkError("invalid_cursor")
        self.store.poll_result(account.account_id, cursor=cursor or None)
        return True

    def run_account(self, account_id: str, stop: Event) -> None:
        delay = 1
        paused_account = None
        retry_after = 0.0
        try:
            while not stop.is_set():
                account = self.store.account(account_id)
                if not account:
                    return
                if account == paused_account and time.monotonic() < retry_after:
                    stop.wait(1)
                    continue
                try:
                    if not self.poll_once(account, stop):
                        stop.wait(2)
                    else:
                        delay = 1
                        stop.wait(0.25)
                except httpx.ReadTimeout:
                    # A long-poll timeout is normal; no message was dispatched.
                    stop.wait(1)
                except (IlinkError, httpx.HTTPError) as exc:
                    invalid_session = isinstance(exc, IlinkError) and exc.code in (
                        -14,
                        "http_401",
                        "http_403",
                    )
                    self.store.poll_result(
                        account_id,
                        status="login_required" if invalid_session else "error",
                    )
                    logger.warning(
                        "WeChat receiver %s (%s)",
                        (
                            "requires a new QR login"
                            if invalid_session
                            else "will retry polling"
                        ),
                        type(exc).__name__,
                    )
                    if invalid_session:
                        # Re-login replaces the token and immediately lifts the pause.
                        paused_account = account
                        retry_after = time.monotonic() + 3600
                    stop.wait(delay)
                    delay = min(delay * 2, 60)
        finally:
            self.store.release(account_id, self.owner)
            self.close()
