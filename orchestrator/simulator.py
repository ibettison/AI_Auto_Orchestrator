"""Offline scenarios for demonstrating contract behavior."""

import argparse
import json
from .contract import Event, EventType
from .state_machine import Orchestrator


def event(run, n, typ, version, sha="sha-demo", key=None, **payload):
    return Event(f"evt-{n}", typ, run, n, version, sha, key or f"key-{n}", payload)


def scenario_green():
    o = Orchestrator("green", "sha-demo")
    for n, typ, payload in [(1, EventType.START, {}), (2, EventType.IMPLEMENTED, {"tests_pass": True}), (3, EventType.REVIEW_PASSED, {"tests_pass": True})]:
        o.apply(event("green", n, typ, n - 1, **payload))
    return o


def scenario_amber_fix():
    o = Orchestrator("amber", "sha-demo")
    seq = [(EventType.START, {}), (EventType.IMPLEMENTED, {"scope_changed": True}), (EventType.REVIEW_FINDINGS, {"findings": ["missing test"]}), (EventType.HUMAN_DECISION, {"decision": "approve_fix", "human_approved": True}), (EventType.FIX_APPLIED, {"tests_pass": True}), (EventType.REVIEW_PASSED, {"tests_pass": True})]
    for n, (typ, payload) in enumerate(seq, 1):
        o.apply(event("amber", n, typ, n - 1, **payload))
    return o


def scenario_red_gate():
    o = Orchestrator("red", "sha-demo")
    for n, (typ, payload) in enumerate([(EventType.START, {}), (EventType.IMPLEMENTED, {"external_side_effect": True}), (EventType.REVIEW_FINDINGS, {"destructive": True}), (EventType.HUMAN_DECISION, {"decision": "stop"})], 1):
        o.apply(event("red", n, typ, n - 1, **payload))
    return o


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["green", "amber-fix", "red-gate", "all"], default="all")
    args = parser.parse_args()
    scenarios = {"green": scenario_green, "amber-fix": scenario_amber_fix, "red-gate": scenario_red_gate}
    selected = scenarios.items() if args.scenario == "all" else [(args.scenario, scenarios[args.scenario])]
    print(json.dumps({name: fn().snapshot.to_dict() for name, fn in selected}, indent=2))


if __name__ == "__main__":
    main()

