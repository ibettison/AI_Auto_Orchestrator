"""Offline scenarios for demonstrating contract behavior."""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path
from .contract import Event, EventType
from .runner import AdapterResult, BoundedRunner, FakeCodexAdapter, RunnerConfig
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
    seq = [(EventType.START, {}), (EventType.IMPLEMENTED, {"scope_changed": True}), (EventType.REVIEW_FINDINGS, {"findings": ["missing test"]}), (EventType.FIX_APPLIED, {"tests_pass": True}), (EventType.REVIEW_PASSED, {"tests_pass": True})]
    for n, (typ, payload) in enumerate(seq, 1):
        o.apply(event("amber", n, typ, n - 1, **payload))
    return o


def scenario_red_gate():
    o = Orchestrator("red", "sha-demo")
    for n, (typ, payload) in enumerate([(EventType.START, {}), (EventType.IMPLEMENTED, {"external_side_effect": True}), (EventType.REVIEW_FINDINGS, {"destructive": True}), (EventType.HUMAN_DECISION, {"decision": "stop"})], 1):
        o.apply(event("red", n, typ, n - 1, **payload))
    return o


def _runner_config(repo: Path, sha: str, run_id: str, **overrides):
    check = ("python3", "-c", "print('check')")
    values = dict(run_id=run_id, repository=str(repo), source_sha=sha, allowed_paths=("src",),
                  allowed_commands=(check,), required_checks=(check,), objective="offline Slice B scenario",
                  command_timeout_seconds=0.1)
    values.update(overrides)
    return RunnerConfig(**values)


def _temporary_repo():
    directory = tempfile.TemporaryDirectory(prefix="orchestrator-simulator-")
    repo = Path(directory.name) / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "offline-simulator"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "offline@example.invalid"], cwd=repo, check=True)
    (repo / "src").mkdir()
    (repo / "src" / "README.txt").write_text("simulator\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True, stdout=subprocess.PIPE, check=True).stdout.strip()
    return directory, repo, sha


def scenario_runner_green():
    directory, repo, sha = _temporary_repo()
    try:
        def action(_, workspace, commands):
            (workspace / "src" / "result.txt").write_text("fake Codex\n")
            commands.run(("python3", "-c", "print('check')"))
            return AdapterResult(0, "fake implementation")
        return BoundedRunner(FakeCodexAdapter(action)).run(_runner_config(repo, sha, "sim-runner-green"))
    finally:
        directory.cleanup()


def scenario_runner_failure():
    directory, repo, sha = _temporary_repo()
    try:
        return BoundedRunner(FakeCodexAdapter(lambda *_: AdapterResult(3, failure_reason="test command failed"))).run(_runner_config(repo, sha, "sim-runner-failure"))
    finally:
        directory.cleanup()


def scenario_runner_timeout():
    directory, repo, sha = _temporary_repo()
    try:
        timeout_command = ("python3", "-c", "__import__('time').sleep(1)")
        def action(_, __, commands):
            commands.run(timeout_command)
            return AdapterResult(0)
        return BoundedRunner(FakeCodexAdapter(action)).run(_runner_config(repo, sha, "sim-runner-timeout", allowed_commands=(timeout_command,), required_checks=(timeout_command,)))
    finally:
        directory.cleanup()


def scenario_runner_scope_violation():
    directory, repo, sha = _temporary_repo()
    try:
        def action(_, workspace, __):
            (workspace / "forbidden.txt").write_text("out of scope\n")
            return AdapterResult(0)
        return BoundedRunner(FakeCodexAdapter(action)).run(_runner_config(repo, sha, "sim-runner-scope"))
    finally:
        directory.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["green", "amber-fix", "red-gate", "runner", "all"], default="all")
    args = parser.parse_args()
    scenarios = {"green": scenario_green, "amber-fix": scenario_amber_fix, "red-gate": scenario_red_gate}
    if args.scenario in ("runner", "all"):
        scenarios.update({"runner-green": scenario_runner_green, "runner-failure": scenario_runner_failure,
                          "runner-timeout": scenario_runner_timeout, "runner-scope-violation": scenario_runner_scope_violation})
    selected = scenarios.items() if args.scenario == "all" else [(args.scenario, scenarios[args.scenario])] if args.scenario in scenarios else [(name, scenarios[name]) for name in ("runner-green", "runner-failure", "runner-timeout", "runner-scope-violation")]
    results = {}
    for name, fn in selected:
        value = fn()
        results[name] = value.snapshot.to_dict() if hasattr(value, "snapshot") else value.to_dict()
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
