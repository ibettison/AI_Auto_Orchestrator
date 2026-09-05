"""Tests for free OpenCode/OpenRouter Stage 1 adapters — no OpenAI usage."""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.objective_runner import (
    CodexResult,
    ObjectiveProfile,
    ObjectiveRunError,
    ObjectiveRunner,
    PullRequestIdentity,
)
from orchestrator.opencode_runner import OpenCodeExecutor, OpencodeSelection, OpenCodeReviewer
from orchestrator.reviewer import ProviderFailure, ReviewError, ReviewRequest

# Harmless explicit fake binary so tests never depend on HOME, PATH, the
# invoking user, or an installed opencode binary.
TEST_BINARY = "/test/opencode"


def completed(text="", code=0):
    return subprocess.CompletedProcess(["opencode", "run"], code, text, "")


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def source_repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", remote], check=True, capture_output=True)
    source = tmp_path / "source"
    subprocess.run(["git", "clone", remote, source], check=True, capture_output=True)
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.invalid")
    (source / "src").mkdir()
    (source / "src" / "app.txt").write_text("base\n", encoding="utf-8")
    git(source, "add", "src/app.txt")
    git(source, "commit", "-m", "base")
    git(source, "push", "origin", "main")
    return source


def profile(source: Path, **overrides) -> ObjectiveProfile:
    values = dict(
        repository="owner/repo", repository_path=str(source), base_branch="main",
        allowed_paths=("src",), required_checks=(("python3", "-c", "print('ok')"),), max_cycles=2,
    )
    values.update(overrides)
    return ObjectiveProfile(**values)


class FakeGitHub:
    def __init__(self):
        self.head = None
        self.override_head = None
        self.comments = []
        self.pushes = 0

    def push(self, workspace, repository, branch):
        self.pushes += 1
        self.head = git(workspace, "rev-parse", "HEAD")

    def create_pr(self, workspace, repository, branch, base, title, body):
        return PullRequestIdentity(41, "https://example.invalid/pull/41", repository, branch)

    def head_sha(self, workspace, repository, pr_number):
        return self.override_head or self.head

    def comment(self, workspace, repository, pr_number, body):
        self.comments.append(body)


APPROVED_JSON = {"review_id": "r", "reviewed_head_sha": "h", "verdict": "approved",
                 "findings": [], "summary": "ok", "risk": "green", "requires_human": False}


def review_json_for(prompt_argv, verdict="approved", findings=(), sha=None):
    prompt = next(arg for arg in prompt_argv if "head_sha" in arg)
    head = sha or re.search(r'"head_sha": "([0-9a-f]+)"', prompt).group(1)
    review_id = re.search(r'"review_id": "([^"]+)"', prompt).group(1)
    body = dict(APPROVED_JSON, review_id=review_id, reviewed_head_sha=head,
                verdict=verdict, findings=list(findings))
    return json.dumps(body)


def finding_json(fid="f-1"):
    return {"finding_id": fid, "severity": "P1", "title": "Defect", "description": "A defect exists.",
            "path": "src/app.txt", "line": 1, "category": "correctness", "remediation": "Fix it."}


