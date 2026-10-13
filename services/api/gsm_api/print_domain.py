from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


PrintStatus = Literal[
    "draft",
    "queued",
    "leased",
    "spooling",
    "submitted",
    "completed",
    "failed",
    "cancelled",
    "delivery_unknown",
]

TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "delivery_unknown"}
)

ALLOWED_TRANSITIONS = {
    "draft": frozenset({"queued", "cancelled"}),
    "queued": frozenset({"leased", "cancelled"}),
    "leased": frozenset({"queued", "spooling", "failed", "cancelled"}),
    "spooling": frozenset(
        {"submitted", "failed", "cancelled", "delivery_unknown"}
    ),
    "submitted": frozenset(
        {"completed", "failed", "cancelled", "delivery_unknown"}
    ),
}

_PAGE_RANGE = re.compile(r"(?P<start>[1-9]\d*)(?:\s*-\s*(?P<end>[1-9]\d*))?")


class PrintSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    printer_id: str = Field(min_length=1, max_length=200)
    copies: int = Field(default=1, ge=1, le=20)
    page_ranges: str = Field(default="", max_length=1000)
    sides: Literal[
        "simplex", "duplex_long_edge", "duplex_short_edge"
    ] = "simplex"
    color_mode: Literal["monochrome", "color"] = "monochrome"
    paper: Literal["auto", "A4"] = "A4"


def canonical_page_ranges(value: str, *, page_count: int) -> str:
    if page_count < 1:
        raise ValueError("page_count must be positive")
    if not value.strip():
        return "1" if page_count == 1 else f"1-{page_count}"

    ranges: list[tuple[int, int]] = []
    for part in value.split(","):
        match = _PAGE_RANGE.fullmatch(part.strip())
        if match is None:
            raise ValueError("invalid page range")
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        if end < start:
            raise ValueError("page range must be ascending")
        if end > page_count:
            raise ValueError("page range is out of bounds")
        ranges.append((start, end))

    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    return ",".join(
        str(start) if start == end else f"{start}-{end}"
        for start, end in merged
    )


def ensure_transition(current: str, target: str) -> None:
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise ValueError(f"invalid print status transition: {current} -> {target}")
