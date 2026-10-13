from datetime import datetime, timedelta, timezone

from gsm_api.confirmations import Confirmation, can_confirm, required_phrase_for


def test_confirmation_requires_exact_phrase():
    confirmation = Confirmation(
        id="c1",
        required_phrase="确认停止 container llama-debug",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
    )
    assert can_confirm(confirmation, "确认停止 container llama-debug")
    assert not can_confirm(confirmation, "确认")


def test_expired_confirmation_is_rejected():
    confirmation = Confirmation(
        id="c1",
        required_phrase="确认停止 container llama-debug",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    assert not can_confirm(confirmation, "确认停止 container llama-debug")


def test_required_phrase_names_target():
    assert (
        required_phrase_for("停止", "container", "llama-debug")
        == "确认停止 container llama-debug"
    )
