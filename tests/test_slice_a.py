import unittest

from orchestrator.contract import Event, EventType, Risk, State
from orchestrator.simulator import event, scenario_amber_fix, scenario_green, scenario_red_gate
from orchestrator.state_machine import (
    ConcurrencyError,
    IntegrityError,
    Orchestrator,
    StaleEventError,
    TransitionError,
)


class SliceATests(unittest.TestCase):
    def test_green_path_completes(self):
        s = scenario_green().snapshot
        self.assertEqual((s.state, s.risk, s.version), (State.COMPLETE, Risk.GREEN, 3))

    def test_ordinary_findings_auto_fix_without_human_gate(self):
        s = scenario_amber_fix().snapshot
        self.assertEqual((s.state, s.review_cycles), (State.COMPLETE, 1))

    def test_red_stop_gate_blocks(self):
        s = scenario_red_gate().snapshot
        self.assertEqual((s.state, s.risk, s.gate_reason), (State.BLOCKED, Risk.RED, "human_stopped_run"))

    def test_all_legal_recovery_transitions_preserve_block(self):
        o = scenario_red_gate()
        o.apply(event("red", 5, EventType.RECOVER, 4, sha="sha-demo"))
        s = o.apply(event("red", 6, EventType.REPLAY, 5, sha="sha-demo"))
        self.assertEqual((s.state, s.risk), (State.BLOCKED, Risk.RED))

    def test_review_request_is_a_legal_reviewer_transition(self):
        o = Orchestrator("r", "sha")
        o.apply(event("r", 1, EventType.START, 0, sha="sha"))
        o.apply(event("r", 2, EventType.IMPLEMENTED, 1, sha="sha"))
        s = o.apply(event("r", 3, EventType.REVIEW_REQUESTED, 2, sha="sha"))
        self.assertEqual(s.state, State.REVIEWING)

    def test_duplicate_exact_event_is_idempotent(self):
        o = Orchestrator("r", "sha")
        e = event("r", 1, EventType.START, 0, sha="sha")
        o.apply(e)
        self.assertEqual(o.apply(e), o.snapshot)
        self.assertEqual(o.snapshot.version, 1)

    def test_event_payload_is_immutable_and_replay_stays_deterministic(self):
        payload = {"findings": ["one"], "metadata": {"owner": "review"}}
        o = Orchestrator("r", "sha")
        start = event("r", 1, EventType.START, 0, sha="sha")
        o.apply(start)
        findings = event("r", 2, EventType.IMPLEMENTED, 1, sha="sha", **payload)
        o.apply(findings)
        payload["findings"].append("mutated caller input")
        payload["metadata"]["owner"] = "mutated caller input"
        with self.assertRaises(TypeError):
            findings.payload["findings"] = ("changed",)
        with self.assertRaises(TypeError):
            findings.payload["findings"] += ("changed",)
        self.assertEqual(findings.to_dict()["payload"], {"findings": ["one"], "metadata": {"owner": "review"}})
        replayed = Orchestrator("r", "sha").replay(o.events)
        self.assertEqual(replayed, o.snapshot)

    def test_conflicting_event_id_and_key_fail_closed(self):
        o = Orchestrator("r", "sha")
        first = event("r", 1, EventType.START, 0, sha="sha", key="same")
        o.apply(first)
        with self.assertRaises(IntegrityError):
            o.apply(event("r", 1, EventType.START, 0, sha="sha", key="different", payload={"changed": True}))
        with self.assertRaises(IntegrityError):
            o.apply(event("r", 2, EventType.START, 1, sha="sha", key="same"))

    def test_red_risk_is_durable_and_needs_distinct_approval(self):
        o = Orchestrator("r", "sha")
        o.apply(event("r", 1, EventType.START, 0, sha="sha"))
        s = o.apply(event("r", 2, EventType.IMPLEMENTED, 1, sha="sha", external_side_effect=True))
        self.assertEqual((s.risk, s.red_pending), (Risk.RED, True))
        s = o.apply(event("r", 3, EventType.REVIEW_PASSED, 2, sha="sha", tests_pass=True))
        self.assertEqual(s.state, State.HUMAN_DECISION_REQUIRED)
        with self.assertRaises(TransitionError):
            o.apply(event("r", 4, EventType.HUMAN_DECISION, 3, sha="sha", decision="approve_fix"))
        o.apply(event("r", 4, EventType.HUMAN_DECISION, 3, sha="sha", decision="approve_red_action"))
        s = o.apply(event("r", 5, EventType.REVIEW_PASSED, 4, sha="sha", tests_pass=True))
        self.assertEqual((s.state, s.red_pending), (State.COMPLETE, False))

    def test_payload_approval_cannot_bypass_red_gate(self):
        o = Orchestrator("r", "sha")
        o.apply(event("r", 1, EventType.START, 0, sha="sha"))
        s = o.apply(event("r", 2, EventType.IMPLEMENTED, 1, sha="sha", external_side_effect=True, human_approved=True))
        self.assertEqual((s.risk, s.red_pending), (Risk.RED, True))

    def test_review_cycle_limit_escalates_explicitly(self):
        o = Orchestrator("r", "sha", max_review_cycles=1)
        for n, typ, payload in [(1, EventType.START, {}), (2, EventType.IMPLEMENTED, {}), (3, EventType.REVIEW_FINDINGS, {}), (4, EventType.FIX_APPLIED, {}), (5, EventType.REVIEW_FINDINGS, {})]:
            s = o.apply(event("r", n, typ, n - 1, sha="sha", **payload))
        self.assertEqual((s.state, s.gate_reason), (State.HUMAN_DECISION_REQUIRED, "automatic_review_cycle_limit_reached"))

    def test_stale_sha_sequence_and_concurrency_are_rejected(self):
        o = Orchestrator("r", "sha")
        with self.assertRaises(StaleEventError):
            o.apply(event("r", 1, EventType.START, 0, sha="old"))
        with self.assertRaises(StaleEventError):
            o.apply(event("r", 2, EventType.START, 0, sha="sha"))
        o.apply(event("r", 1, EventType.START, 0, sha="sha"))
        with self.assertRaises(ConcurrencyError):
            o.apply(event("r", 2, EventType.IMPLEMENTED, 0, sha="sha"))

    def test_illegal_transition_and_terminal_state(self):
        o = Orchestrator("r", "sha")
        with self.assertRaises(TransitionError):
            o.apply(event("r", 1, EventType.REVIEW_PASSED, 0, sha="sha"))
        for n, typ in enumerate((EventType.START, EventType.IMPLEMENTED, EventType.REVIEW_PASSED), 1):
            o.apply(event("r", n, typ, n - 1, sha="sha", tests_pass=True))
        with self.assertRaises(TransitionError):
            o.apply(event("r", 4, EventType.START, 3, sha="sha"))

    def test_malformed_unknown_version_and_event_type_are_rejected(self):
        base = event("r", 1, EventType.START, 0, sha="sha").to_dict()
        invalid_values = [{**base, "schema": "loop/v9"}, {**base, "event_type": "unknown"}, {**base, "sequence": 0}, {**base, "payload": []}, {key: value for key, value in base.items() if key != "event_id"}, []]
        for value in invalid_values:
            with self.assertRaises(ValueError):
                Event.from_dict(value)

    def test_corrupt_history_replay_is_transactional(self):
        o = Orchestrator("r", "sha")
        corrupt = [event("r", 1, EventType.START, 0, sha="sha"), event("r", 3, EventType.IMPLEMENTED, 1, sha="sha")]
        with self.assertRaises(IntegrityError):
            o.replay(corrupt)
        self.assertEqual((o.snapshot.state, o.snapshot.version), (State.PLANNED, 0))


if __name__ == "__main__":
    unittest.main()