class ExecutorTests(unittest.TestCase):
    def test_successful_bounded_execution(self):
        seen = {}

        def runner(argv, cwd, env, timeout):
            seen.update(argv=argv, cwd=cwd, env=env, timeout=timeout)
            return completed('{"session_id": "ses_exec123"}\ndone')

        workspace = Path("/tmp/ws")
        result = OpenCodeExecutor("nebuis/gpt-5-nano", runner, TEST_BINARY).execute(
            "do it", workspace, profile(Path("/tmp")))
        self.assertIsInstance(result, CodexResult)
        self.assertEqual(seen["argv"][0], TEST_BINARY)
        self.assertEqual(seen["argv"][1], "run")
        self.assertIn("do it", seen["argv"])
        self.assertIn("--model", seen["argv"])
        self.assertEqual(seen["cwd"], workspace)
        self.assertEqual(seen["timeout"], 1200)

    def test_session_continuity_for_fix_cycle(self):
        calls = []

        def runner(argv, cwd, env, timeout):
            calls.append(list(argv))
            if len(calls) == 1:
                return completed('{"type":"session.created","session_id": "ses_fix999"}\nok')
            return completed("fixed")

        executor = OpenCodeExecutor(runner=runner, binary=TEST_BINARY)
        workspace, prof = Path("/tmp/ws"), profile(Path("/tmp"))
        executor.execute("implement", workspace, prof)
        self.assertEqual(executor.session_id, "ses_fix999")
        executor.execute("fix", workspace, prof)
        self.assertIn("--session", calls[1])
        self.assertEqual(calls[1][calls[1].index("--session") + 1], "ses_fix999")
        self.assertNotIn("--session", calls[0])

    def test_fresh_session_when_no_id_observable(self):
        calls = []

        def runner(argv, cwd, env, timeout):
            calls.append(list(argv))
            return completed("plain text, no id")

        executor = OpenCodeExecutor(runner=runner, binary=TEST_BINARY)
        prof = profile(Path("/tmp"))
        executor.execute("a", Path("/tmp/ws"), prof)
        executor.execute("b", Path("/tmp/ws"), prof)
        self.assertIsNone(executor.session_id)
        self.assertNotIn("--session", calls[1])

    def test_default_binary_resolution_preserved(self):
        seen = {}

        def runner(argv, cwd, env, timeout):
            seen["argv"] = list(argv)
            return completed("ok")

        with mock.patch("orchestrator.opencode_runner._opencode_binary", return_value="/resolved/opencode"):
            OpenCodeExecutor(runner=runner).execute("x", Path("/tmp/ws"), profile(Path("/tmp")))
        self.assertEqual(seen["argv"][0], "/resolved/opencode")

    def test_binary_resolver_callable_honored(self):
        seen = {}

        def runner(argv, cwd, env, timeout):
            seen["argv"] = list(argv)
            return completed("ok")

        OpenCodeExecutor(runner=runner, binary=lambda: "/custom/opencode").execute(
            "x", Path("/tmp/ws"), profile(Path("/tmp")))
        self.assertEqual(seen["argv"][0], "/custom/opencode")

    def test_timeout_fails_closed(self):
        def runner(argv, cwd, env, timeout):
            raise subprocess.TimeoutExpired(argv, timeout)

        with self.assertRaises(ObjectiveRunError):
            OpenCodeExecutor(runner=runner, binary=TEST_BINARY).execute("x", Path("/tmp/ws"), profile(Path("/tmp")))

    def test_nonzero_exit_fails_closed(self):
        with self.assertRaises(ObjectiveRunError):
            OpenCodeExecutor(runner=lambda *a: completed("nope", 3), binary=TEST_BINARY).execute("x", Path("/tmp/ws"), profile(Path("/tmp")))

    def test_output_cap_fails_closed(self):
        prof = profile(Path("/tmp"), max_output_bytes=16)
        oversized = '{"type": "text", "text": "' + "y" * 100 + '"}'
        with self.assertRaises(ObjectiveRunError):
            OpenCodeExecutor(runner=lambda *a: completed(oversized), binary=TEST_BINARY).execute("x", Path("/tmp/ws"), prof)

    def test_bounded_plain_fallback_succeeds(self):
        prof = profile(Path("/tmp"), max_output_bytes=16)
        result = OpenCodeExecutor(runner=lambda *a: completed("x" * 16), binary=TEST_BINARY).execute("x", Path("/tmp/ws"), prof)
        self.assertEqual(result.final_response, "x" * 16)

    def test_oversized_plain_fallback_fails_closed(self):
        prof = profile(Path("/tmp"), max_output_bytes=16)
        with self.assertRaises(ObjectiveRunError):
            OpenCodeExecutor(runner=lambda *a: completed("x" * 17), binary=TEST_BINARY).execute("x", Path("/tmp/ws"), prof)
        with self.assertRaises(ProviderFailure):
            OpenCodeReviewer(max_output_bytes=16, runner=lambda *a: completed("y" * 17), binary=TEST_BINARY).review(
                ReviewerTests().request())

    def test_openai_api_key_never_passed(self):
        seen = {}

        def runner(argv, cwd, env, timeout):
            seen["env"] = dict(env)
            return completed("ok")

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sentinel-must-not-pass"}):
            OpenCodeExecutor(runner=runner, binary=TEST_BINARY).execute("x", Path("/tmp/ws"), profile(Path("/tmp")))
        self.assertNotIn("OPENAI_API_KEY", seen["env"])
        self.assertNotIn("sentinel-must-not-pass", json.dumps(seen["env"]))


