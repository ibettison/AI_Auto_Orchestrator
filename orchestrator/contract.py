"""Versioned, JSON-compatible coordination contract for Slice A."""

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class State(StrEnum):
    PLANNED = "planned"
    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    WAITING_HUMAN = "waiting_human"
    FIXING = "fixing"
    COMPLETE = "complete"
    BLOCKED = "blocked"
    RECOVERY = "recovery"


class Risk(StrEnum):
    GREEN = "green"
    AMBER = "amber"
    RED = "red"


class EventType(StrEnum):
    START = "start"
    IMPLEMENTED = "implemented"
    REVIEW_REQUESTED = "review_requested"
    REVIEW_PASSED = "review_passed"
    REVIEW_FINDINGS = "review_findings"
    HUMAN_DECISION = "human_decision"
    FIX_APPLIED = "fix_applied"
    COMPLETE = "complete"
    RECOVER = "recover"
    REPLAY = "replay"


@dataclass(frozen=True)
class Event:
    """An append-only event. Event IDs and idempotency keys are caller supplied."""

    event_id: str
    event_type: EventType
    run_id: str
    sequence: int
    expected_version: int
    source_sha: str
    idempotency_key: str
    payload: dict[str, Any] = field(default_factory=dict)
    schema: str = "loop/v1"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["event_type"] = self.event_type.value
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Event":
        return cls(**{**value, "event_type": EventType(value["event_type"])})


@dataclass(frozen=True)
class Snapshot:
    run_id: str
    state: State
    risk: Risk
    version: int
    last_sequence: int
    source_sha: str
    review_cycles: int
    human_decision: str | None
    applied_event_ids: tuple[str, ...]
    findings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["state"] = self.state.value
        value["risk"] = self.risk.value
        value["applied_event_ids"] = list(self.applied_event_ids)
        value["findings"] = list(self.findings)
        return value

