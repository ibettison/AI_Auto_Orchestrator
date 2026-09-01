# AI Auto Orchestrator — Slice A

This repository is an independent, offline reference implementation for coordinating AI development work. It defines a versioned `loop/v1` event contract and a deterministic state machine. It has no provider, network, repository, webhook, email, production, or automation integration.

## Contract

Events are append-only JSON-compatible records containing `event_id`, `event_type`, `run_id`, `sequence`, `expected_version`, `source_sha`, `idempotency_key`, and `payload`. A snapshot is derived only by applying valid events. `expected_version` provides optimistic concurrency; `sequence`, `source_sha`, and `run_id` reject stale or misrouted events. Repeating an event ID or idempotency key is a no-op.

Payloads are recursively frozen when an event is constructed, so caller mutations cannot rewrite retained history or change replay results. `Event.to_dict()` returns a JSON-compatible copy.

States are `planned → implementing → reviewing → complete`, with ordinary findings taking an automatic `fixing → reviewing` loop. RED conditions and exhausted review budgets enter `human_decision_required`; explicit `blocked/recovery` paths are fail-closed. Review findings consume a bounded cycle budget (default two). Human decisions support `approve_fix`, `approve_red_action`, and `stop`; generic fix approval cannot satisfy a RED gate.

Risk is deterministic: GREEN means tests pass with no scope or external side effect; AMBER means test/scope uncertainty or a human-approved fix; RED means destructive/external side effects without approval, or an explicit human stop. RED is carried durably in the snapshot until `approve_red_action` is applied.

## Run it offline

```bash
python3 -m unittest discover -v
python3 -m orchestrator.simulator --scenario all
```

## LayMatched example profile

The profile below is deliberately descriptive only. It does not connect to, inspect, or modify LayMatched.

```json
{
  "profile": "laymatched-example",
  "source_sha": "sha256:example-input",
  "scope": "documentation-only change",
  "risk": "green",
  "human_gate": "required when scope changes or an external side effect is proposed",
  "max_review_cycles": 2,
  "providers": []
}
```

## Scope and limitations

Persistence, authentication, permissions, real Git operations, provider adapters, and production execution are intentionally out of scope for Slice A. A caller owns durable storage and must retain the event log for replay/recovery. The in-memory implementation is a contract simulator, not a production coordinator.