class ReviewerTests(unittest.TestCase):
    def request(self, **overrides):
        values = dict(review_id="rev-1", run_id="run-1", repository="/tmp/repo", objective="obj",
                      base_sha="a" * 40, head_sha="b" * 40, diff="diff", diff_digest="d" * 64,
                      validation_evidence={}, cycle=1)
        values["diff_digest"] = __import__("hashlib").sha256(b"diff").hexdigest()
        values.update(overrides)
        return ReviewRequest(**values)

    def test_approved_review(self):
        reviewer = OpenCodeReviewer("free/model", runner=lambda *a: completed(review_json_for(a[0])), binary=TEST_BINARY)
        result = reviewer.review(self.request())
        self.assertEqual(result.verdict.value, "approved")
        self.assertEqual(result.reviewed_head_sha, "b" * 40)

    def test_reviewer_uses_independent_session_in_scratch(self):
        seen = {}

        def runner(argv, cwd, env, timeout):
            seen.update(argv=argv, cwd=Path(cwd))
            return completed(review_json_for(argv))

        workspace = Path("/tmp/objective-workspace")
        reviewer = OpenCodeReviewer(runner=runner, binary=TEST_BINARY)
        reviewer.review(self.request())
        self.assertNotIn("--session", seen["argv"])
        self.assertNotEqual(seen["cwd"], workspace)
        self.assertIn("opencode-review-", str(seen["cwd"]))

    def test_malformed_json_fails_closed_after_two_attempts(self):
        calls = []

        def runner(argv, cwd, env, timeout):
            calls.append(argv)
            return completed("not json at all {{{{")

        with self.assertRaises(ProviderFailure):
            OpenCodeReviewer(runner=runner, binary=TEST_BINARY).review(self.request())
        self.assertEqual(len(calls), 2)

    def test_wrong_reviewed_sha_fails_closed(self):
        calls = []

        def runner(argv, cwd, env, timeout):
            calls.append(argv)
            return completed(review_json_for(argv, sha="c" * 40))

        with self.assertRaises(ReviewError):
            OpenCodeReviewer(runner=runner, binary=TEST_BINARY).review(self.request())
        self.assertEqual(len(calls), 1)

    def test_reviewer_timeout_output_cap_and_exit_fail_closed(self):
        def timeout_runner(*a):
            raise subprocess.TimeoutExpired(["opencode"], 1)

        with self.assertRaises(ProviderFailure):
            OpenCodeReviewer(runner=timeout_runner, binary=TEST_BINARY).review(self.request())
        with self.assertRaises(ProviderFailure):
            OpenCodeReviewer(runner=lambda *a: completed("x", 2), binary=TEST_BINARY).review(self.request())
        oversized = '{"type": "text", "text": "' + "z" * 50 + '"}'
        with self.assertRaises(ProviderFailure):
            OpenCodeReviewer(max_output_bytes=4, runner=lambda *a: completed(oversized), binary=TEST_BINARY).review(self.request())


class NoFallbackTests(unittest.TestCase):
    def test_opencode_path_never_touches_openai(self):
        source = Path(__file__).resolve().parent.parent.joinpath("orchestrator", "opencode_runner.py").read_text()
        self.assertIsNone(re.search(r"^\s*(import openai|from openai|import openai_codex|from openai_codex)\b", source, re.M))
        self.assertNotIn("OPENAI_API_KEY", [line.split("=")[0] for line in source.splitlines() if "getenv" in line or "environ[" in line])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            with mock.patch("orchestrator.reviewer.OpenAIResponsesReviewer.from_environment",
                             side_effect=AssertionError("must not consult OpenAI")):
                OpenCodeExecutor(runner=lambda *a: completed("ok"), binary=TEST_BINARY).execute("x", Path("/tmp"), profile(Path("/tmp")))
                OpenCodeReviewer(runner=lambda *a: completed(review_json_for(a[0])), binary=TEST_BINARY).review(
                    ReviewerTests().request())

    def test_selection_factory(self):
        selection = OpencodeSelection("opencode", "opencode", "free/model")
        self.assertIsInstance(selection.make_executor(), OpenCodeExecutor)
        self.assertIsInstance(selection.make_reviewer(), OpenCodeReviewer)
        with self.assertRaises(ObjectiveRunError):
            OpencodeSelection("other", "opencode").make_executor()

    def test_no_merge_or_deploy_path(self):
        source = Path(__file__).resolve().parent.parent.joinpath("orchestrator", "opencode_runner.py").read_text().lower()
        self.assertNotIn("gh pr merge", source)
        self.assertNotIn("deploy", source)


