"""Pure deterministic event reducer with optimistic concurrency protections."""

from dataclasses import replace
from .contract import Event, EventType, Risk, Snapshot, State
from .risk import classify


class TransitionError(ValueError):
    pass


class ConcurrencyError(TransitionError):
    pass


class StaleEventError(TransitionError):
    pass


_TRANSITIONS = {
    (State.PLANNED, EventType.START): State.IMPLEMENTING,
    (State.IMPLEMENTING, EventType.IMPLEMENTED): State.REVIEWING,
    (State.REVIEWING, EventType.REVIEW_REQUESTED): State.REVIEWING,
    (State.REVIEWING, EventType.REVIEW_PASSED): State.COMPLETE,
    (State.REVIEWING, EventType.REVIEW_FINDINGS): State.WAITING_HUMAN,
    (State.WAITING_HUMAN, EventType.HUMAN_DECISION): State.FIXING,
    (State.FIXING, EventType.FIX_APPLIED): State.REVIEWING,
    (State.BLOCKED, EventType.RECOVER): State.RECOVERY,
    (State.RECOVERY, EventType.REPLAY): State.REVIEWING,
}


class Orchestrator:
    """In-memory reference implementation; persistence is an event log supplied by caller."""

    def __init__(self, run_id: str, source_sha: str, max_review_cycles: int = 2):
        if max_review_cycles < 1:
            raise ValueError("max_review_cycles must be positive")
        self.max_review_cycles = max_review_cycles
        self.snapshot = Snapshot(run_id, State.PLANNED, Risk.GREEN, 0, 0, source_sha, 0, None, (), ())
        self.events: list[Event] = []

    def apply(self, event: Event) -> Snapshot:
        s = self.snapshot
        if event.schema != "loop/v1" or event.run_id != s.run_id:
            raise TransitionError("event schema or run_id does not match")
        if event.event_id in s.applied_event_ids or event.idempotency_key in {e.idempotency_key for e in self.events}:
            return s
        if event.source_sha != s.source_sha:
            raise StaleEventError("event source SHA is stale")
        if event.expected_version != s.version:
            raise ConcurrencyError(f"expected version {event.expected_version}, current is {s.version}")
        if event.sequence != s.last_sequence + 1:
            raise StaleEventError(f"expected sequence {s.last_sequence + 1}, got {event.sequence}")
        if (s.state, event.event_type) not in _TRANSITIONS:
            raise TransitionError(f"{event.event_type} is invalid from {s.state}")
        next_state = _TRANSITIONS[(s.state, event.event_type)]
        payload = event.payload
        risk = classify(**{k: payload[k] for k in ("tests_pass", "scope_changed", "destructive", "external_side_effect", "human_approved") if k in payload})
        if next_state == State.COMPLETE and risk == Risk.RED:
            raise TransitionError("RED work cannot complete without an approved human gate")
        cycles = s.review_cycles
        if event.event_type == EventType.REVIEW_FINDINGS:
            cycles += 1
            if cycles > self.max_review_cycles:
                next_state, risk = State.BLOCKED, Risk.AMBER
        if event.event_type == EventType.HUMAN_DECISION:
            decision = payload.get("decision")
            if decision not in ("approve_fix", "stop"):
                raise TransitionError("human decision must be approve_fix or stop")
            if decision == "stop":
                next_state, risk = State.BLOCKED, Risk.RED
        decision = payload.get("decision", s.human_decision)
        findings = tuple(payload.get("findings", s.findings))
        self.snapshot = replace(s, state=next_state, risk=risk, version=s.version + 1,
                                last_sequence=event.sequence, review_cycles=cycles,
                                human_decision=decision, applied_event_ids=s.applied_event_ids + (event.event_id,), findings=findings)
        self.events.append(event)
        return self.snapshot

    def replay(self, events: list[Event]) -> Snapshot:
        for event in events:
            self.apply(event)
        return self.snapshot
