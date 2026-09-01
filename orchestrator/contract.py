"""Versioned, JSON-compatible coordination contract for Slice A."""

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class State(StrEnum):
    PLANNED = "planned"
    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    HUMAN_DECISION_REQUIRED = "human_decision_required"
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
        if not isinstance(value, dict):
            raise ValueError("event must be an object")
        required = {"event_id", "event_type", "run_id", "sequence", "expected_version", "source_sha", "idempotency_key"}
        missing = required - value.keys()
        if missing:
            raise ValueError(f"missing event fields: {sorted(missing)}")
        unknown = set(value) - required - {"payload", "schema"}
        if unknown:
            raise ValueError(f"unknown event fields: {sorted(unknown)}")
        try:
            event_type = EventType(value["event_type"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("unknown event type") from exc
        event = cls(**{**value, "event_type": event_type})
        event.validate()
        return event

    def validate(self) -> None:
        if self.schema != "loop/v1":
            raise ValueError("unknown contract version")
        if not isinstance(self.event_type, EventType):
            raise ValueError("event_type must be a known EventType")
        if not all(isinstance(v, str) and v for v in (self.event_id, self.run_id, self.source_sha, self.idempotency_key)):
            raise ValueError("event identity fields must be non-empty strings")
        if not isinstance(self.sequence, int) or self.sequence < 1:
            raise ValueError("sequence must be a positive integer")
        if not isinstance(self.expected_version, int) or self.expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be an object")


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
    red_pending: bool
    gate_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["state"] = self.state.value
        value["risk"] = self.risk.value
        value["applied_event_ids"] = list(self.applied_event_ids)
        value["findings"] = list(self.findings)
        return value
