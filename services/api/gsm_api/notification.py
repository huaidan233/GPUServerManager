from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from typing import Any, Literal, Protocol

import httpx

from gsm_api import typed_records


DispatchOutcome = Literal["sent", "failed", "unknown", "cancelled"]


class NotificationDispatcher(Protocol):
    def send(
        self,
        *,
        user_id: str,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> DispatchOutcome:
        ...


class HttpWeChatDispatcher:
    def __init__(
        self,
        *,
        agent_url: str | None = None,
        outbound_url: str | None = None,
        internal_token: str,
        http_client: httpx.Client | None = None,
    ) -> None:
        if outbound_url:
            self._url = outbound_url
        elif agent_url:
            self._url = agent_url.rstrip("/") + "/wechat-agent/outbound"
        else:
            raise ValueError("WeChat Agent URL is required")
        self._headers = {"Authorization": f"Bearer {internal_token}"}
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=10, trust_env=False)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def send(
        self,
        *,
        user_id: str,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> DispatchOutcome:
        try:
            response = self._client.post(
                self._url,
                headers=self._headers,
                json={
                    "user_id": user_id,
                    "text": _notification_text(kind, payload),
                    "idempotency_key": idempotency_key,
                },
            )
        except httpx.HTTPError:
            return "unknown"
        if not 200 <= response.status_code < 300:
            return "failed"
        try:
            reported = response.json().get("status")
        except (ValueError, AttributeError):
            return "unknown"
        return reported if reported in {"sent", "failed", "unknown"} else "unknown"


def default_notification_dispatcher():
    from gsm_api.serverchan import MemberNotificationDispatcher
    agent_url = os.environ.get("GSM_WECHAT_AGENT_URL")
    outbound_url = os.environ.get("GSM_WECHAT_OUTBOUND_URL")
    internal_token = os.environ.get("GSM_WECHAT_INTERNAL_TOKEN")
    if not (outbound_url or agent_url) or not internal_token:
        return MemberNotificationDispatcher()
    return MemberNotificationDispatcher(HttpWeChatDispatcher(
        agent_url=agent_url,
        outbound_url=outbound_url,
        internal_token=internal_token,
    ))


def _notification_text(kind: str, payload: dict[str, Any]) -> str:
    if kind == "notification_test":
        return "GPU Server Manager 测试通知\n通知连接已就绪，可接收 GPU 空闲和额度到期提醒。"
    if kind in {"quota_expiring", "quota_expired"}:
        label = "即将到期" if kind == "quota_expiring" else "已到期"
        return f"临时额度{label}：{payload.get('ends_at', '')}。已有容器继续运行，后续启动按有效额度检查。"
    if kind == "training_finished":
        label = {"succeeded": "已完成", "failed": "失败", "interrupted": "已中断，未取得退出码"}.get(payload.get("status"), "状态待核实")
        code = payload.get("exit_code")
        return (f"任务{label}：{payload.get('name', '任务')}\n容器：{payload.get('container_name', '未知')}"
                + (f"\n退出码：{code}" if code is not None else "")
                + f"\n任务ID：{payload.get('id', '')}\n日志摘要：\n{payload.get('log_excerpt', '')}")[:2000]
    if kind == "gpu_idle_inquiry":
        return f"GPU {payload.get('gpu_uuid', '未知')} 长时间低利用率；请回复：继续使用、可以共享或准备释放。"
    if kind == "gpu_queue_candidate":
        return f"有符合条件的 GPU 候选：{len(payload.get('candidates', []))} 张。该推荐不锁定 GPU。"
    if kind == "quota_approval_requested":
        return f"有新的个人额度申请待审批：{payload.get('approval_id', '未知')}。"
    if kind == "quota_approval_decided":
        decision = {"approved": "已通过", "rejected": "已拒绝"}.get(payload.get("decision"), "已更新")
        return f"配额申请 {payload.get('approval_id', '未知')}：{decision}。请到额度申请页面查看详情。"
    if kind == "member_expiry_reminder":
        display_name = payload.get("display_name", "成员")
        graduation = payload.get("expected_graduation_date", "未知")
        days_remaining = payload.get("days_remaining", "未知")
        return (
            f"{display_name} 的预计毕业日期为 {graduation}，距今还有 {days_remaining} 天。"
            "如需延期，请联系管理员提交延期申请。"
        )
    print_status = {
        "print_job_queued": "已进入打印队列",
        "print_job_submitted": "已提交到 Windows 打印队列",
        "print_job_completed": "已离开 Windows 打印队列",
        "print_job_failed": "打印失败",
        "print_job_cancelled": "已取消",
        "print_job_delivery_unknown": "打印结果未知，请勿直接重复提交",
    }.get(kind)
    if print_status:
        return f"打印任务 {payload.get('print_job_id', '未知')}：{print_status}。"
    return f"GPU Server Manager 通知：{kind}。"


def enqueue_notification(
    *,
    user_id: str,
    kind: str,
    payload: dict[str, Any],
    event_id: str,
    now: datetime | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    key = idempotency_key or f"{kind}:{event_id}:{user_id}"
    existing = typed_records.get(typed_records.NOTIFICATION, key)
    if existing is not None:
        return existing
    record = {
        "id": key,
        "user_id": user_id,
        "kind": kind,
        "payload": payload,
        "status": "pending",
        "attempts": 0,
        "created_at": (now or datetime.now(timezone.utc)).isoformat(),
    }
    return typed_records.save(typed_records.NOTIFICATION, key, record)


def dispatch_pending_notifications(
    dispatcher: NotificationDispatcher,
    *,
    now: datetime | None = None,
) -> None:
    current_time = now or datetime.now(timezone.utc)
    for record in typed_records.list_kind(typed_records.NOTIFICATION):
        if record.get("status") != "pending":
            continue
        retry = record.get("next_attempt_at")
        if retry and datetime.fromisoformat(retry) > current_time:
            continue
        claimed = typed_records.transition(
            typed_records.NOTIFICATION,
            record["id"],
            "pending",
            {
                "status": "dispatching",
                "attempts": int(record.get("attempts", 0)) + 1,
                "last_attempt_at": current_time.isoformat(),
            },
        )
        if claimed is None:
            continue
        try:
            send = dispatcher.send
            if claimed["kind"] == "training_finished":
                from functools import partial
                from gsm_api.training_notifications import dispatch_if_current
                send = partial(dispatch_if_current, dispatcher)
            outcome = send(
                user_id=claimed["user_id"],
                kind=claimed["kind"],
                payload=claimed["payload"],
                idempotency_key=claimed["id"],
            )
        except Exception as exc:
            _finish_dispatch(
                claimed["id"],
                status="unknown",
                now=current_time,
                detail=type(exc).__name__,
            )
            continue

        if outcome == "sent":
            _finish_dispatch(claimed["id"], status="sent", now=current_time)
        elif outcome == "cancelled":
            typed_records.transition(typed_records.NOTIFICATION, claimed["id"], "dispatching", {"status": "cancelled"})
        elif outcome == "failed":
            typed_records.compare_and_patch(typed_records.NOTIFICATION, claimed["id"], {"status": "dispatching"},
                {"next_attempt_at": (current_time + timedelta(seconds=min(3600, 30 * 2 ** min(claimed["attempts"], 7)))).isoformat()})
            _finish_dispatch(claimed["id"], status="pending", now=current_time)
        else:
            _finish_dispatch(claimed["id"], status="unknown", now=current_time)


def _finish_dispatch(
    notification_id: str,
    *,
    status: str,
    now: datetime,
    detail: str | None = None,
) -> None:
    timestamp_field = {
        "sent": "sent_at",
        "pending": "last_failed_at",
        "unknown": "unknown_at",
    }[status]
    patch = {"status": status, timestamp_field: now.isoformat()}
    if detail is not None:
        patch["transport_detail"] = detail
    typed_records.transition(
        typed_records.NOTIFICATION,
        notification_id,
        "dispatching",
        patch,
    )


__all__ = [
    "DispatchOutcome",
    "NotificationDispatcher",
    "dispatch_pending_notifications",
    "enqueue_notification",
]
