"""Offline scenarios for demonstrating contract behavior."""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path
from .contract import Event, EventType
from .reviewer import Finding, FakeReviewer, ProviderFailure, ReviewFixLoop, ReviewInputPreparer, ReviewResult, Severity, Verdict
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


def _review_fixture(run_id="sim-review"):
    directory, repo, base = _temporary_repo()
    (repo / "src" / "result.txt").write_text("change\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "change"], cwd=repo, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True, stdout=subprocess.PIPE, check=True).stdout.strip()
    request = ReviewInputPreparer().prepare(review_id=f"{run_id}-1", run_id=run_id, repository=str(repo), objective="offline review", base_sha=base, expected_head_sha=head, validation_evidence={"passed": True}, cycle=1)
    check = ("python3", "-c", "print('check')")
    config = RunnerConfig(run_id=run_id, repository=str(repo), source_sha=base, allowed_paths=("src",), allowed_commands=(check,), required_checks=(check,), objective="offline implementation")
    implementation = BoundedRunner(FakeCodexAdapter(lambda _, __, commands: (commands.run(check), AdapterResult(0))[1])).run(config)
    return directory, repo, request, implementation


def _review_value(result):
    return {"state": result.state.value, "reviews": len(result.review_results), "reason": result.reason}


def scenario_review_clean():
    directory, repo, request, implementation = _review_fixture("sim-review-clean")
    try:
        result = ReviewFixLoop(FakeReviewer(lambda current: ReviewResult(current.review_id, current.head_sha, Verdict.APPROVED))).execute(request, implementation)
        return _review_value(result)
    finally:
        directory.cleanup()


def scenario_review_fix():
    directory, repo, request, implementation = _review_fixture("sim-review-fix")
    try:
        finding = Finding("f-1", Severity.P1, "defect", "real defect", "src/result.txt", 1, "correctness", "fix it")
        calls = []
        def review(current):
            calls.append(current)
            return ReviewResult(current.review_id, current.head_sha, Verdict.CHANGES_REQUESTED, (finding,)) if len(calls) == 1 else ReviewResult(current.review_id, current.head_sha, Verdict.APPROVED)
        def fixer(old, _):
            (repo / "src" / "result.txt").write_text("fixed\n")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "fix"], cwd=repo, check=True)
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True, stdout=subprocess.PIPE, check=True).stdout.strip()
            check = ("python3", "-c", "print('check')")
            cfg = RunnerConfig(run_id="sim-review-fix", repository=str(repo), source_sha=head, allowed_paths=("src",), allowed_commands=(check,), required_checks=(check,), objective="offline implementation")
            run = BoundedRunner(FakeCodexAdapter(lambda _, __, commands: (commands.run(check), AdapterResult(0))[1])).run(cfg)
            next_request = ReviewInputPreparer().prepare(review_id="sim-review-fix-2", run_id="sim-review-fix", repository=str(repo), objective="offline review", base_sha=request.base_sha, expected_head_sha=head, validation_evidence={"passed": True}, cycle=2)
            return run, next_request
        return _review_value(ReviewFixLoop(FakeReviewer(review), max_cycles=2).execute(request, implementation, fixer))
    finally:
        directory.cleanup()


def scenario_review_stale():
    directory, repo, request, implementation = _review_fixture("sim-review-stale")
    try:
        def review(current):
            (repo / "src" / "stale.txt").write_text("new head\n")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "stale"], cwd=repo, check=True)
            return ReviewResult(current.review_id, current.head_sha, Verdict.APPROVED)
        return _review_value(ReviewFixLoop(FakeReviewer(review)).execute(request, implementation))
    finally:
        directory.cleanup()


def scenario_review_malformed():
    directory, repo, request, implementation = _review_fixture("sim-review-malformed")
    try:
        bad = ReviewResult(request.review_id, request.head_sha, Verdict.CHANGES_REQUESTED)
        return _review_value(ReviewFixLoop(FakeReviewer([bad])).execute(request, implementation))
    finally:
        directory.cleanup()


