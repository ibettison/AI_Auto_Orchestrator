import unittest
from orchestrator.contract import Event, EventType, Risk, State
from orchestrator.simulator import event, scenario_amber_fix, scenario_green, scenario_red_gate
from orchestrator.state_machine import ConcurrencyError, Orchestrator, StaleEventError, TransitionError


class SliceATests(unittest.TestCase):
  def test_green_path_completes(self):
    s = scenario_green().snapshot
    self.assertEqual((s.state, s.risk, s.version), (State.COMPLETE, Risk.GREEN, 3))


  def test_amber_path_requires_gate_and_fix(self):
    s = scenario_amber_fix().snapshot
    self.assertEqual(s.state, State.COMPLETE)
    self.assertEqual(s.review_cycles, 1)


  def test_red_stop_gate_blocks(self):
    s = scenario_red_gate().snapshot
    self.assertEqual((s.state, s.risk), (State.BLOCKED, Risk.RED))


  def test_duplicate_event_is_idempotent(self):
    o = Orchestrator("r", "sha")
    e = event("r", 1, EventType.START, 0, sha="sha")
    o.apply(e)
    self.assertEqual(o.apply(e), o.snapshot)
    self.assertEqual(o.snapshot.version, 1)


  def test_stale_sha_and_optimistic_concurrency_are_rejected(self):
    o = Orchestrator("r", "sha")
    with self.assertRaises(StaleEventError):
        o.apply(event("r", 1, EventType.START, 0, sha="old"))
    o.apply(event("r", 1, EventType.START, 0, sha="sha"))
    with self.assertRaises(ConcurrencyError):
        o.apply(event("r", 2, EventType.IMPLEMENTED, 0, sha="sha"))


  def test_review_cycles_are_bounded(self):
    o = Orchestrator("r", "sha", max_review_cycles=1)
    o.apply(event("r", 1, EventType.START, 0, sha="sha"))
    o.apply(event("r", 2, EventType.IMPLEMENTED, 1, sha="sha"))
    o.apply(event("r", 3, EventType.REVIEW_FINDINGS, 2, sha="sha"))
    o.apply(event("r", 4, EventType.HUMAN_DECISION, 3, sha="sha", decision="approve_fix"))
    o.apply(event("r", 5, EventType.FIX_APPLIED, 4, sha="sha"))
    s = o.apply(event("r", 6, EventType.REVIEW_FINDINGS, 5, sha="sha"))
    self.assertEqual((s.state, s.risk), (State.BLOCKED, Risk.AMBER))

  def test_red_work_cannot_bypass_gate_with_review_pass(self):
    o = Orchestrator("r", "sha")
    o.apply(event("r", 1, EventType.START, 0, sha="sha"))
    o.apply(event("r", 2, EventType.IMPLEMENTED, 1, sha="sha"))
    with self.assertRaises(TransitionError):
      o.apply(event("r", 3, EventType.REVIEW_PASSED, 2, sha="sha", external_side_effect=True))


  def test_replay_is_deterministic(self):
    original = scenario_amber_fix()
    replayed = Orchestrator("amber", "sha-demo").replay(original.events)
    self.assertEqual(replayed, original.snapshot)


if __name__ == "__main__":
  unittest.main()
