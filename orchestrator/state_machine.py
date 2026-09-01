"""Pure deterministic event reducer with fail-closed coordination protections."""

import hashlib
import json
from dataclasses import replace

from .contract import Event, EventType, Risk, Snapshot, State
from .risk import classify


class TransitionError(ValueError):
    pass


class ConcurrencyError(TransitionError):
    pass


class StaleEventError(TransitionError):
    pass


class IntegrityError(TransitionError):
    """An event identity was reused for a different event body."""


_TRANSITIONS = {
    (State.PLANNED, EventType.START): State.IMPLEMENTING,
    (State.IMPLEMENTING, EventType.IMPLEMENTED): State.REVIEWING,
    (State.IMPLEMENTING, EventType.RUNNER_FAILED): State.BLOCKED,
    (State.REVIEWING, EventType.REVIEW_REQUESTED): State.REVIEWING,
    (State.REVIEWING, EventType.REVIEW_PASSED): State.COMPLETE,
    (State.REVIEWING, EventType.REVIEW_FINDINGS): State.FIXING,
    (State.FIXING, EventType.REVIEW_FINDINGS): State.HUMAN_DECISION_REQUIRED,
    (State.HUMAN_DECISION_REQUIRED, EventType.HUMAN_DECISION): State.FIXING,
    (State.FIXING, EventType.FIX_APPLIED): State.REVIEWING,
    (State.BLOCKED, EventType.RECOVER): State.RECOVERY,
    (State.RECOVERY, EventType.REPLAY): State.RECOVERY,
}


def _fingerprint(event: Event) -> str:
    body = json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


class Orchestrator:
    """In-memory reference implementation; callers own durable event storage."""

    def __init__(self, run_id: str, source_sha: str, max_review_cycles: int = 2):
        if not isinstance(run_id, str) or not run_id or not isinstance(source_sha, str) or not source_sha:
            raise ValueError("run_id and source_sha must be non-empty strings")
        if max_review_cycles < 1:
            raise ValueError("max_review_cycles must be positive")
        self.max_review_cycles = max_review_cycles
        self.snapshot = Snapshot(run_id, State.PLANNED, Risk.GREEN, 0, 0, source_sha, 0, None, (), (), False, None)
        self.events: list[Event] = []

    def apply(self, event: Event) -> Snapshot:
        event.validate()
        s = self.snapshot
        if event.run_id != s.run_id:
            raise TransitionError("event run_id does not match")
        matches = [e for e in self.events if e.event_id == event.event_id or e.idempotency_key == event.idempotency_key]
        if matches:
            if all(_fingerprint(e) == _fingerprint(event) for e in matches):
                return s
            raise IntegrityError("event identity or idempotency key was reused with a different body")
        if event.source_sha != s.source_sha:
            raise StaleEventError("event source SHA is stale")
        if event.expected_version != s.version:
            raise ConcurrencyError(f"expected version {event.expected_version}, current is {s.version}")
        if event.sequence != s.last_sequence + 1:
            raise StaleEventError(f"expected sequence {s.last_sequence + 1}, got {event.sequence}")
        if (s.state, event.event_type) not in _TRANSITIONS:
            raise TransitionError(f"{event.event_type} is invalid from {s.state}")

        payload = event.payload
        event_risk = classify(**{k: payload[k] for k in ("tests_pass", "scope_changed", "destructive", "external_side_effect", "human_approved") if k in payload})
        cycles = s.review_cycles + int(event.event_type == EventType.REVIEW_FINDINGS)
        red_pending = s.red_pending or event_risk == Risk.RED
        gate_reason = s.gate_reason
        next_state = _TRANSITIONS[(s.state, event.event_type)]
        decision = payload.get("decision", s.human_decision)

        if event.event_type == EventType.REVIEW_FINDINGS:
            if payload.get("human_required"):
                next_state, gate_reason = State.HUMAN_DECISION_REQUIRED, payload.get("gate_reason", "review_requires_human_decision")
            elif red_pending:
                next_state, gate_reason = State.HUMAN_DECISION_REQUIRED, "red_risk_requires_human_approval"
            elif cycles > self.max_review_cycles:
                next_state, gate_reason = State.HUMAN_DECISION_REQUIRED, "automatic_review_cycle_limit_reached"
            else:
                gate_reason = None
        elif event.event_type == EventType.REVIEW_PASSED and red_pending:
            next_state, gate_reason = State.HUMAN_DECISION_REQUIRED, "red_risk_requires_human_approval"
        elif event.event_type == EventType.HUMAN_DECISION:
            if decision not in ("approve_fix", "approve_red_action", "stop"):
                raise TransitionError("human decision must be approve_fix, approve_red_action, or stop")
            if decision == "stop":
                next_state, event_risk, gate_reason = State.BLOCKED, Risk.RED, "human_stopped_run"
            elif decision == "approve_red_action":
                if not s.red_pending:
                    raise TransitionError("approve_red_action requires a RED gate")
                red_pending, gate_reason = False, None
                next_state = State.REVIEWING
            elif s.red_pending:
                raise TransitionError("approve_fix cannot satisfy a RED gate; use approve_red_action")
            else:
                gate_reason = None
        elif event.event_type == EventType.RECOVER:
            gate_reason = s.gate_reason or "recovery_requires_replay"
        elif event.event_type == EventType.REPLAY:
            # Recovery never jumps back into execution. The durable blocked reason remains authoritative.
            next_state, gate_reason = State.BLOCKED, s.gate_reason or "recovery_replay_preserved_block"

        risk = Risk.RED if red_pending or event_risk == Risk.RED else event_risk
        if next_state == State.COMPLETE and red_pending:
            raise TransitionError("RED work requires explicit approve_red_action before completion")
        if next_state == State.COMPLETE and payload.get("tests_pass") is False:
            raise TransitionError("work cannot complete while tests fail")
        findings = tuple(payload.get("findings", s.findings))
        self.snapshot = replace(s, state=next_state, risk=risk, version=s.version + 1,
                                last_sequence=event.sequence, review_cycles=cycles,
                                human_decision=decision, applied_event_ids=s.applied_event_ids + (event.event_id,),
                                findings=findings, red_pending=red_pending, gate_reason=gate_reason)
        self.events.append(event)
        return self.snapshot

    def replay(self, events: list[Event]) -> Snapshot:
        """Apply a complete durable history transactionally; corrupt history changes nothing."""
        candidate = Orchestrator(self.snapshot.run_id, self.snapshot.source_sha, self.max_review_cycles)
        try:
            for event in events:
                candidate.apply(event)
        except (TransitionError, ValueError, TypeError) as exc:
            raise IntegrityError("corrupt or invalid event history; replay aborted") from exc
        self.snapshot, self.events = candidate.snapshot, candidate.events
        return self.snapshot