def scenario_review_repeat():
    directory, repo, request, implementation = _review_fixture("sim-review-repeat")
    try:
        finding = Finding("f-1", Severity.P1, "repeat", "repeat defect", "src/result.txt", 1, "correctness", "fix it")
        def fixer(old, _):
            (repo / "src" / "result.txt").write_text(f"progress {old.cycle}\n")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "progress"], cwd=repo, check=True)
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True, stdout=subprocess.PIPE, check=True).stdout.strip()
            check = ("python3", "-c", "print('check')")
            run = BoundedRunner(FakeCodexAdapter(lambda _, __, commands: (commands.run(check), AdapterResult(0))[1])).run(RunnerConfig(run_id="sim-review-repeat", repository=str(repo), source_sha=head, allowed_paths=("src",), allowed_commands=(check,), required_checks=(check,), objective="offline implementation"))
            return run, ReviewInputPreparer().prepare(review_id=f"sim-review-repeat-{old.cycle + 1}", run_id="sim-review-repeat", repository=str(repo), objective="offline review", base_sha=request.base_sha, expected_head_sha=head, validation_evidence={"passed": True}, cycle=old.cycle + 1)
        result = ReviewFixLoop(FakeReviewer(lambda current: ReviewResult(current.review_id, current.head_sha, Verdict.CHANGES_REQUESTED, (finding,))), max_cycles=3, repeat_threshold=2).execute(request, implementation, fixer)
        return _review_value(result)
    finally:
        directory.cleanup()


def scenario_review_human():
    directory, repo, request, implementation = _review_fixture("sim-review-human")
    try:
        return _review_value(ReviewFixLoop(FakeReviewer([ReviewResult(request.review_id, request.head_sha, Verdict.HUMAN_DECISION_REQUIRED, summary="ambiguous", requires_human=True)])).execute(request, implementation))
    finally:
        directory.cleanup()


def scenario_review_red():
    directory, repo, request, implementation = _review_fixture("sim-review-red")
    try:
        return _review_value(ReviewFixLoop(FakeReviewer([ReviewResult(request.review_id, request.head_sha, Verdict.APPROVED, risk="red")])).execute(request, implementation))
    finally:
        directory.cleanup()


def scenario_review_provider_failure():
    directory, repo, request, implementation = _review_fixture("sim-review-provider")
    try:
        return _review_value(ReviewFixLoop(FakeReviewer(lambda _: (_ for _ in ()).throw(ProviderFailure("offline provider failure")))).execute(request, implementation))
    finally:
        directory.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["green", "amber-fix", "red-gate", "runner", "all"], default="all")
    args = parser.parse_args()
    scenarios = {"green": scenario_green, "amber-fix": scenario_amber_fix, "red-gate": scenario_red_gate,
                 "review-clean": scenario_review_clean, "review-fix": scenario_review_fix, "review-stale": scenario_review_stale,
                 "review-malformed": scenario_review_malformed, "review-repeat": scenario_review_repeat, "review-human": scenario_review_human,
                 "review-red": scenario_review_red, "review-provider-failure": scenario_review_provider_failure}
    if args.scenario in ("runner", "all"):
        scenarios.update({"runner-green": scenario_runner_green, "runner-failure": scenario_runner_failure,
                          "runner-timeout": scenario_runner_timeout, "runner-scope-violation": scenario_runner_scope_violation})
    if args.scenario == "all":
        selected = scenarios.items()
    elif args.scenario in scenarios:
        selected = [(args.scenario, scenarios[args.scenario])]
    else:
        selected = [(name, scenarios[name]) for name in ("runner-green", "runner-failure", "runner-timeout", "runner-scope-violation")]
    results = {}
    for name, fn in selected:
        value = fn()
        results[name] = value.snapshot.to_dict() if hasattr(value, "snapshot") else value.to_dict() if hasattr(value, "to_dict") else value
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