class LoopTests(unittest.TestCase):
    def run_loop(self, tmp_path, reviewer_behavior, run_id):
        source = source_repo(tmp_path)
        github = FakeGitHub()
        edit_calls, review_calls = [], []

        def codex_runner(argv, cwd, env, timeout):
            edit_calls.append(list(argv))
            step = len(edit_calls)
            (Path(cwd) / "src" / "app.txt").write_text("defect\n" if step == 1 else "fixed\n", encoding="utf-8")
            if step == 1:
                return completed('{"session_id": "ses_loop42"}\nimplemented')
            self.assertIn("--session", argv)
            return completed("fixed")

        def review_runner(argv, cwd, env, timeout):
            review_calls.append(list(argv))
            verdict = reviewer_behavior(len(review_calls))
            findings = [finding_json()] if verdict == "changes_requested" else []
            return completed(review_json_for(argv, verdict=verdict, findings=findings))

        executor = OpenCodeExecutor("free/model", codex_runner, TEST_BINARY)
        reviewer = OpenCodeReviewer("free/model", runner=review_runner, binary=TEST_BINARY)
        outcome = ObjectiveRunner(executor, reviewer, github).execute(
            "Change the app", profile(source), tmp_path / "state", run_id)
        return outcome, github, edit_calls, review_calls

    def invoke(self, behavior, run_id):
        with tempfile.TemporaryDirectory() as directory:
            return self.run_loop(Path(directory), behavior, run_id)

    def test_changes_requested_fix_same_pr_rereview(self):
        outcome, github, edit_calls, review_calls = self.invoke(lambda n: "changes_requested" if n == 1 else "approved", "loop-fix")
        self.assertEqual(outcome.state, "human_merge_approval_required")
        self.assertEqual(outcome.review_cycles, 2)
        self.assertEqual(len(edit_calls), 2)
        self.assertEqual(github.pushes, 2)
        self.assertEqual(outcome.pr_number, 41)
        self.assertEqual(len(review_calls), 2)

    def test_approved_stops_for_human_merge(self):
        outcome, github, edit_calls, review_calls = self.invoke(lambda n: "approved", "loop-approved")
        self.assertEqual(outcome.state, "human_merge_approval_required")
        self.assertEqual(len(edit_calls), 1)
        self.assertEqual(len(review_calls), 1)
        self.assertIn(outcome.head_sha, github.comments[0])


class TransportBoundsTests(unittest.TestCase):
    """Raw transport vs semantic result caps (commissioning-stage1-004)."""

    def large_valid_stream(self, final_text, session="ses_stream1"):
        envelope = '{"type": "reasoning", "text": "' + "r" * 2000 + '", "metadata": {"k": "v"}}\n'
        lines = [envelope * 250]  # ~500KB of transport noise
        lines.append('{"type": "text", "session_id": "%s"}\n' % session)
        lines.append('{"type": "text", "text": %s}\n' % json.dumps(final_text))
        return "".join(lines)

    def test_large_valid_stream_succeeds_when_result_bounded(self):
        stream = self.large_valid_stream("done")
        self.assertGreater(len(stream.encode()), 256 * 1024)
        executor = OpenCodeExecutor(runner=lambda *a: completed(stream), binary=TEST_BINARY)
        result = executor.execute("x", Path("/tmp/ws"), profile(Path("/tmp")))
        self.assertEqual(result.final_response, "done")
        self.assertEqual(executor.session_id, "ses_stream1")

    def test_reviewer_extracts_result_from_large_stream(self):
        payload = {"review_id": "rev-9", "reviewed_head_sha": "b" * 40, "verdict": "approved",
                   "findings": [], "summary": "ok", "risk": "green", "requires_human": False}
        stream = self.large_valid_stream(json.dumps(payload), session="ses_rev9")
        reviewer = OpenCodeReviewer(runner=lambda *a: completed(stream), binary=TEST_BINARY)
        request = ReviewerTests().request(review_id="rev-9")
        result = reviewer.review(request)
        self.assertEqual(result.verdict.value, "approved")
        self.assertEqual(result.reviewed_head_sha, "b" * 40)

    def test_runaway_raw_transport_fails_closed(self):
        import orchestrator.opencode_runner as module

        big = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 3000000)"]
        with mock.patch.object(module, "_MAX_TRANSPORT_BYTES", 1024):
            with self.assertRaises(ObjectiveRunError):
                module._default_runner(big, Path("/tmp"), {"PATH": os.environ.get("PATH", "")}, 30)

    def test_real_timeout_kills_bounded(self):
        import orchestrator.opencode_runner as module

        sleepy = [sys.executable, "-c", "import time; time.sleep(30)"]
        with self.assertRaises(ObjectiveRunError):
            module._default_runner(sleepy, Path("/tmp"), {"PATH": os.environ.get("PATH", "")}, 1)


class ReadmeScopeTests(unittest.TestCase):
    """Exact-file allowed paths (commissioning-stage1-003 finding)."""

    def test_readme_exact_file_allowed(self):
        from orchestrator.runner import PathPolicy

        PathPolicy(("docs", "README.md")).verify(["README.md"])
        PathPolicy(("docs", "README.md")).verify(["docs/pr-poller.md"])

    def test_docs_only_profile_rejects_readme(self):
        from orchestrator.runner import PathPolicy, PolicyViolation

        with self.assertRaises(PolicyViolation):
            PathPolicy(("docs",)).verify(["README.md"])


if __name__ == "__main__":
    unittest.main()
