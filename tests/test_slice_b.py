import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from orchestrator.contract import EventType, Risk, State
from orchestrator.integration import RunnerCoordinator
from orchestrator.state_machine import Orchestrator
from orchestrator.runner import (
    AdapterResult,
    BoundedRunner,
    CommandPolicy,
    FakeCodexAdapter,
    PathPolicy,
    PolicyViolation,
    RunnerConfig,
    RunLeaseRegistry,
    UnsafeConfiguration,
)


class SliceBTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="slice-b-test-")
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git(["init", "-q"])
        self.git(["config", "user.name", "Slice B Test"])
        self.git(["config", "user.email", "slice-b@example.invalid"])
        (self.repo / "src").mkdir()
        (self.repo / "src" / "README.txt").write_text("clean\n")
        self.git(["add", "."])
        self.git(["commit", "-qm", "initial"])
        self.sha = self.git(["rev-parse", "HEAD"]).strip()

    def tearDown(self):
        self.temp.cleanup()

    def git(self, args):
        result = subprocess.run(["git", *args], cwd=self.repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def config(self, **changes):
        values = dict(run_id="run-1", repository=str(self.repo), source_sha=self.sha,
                      allowed_paths=("src",), allowed_commands=(("python3", "-c", "print('check')"),), objective="make the bounded change")
        values.update(changes)
        return RunnerConfig(**values)

    def test_valid_unattended_run_and_cleanup(self):
        def action(objective, workspace, commands):
            (workspace / "src" / "result.txt").write_text(objective)
            commands.run(("python3", "-c", "print('check')"))
            return AdapterResult(0, "fake implementation", "")

        result = BoundedRunner(FakeCodexAdapter(action)).run(self.config())
        self.assertEqual((result.status, result.exit_code, result.files_changed), ("completed", 0, ("src/result.txt",)))
        self.assertTrue(any(record.action == "cleanup" for record in result.audit))
        self.assertEqual(result.commands_executed[0].exit_code, 0)
        self.assertEqual((self.repo / "src" / "result.txt").exists(), False)
        self.assertEqual(self.git(["status", "--porcelain"]), "")

    def test_malformed_objective_and_path_traversal_rejected(self):
        with self.assertRaises(UnsafeConfiguration):
            self.config(objective="")
        with self.assertRaises(UnsafeConfiguration):
            self.config(allowed_paths=("../src",))
        with self.assertRaises(UnsafeConfiguration):
            self.config(allowed_paths=("/tmp",))

    def test_wrong_or_stale_source_sha_fails_closed(self):
        result = BoundedRunner(FakeCodexAdapter()).run(self.config(source_sha="0" * 40))
        self.assertEqual(result.status, "failed")
        self.assertIn("exact commit", result.failure_reason)

    def test_dirty_source_checkout_is_rejected(self):
        (self.repo / "dirty.txt").write_text("uncommitted\n")
        result = BoundedRunner(FakeCodexAdapter()).run(self.config())
        self.assertEqual(result.status, "failed")
        self.assertIn("dirty", result.failure_reason)

    def test_command_allowlist_and_shell_composition_fail_closed(self):
        policy = CommandPolicy((("python3", "-c", "print('check')"),), 5)
        policy.authorize(("python3", "-c", "print('check')"))
        for attempted in (("python3", "-c", "x; y"), ("python3", "-c", "x && y"), ("python3", "-c", "x || y"), ("python3", "-c", "x | y"), ("python3", "-c", "$(id)"), ("python3", "-c", "x > out"), ("sh", "-c", "id"), ("python3", "-c", "not-allowlisted")):
            with self.assertRaises((PolicyViolation, UnsafeConfiguration)):
                policy.authorize(attempted)

    def test_out_of_scope_modification_fails(self):
        def action(_, workspace, __):
            (workspace / "forbidden.txt").write_text("no\n")
            return AdapterResult(0)

        result = BoundedRunner(FakeCodexAdapter(action)).run(self.config())
        self.assertEqual(result.status, "failed")
        self.assertIn("out-of-scope", result.failure_reason)

    def test_path_policy_rejects_traversal_even_after_worker(self):
        with self.assertRaises(UnsafeConfiguration):
            PathPolicy(("src",)).verify(("src/../secret",))

    def test_timeout_command_count_and_output_bounds(self):
        def timeout_action(_, __, commands):
            commands.run(("python3", "-c", "__import__('time').sleep(1)"))
            return AdapterResult(0)

        timed = BoundedRunner(FakeCodexAdapter(timeout_action)).run(self.config(command_timeout_seconds=0.05, allowed_commands=(("python3", "-c", "__import__('time').sleep(1)"),)))
        self.assertEqual(timed.status, "timed_out")

        def many_commands(_, __, commands):
            commands.run(("python3", "-c", "print('check')"))
            commands.run(("python3", "-c", "print('check')"))
            return AdapterResult(0)

        counted = BoundedRunner(FakeCodexAdapter(many_commands)).run(self.config(max_commands=1, allowed_commands=(("python3", "-c", "print('check')"),)))
        self.assertEqual(counted.status, "failed")
        self.assertIn("command count", counted.failure_reason)

        noisy = BoundedRunner(FakeCodexAdapter(lambda *_: AdapterResult(0, "x" * 100)),).run(self.config(max_output_size=10))
        self.assertEqual(noisy.status, "failed")

        failed_command = BoundedRunner(FakeCodexAdapter(lambda _, __, commands: (commands.run(("python3", "-c", "raise SystemExit(4)")), AdapterResult(0))[1])).run(self.config(allowed_commands=(("python3", "-c", "raise SystemExit(4)"),)))
        self.assertEqual(failed_command.status, "failed")
        self.assertIn("command failed", failed_command.failure_reason)

    def test_environment_is_allowlisted_and_network_is_denied(self):
        os.environ["SLICE_B_TEST_SECRET"] = "must-not-leak"

        def action(_, __, commands):
            check = commands.run(("python3", "-c", "print(__import__('os').environ.get('SLICE_B_TEST_SECRET', 'MISSING'))"))
            self.assertEqual(check.stdout.strip(), "MISSING")
            return AdapterResult(0)

        env_command = ("python3", "-c", "print(__import__('os').environ.get('SLICE_B_TEST_SECRET', 'MISSING'))")
        result = BoundedRunner(FakeCodexAdapter(action)).run(self.config(environment={}, allowed_commands=(env_command,)))
        self.assertEqual(result.status, "completed")
        with self.assertRaises(UnsafeConfiguration):
            self.config(network_requested=True)

    def test_fake_adapter_failure_and_interrupted_run(self):
        failed = BoundedRunner(FakeCodexAdapter(lambda *_: AdapterResult(7, stderr="failed", failure_reason="checks failed"))).run(self.config())
        self.assertEqual((failed.status, failed.exit_code, failed.failure_reason), ("failed", 7, "checks failed"))
        interrupted = BoundedRunner(FakeCodexAdapter(lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))).run(self.config(run_id="interrupted"))
        self.assertEqual((interrupted.status, interrupted.failure_reason), ("interrupted", "runner interrupted"))

    def test_duplicate_active_run_is_rejected_and_retry_after_failure_works(self):
        entered = threading.Event()
        release = threading.Event()

        def blocking(_, __, ___):
            entered.set()
            release.wait(2)
            return AdapterResult(0)

        runner = BoundedRunner(FakeCodexAdapter(blocking), RunLeaseRegistry())
        first = []
        thread = threading.Thread(target=lambda: first.append(runner.run(self.config())))
        thread.start()
        self.assertTrue(entered.wait(1))
        duplicate = runner.run(self.config())
        self.assertEqual(duplicate.failure_reason, "run already active")
        release.set()
        thread.join(2)
        self.assertEqual(first[0].status, "completed")
        retry = BoundedRunner(FakeCodexAdapter(lambda *_: AdapterResult(4, failure_reason="retryable failure")), RunLeaseRegistry()).run(self.config())
        self.assertEqual(retry.status, "failed")

    def test_structured_result_and_audit_never_include_environment_secret(self):
        result = BoundedRunner(FakeCodexAdapter()).run(self.config(environment={"SAFE": "yes"}))
        encoded = str(result.to_dict())
        self.assertIn("run-1", encoded)
        self.assertIn("workspace_created", encoded)
        self.assertNotIn("SLICE_B_TEST_SECRET", encoded)
        self.assertEqual(result.source_sha, self.sha)

    def test_slice_a_integration_success_failure_and_replay(self):
        success = RunnerCoordinator(BoundedRunner(FakeCodexAdapter())).run(self.config(run_id="integration-success"))
        self.assertEqual(success.snapshot.state, State.REVIEWING)
        replayed = Orchestrator("integration-success", self.sha).replay([
            RunnerCoordinator._event(self.config(run_id="integration-success"), 1, 0, EventType.START, objective="make the bounded change"),
            RunnerCoordinator._event(self.config(run_id="integration-success"), 2, 1, EventType.IMPLEMENTED, tests_pass=True, branch=success.runner.branch, workspace_id=success.runner.workspace_id),
        ])
        self.assertEqual(replayed, success.snapshot)
        failure = RunnerCoordinator(BoundedRunner(FakeCodexAdapter(lambda *_: AdapterResult(2, failure_reason="test failed")))).run(self.config(run_id="integration-failure"))
        self.assertEqual((failure.snapshot.state, failure.snapshot.risk), (State.BLOCKED, Risk.AMBER))
        failure_machine = Orchestrator("integration-failure", self.sha)
        failure_events = [
            RunnerCoordinator._event(self.config(run_id="integration-failure"), 1, 0, EventType.START, objective="make the bounded change"),
            RunnerCoordinator._event(self.config(run_id="integration-failure"), 2, 1, EventType.RUNNER_FAILED, tests_pass=False, failure_reason="test failed"),
        ]
        self.assertEqual(failure_machine.replay(failure_events), failure.snapshot)

    def test_red_gate_remains_intact_after_runner_integration(self):
        def red_action(_, workspace, __):
            (workspace / "src" / "result.txt").write_text("external side effect marker")
            return AdapterResult(0)

        result = RunnerCoordinator(BoundedRunner(FakeCodexAdapter(red_action))).run(self.config(run_id="red-integration"))
        self.assertEqual(result.snapshot.state, State.REVIEWING)
        gated = Orchestrator("red-integration", self.sha).replay([
            RunnerCoordinator._event(self.config(run_id="red-integration"), 1, 0, EventType.START, objective="make the bounded change"),
            RunnerCoordinator._event(self.config(run_id="red-integration"), 2, 1, EventType.IMPLEMENTED, tests_pass=True, branch=result.runner.branch, workspace_id=result.runner.workspace_id),
            RunnerCoordinator._event(self.config(run_id="red-integration"), 3, 2, EventType.REVIEW_PASSED, tests_pass=True, external_side_effect=True),
        ])
        self.assertEqual((gated.state, gated.risk, gated.red_pending), (State.HUMAN_DECISION_REQUIRED, Risk.RED, True))
        # The runner cannot approve a RED action; a later Slice A event still gates it.
        self.assertEqual(result.runner.files_changed, ("src/result.txt",))


if __name__ == "__main__":
    unittest.main()
