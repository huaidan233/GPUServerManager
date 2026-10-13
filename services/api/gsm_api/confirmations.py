from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class Confirmation:
    id: str
    required_phrase: str
    expires_at: datetime
    actor_user_id: str = ""
    action_type: str = ""
    action_payload: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"


def required_phrase_for(verb: str, target_type: str, target_name: str) -> str:
    return f"确认{verb} {target_type} {target_name}"


def can_confirm(confirmation: Confirmation, phrase: str) -> bool:
    now = datetime.now(timezone.utc)
    return now <= confirmation.expires_at and phrase == confirmation.required_phrase
