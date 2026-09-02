import json
import os
import re
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from orchestrator.objective_runner import (
    CodexResult,
    DurableRun,
    GitHubAppClient,
    ObjectiveProfile,
    ObjectiveRunError,
    ObjectiveRunner,
    PullRequestIdentity,
    _git,
)
from orchestrator.reviewer import Finding, ReviewResult, Severity, Verdict


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


def profile(source: Path, *, checks=(('python3', '-c', "print('ok')"),), cycles=2) -> ObjectiveProfile:
    return ObjectiveProfile(
        repository="owner/repo", repository_path=str(source), base_branch="main",
        allowed_paths=("src",), required_checks=checks, max_cycles=cycles,
    )


class EditingCodex:
    def __init__(self, edits):
        self.edits = list(edits)
        self.calls = 0

    def execute(self, prompt, workspace, profile):
        edit = self.edits[self.calls]
        self.calls += 1
        edit(workspace)
        return CodexResult("done")


class FakeGitHub:
    def __init__(self):
        self.head = None
        self.override_head = None
        self.comments = []
        self.pr_body = None

    def push(self, workspace, repository, branch):
        self.pushed_repository = repository
        self.head = git(workspace, "rev-parse", "HEAD")

    def create_pr(self, workspace, repository, branch, base, title, body):
        self.pr_body = body
        return PullRequestIdentity(41, "https://example.invalid/pull/41", repository, branch)

    def head_sha(self, workspace, repository, pr_number):
        return self.override_head or self.head

    def comment(self, workspace, repository, pr_number, body):
        self.comments.append(body)


class CallableReviewer:
    def __init__(self, action):
        self.action = action
        self.requests = []

    def review(self, request):
        self.requests.append(request)
        return self.action(request, len(self.requests))


def approved(request, _cycle):
    return ReviewResult(request.review_id, request.head_sha, Verdict.APPROVED, summary="approved")


def change_app(text):
    return lambda workspace: (workspace / "src" / "app.txt").write_text(text, encoding="utf-8")


def finding_result(request, finding_id="f-1"):
    finding = Finding(finding_id, Severity.P1, "Defect", "A correctness defect exists.",
                      "src/app.txt", 1, "correctness", "Correct the implementation.")
    return ReviewResult(request.review_id, request.head_sha, Verdict.CHANGES_REQUESTED, (finding,), "fix")


class raises:
    def __init__(self, error, match=None):
        self.error = error
        self.match = match

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, _traceback):
        if error_type is None or not issubclass(error_type, self.error):
            return False
        if self.match and not re.search(self.match, str(error)):
            raise AssertionError(f"exception did not match {self.match!r}: {error}")
        return True


def check_approved_exact_sha_stops_for_human_merge(tmp_path):
    source = source_repo(tmp_path)
    github = FakeGitHub()
    reviewer = CallableReviewer(approved)
    outcome = ObjectiveRunner(EditingCodex([change_app("implemented\n")]), reviewer, github).execute(
        "Change the app", profile(source), tmp_path / "state", "run-1")

    assert outcome.state == "human_merge_approval_required"
    assert reviewer.requests[0].head_sha == outcome.head_sha == github.head
    assert github.comments and outcome.head_sha in github.comments[0]
    assert "Closes #" not in github.pr_body
    assert json.loads((tmp_path / "state/run-1/result.json").read_text())["state"] == outcome.state
    events = [json.loads(line) for line in (tmp_path / "state/run-1/events.jsonl").read_text().splitlines()]
    changeset = next(item for item in events if item["event"] == "CHANGESET_VALIDATED")
    assert changeset["details"]["paths"] == ["src/app.txt"]
    reviews = [json.loads(line) for line in (tmp_path / "state/run-1/reviews.jsonl").read_text().splitlines()]
    assert reviews[0]["verdict"] == "approved"
    assert reviews[0]["reviewed_head_sha"] == outcome.head_sha
    assert reviews[0]["diff_digest"] == reviewer.requests[0].diff_digest
    assert not (tmp_path / "state/run-1/workspace").exists()


def check_requested_fix_is_validated_pushed_and_rereviewed(tmp_path):
    source = source_repo(tmp_path)
    github = FakeGitHub()
    reviewer = CallableReviewer(lambda request, cycle: finding_result(request) if cycle == 1 else approved(request, cycle))
    codex = EditingCodex([change_app("defect\n"), change_app("fixed\n")])
    outcome = ObjectiveRunner(codex, reviewer, github).execute(
        "Change the app", profile(source), tmp_path / "state", "run-2")

    assert outcome.state == "human_merge_approval_required"
    assert outcome.review_cycles == 2
    assert reviewer.requests[0].head_sha != reviewer.requests[1].head_sha
    assert codex.calls == 2


def check_unexpected_remote_head_fails_closed(tmp_path):
    source = source_repo(tmp_path)
    github = FakeGitHub()
    github.override_head = "f" * 40
    reviewer = CallableReviewer(approved)
    outcome = ObjectiveRunner(EditingCodex([change_app("implemented\n")]), reviewer, github).execute(
        "Change the app", profile(source), tmp_path / "state", "run-3")

    assert outcome.state == "human_decision_required"
    assert "head changed" in outcome.reason
    assert not reviewer.requests


def check_red_review_never_enters_fix_loop(tmp_path):
    source = source_repo(tmp_path)
    github = FakeGitHub()

    def red(request, _cycle):
        return ReviewResult(request.review_id, request.head_sha, Verdict.HUMAN_DECISION_REQUIRED,
                            summary="risk", risk="red", requires_human=True)

    codex = EditingCodex([change_app("implemented\n")])
    outcome = ObjectiveRunner(codex, CallableReviewer(red), github).execute(
        "Change the app", profile(source), tmp_path / "state", "run-4")
    assert outcome.state == "human_decision_required"
    assert codex.calls == 1


def check_out_of_scope_edit_fails_without_pr(tmp_path):
    source = source_repo(tmp_path)
    github = FakeGitHub()

    def edit_outside(workspace):
        (workspace / "forbidden.txt").write_text("no\n", encoding="utf-8")

    with raises(ObjectiveRunError):
        ObjectiveRunner(EditingCodex([edit_outside]), CallableReviewer(approved), github).execute(
            "Change the app", profile(source), tmp_path / "state", "run-5")
    assert github.head is None
    assert json.loads((tmp_path / "state/run-5/result.json").read_text())["state"] == "human_decision_required"


def check_failed_validation_fails_without_pr(tmp_path):
    source = source_repo(tmp_path)
    github = FakeGitHub()
    failing = (("python3", "-c", "raise SystemExit(1)"),)
    with raises(ObjectiveRunError):
        ObjectiveRunner(EditingCodex([change_app("implemented\n")]), CallableReviewer(approved), github).execute(
            "Change the app", profile(source, checks=failing), tmp_path / "state", "run-6")
    assert github.head is None


def check_mutating_validation_fails_without_pr(tmp_path):
    source = source_repo(tmp_path)
    checker = source / "src" / "mutating_check.py"
    checker.write_text("from pathlib import Path\nPath('src/generated.txt').write_text('unexpected')\n", encoding="utf-8")
    git(source, "add", "src/mutating_check.py")
    git(source, "commit", "-m", "add mutating check")
    git(source, "push", "origin", "main")
    github = FakeGitHub()
    checks = (("python3", "src/mutating_check.py"),)
    with raises(ObjectiveRunError, match="mutated the workspace"):
        ObjectiveRunner(EditingCodex([change_app("implemented\n")]), CallableReviewer(approved), github).execute(
            "Change the app", profile(source, checks=checks), tmp_path / "state", "run-mutation")
    assert github.head is None


def check_ignored_runtime_artifacts_are_not_changes(tmp_path):
    source = source_repo(tmp_path)
    (source / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    git(source, "add", ".gitignore")
    git(source, "commit", "-m", "ignore Python runtime artifacts")
    git(source, "push", "origin", "main")

    def edit_with_cache(workspace):
        (workspace / "src" / "app.txt").write_text("implemented\n", encoding="utf-8")
        cache = workspace / "src" / "__pycache__"
        cache.mkdir()
        (cache / "app.cpython-312.pyc").write_bytes(b"runtime-only")

    github = FakeGitHub()
    outcome = ObjectiveRunner(EditingCodex([edit_with_cache]), CallableReviewer(approved), github).execute(
        "Change the app", profile(source), tmp_path / "state", "run-ignored")
    assert outcome.state == "human_merge_approval_required"
    events = [json.loads(line) for line in (tmp_path / "state/run-ignored/events.jsonl").read_text().splitlines()]
    changeset = next(item for item in events if item["event"] == "CHANGESET_VALIDATED")
    assert changeset["details"]["paths"] == ["src/app.txt"]


def check_tracked_deletion_survives_ignored_artifact_filter(tmp_path):
    source = source_repo(tmp_path)
    (source / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    git(source, "add", ".gitignore")
    git(source, "commit", "-m", "ignore Python runtime artifacts")
    git(source, "push", "origin", "main")

    def delete_with_cache(workspace):
        (workspace / "src" / "app.txt").unlink()
        cache = workspace / "src" / "__pycache__"
        cache.mkdir()
        (cache / "app.cpython-312.pyc").write_bytes(b"runtime-only")

    outcome = ObjectiveRunner(EditingCodex([delete_with_cache]), CallableReviewer(approved), FakeGitHub()).execute(
        "Remove the app file", profile(source), tmp_path / "state", "run-deletion")
    assert outcome.state == "human_merge_approval_required"
    events = [json.loads(line) for line in (tmp_path / "state/run-deletion/events.jsonl").read_text().splitlines()]
    changeset = next(item for item in events if item["event"] == "CHANGESET_VALIDATED")
    assert changeset["details"]["paths"] == ["src/app.txt"]


def check_github_push_uses_trusted_repository_url(_tmp_path):
    calls = []

    def capture(workspace, *args, **kwargs):
        calls.append((workspace, args, kwargs))
        return ""

    with patch("orchestrator.objective_runner._git", side_effect=capture):
        GitHubAppClient().push(Path("/tmp/workspace"), "owner/repo", "codex/run-1")

    assert calls == [
        (
            Path("/tmp/workspace"),
            ("push", "https://github.com/owner/repo.git", "HEAD:refs/heads/codex/run-1"),
            {"timeout": 120},
        )
    ]

    with patch("orchestrator.objective_runner._git") as mocked:
        try:
            GitHubAppClient().push(Path("/tmp/workspace"), "owner/repo?token=unsafe", "codex/run-1")
        except ObjectiveRunError:
            pass
        else:
            raise AssertionError("unsafe repository identity was accepted")
        mocked.assert_not_called()


def check_github_http_error_fails_closed(_tmp_path):
    client = GitHubAppClient()
    error = urllib.error.HTTPError("https://api.github.com/test", 403, "forbidden", {}, None)
    with patch.object(client, "_credential", return_value=("app", "secret")):
        with patch("urllib.request.urlopen", side_effect=error):
            with raises(ObjectiveRunError, match="failed closed"):
                client._request(Path("/tmp"), "GET", "https://api.github.com/test")


def check_pr_head_is_owner_qualified_and_verified(_tmp_path):
    client = GitHubAppClient()
    response = {
        "number": 41,
        "html_url": "https://example.invalid/pull/41",
        "head": {"ref": "codex/run-1", "repo": {"full_name": "owner/repo"}},
    }
    with patch.object(client, "_request", return_value=response) as request:
        identity = client.create_pr(Path("/tmp"), "owner/repo", "codex/run-1", "main", "title", "body")
    assert identity.head_repository == "owner/repo"
    assert request.call_args.args[3]["head"] == "owner:codex/run-1"

    response["head"] = {"ref": "codex/run-1", "repo": {"full_name": "other/repo"}}
    with patch.object(client, "_request", return_value=response):
        with raises(ObjectiveRunError, match="invalid PR identity"):
            client.create_pr(Path("/tmp"), "owner/repo", "codex/run-1", "main", "title", "body")


def check_git_subprocess_excludes_ambient_secrets(tmp_path):
    completed = subprocess.CompletedProcess(["git", "status"], 0, stdout="", stderr="")
    with patch.dict(os.environ, {"OPENAI_API_KEY": "must-not-reach-git"}):
        with patch("subprocess.run", return_value=completed) as run:
            _git(tmp_path, "status")
    child_environment = run.call_args.kwargs["env"]
    assert "OPENAI_API_KEY" not in child_environment
    assert set(child_environment).issubset({"HOME", "PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "CODEX_HOME"})
    assert "core.hooksPath=/dev/null" in run.call_args.args[0]
    assert "commit.gpgSign=false" in run.call_args.args[0]


def check_git_failure_names_only_trusted_operation(tmp_path):
    failed = subprocess.CompletedProcess(["git"], 1, stdout="sensitive output", stderr="secret-token")
    with patch("subprocess.run", return_value=failed):
        with raises(ObjectiveRunError, match="^git commit failed closed$"):
            _git(tmp_path, "commit", "-m", "untrusted objective text")


def check_terminal_run_id_cannot_be_reused(tmp_path):
    source = source_repo(tmp_path)
    runner = ObjectiveRunner(EditingCodex([change_app("implemented\n")]), CallableReviewer(approved), FakeGitHub())
    runner.execute("Change the app", profile(source), tmp_path / "state", "run-7")
    with raises(ObjectiveRunError, match="terminal result"):
        DurableRun(tmp_path / "state", "run-7")


def check_repeated_finding_stops_without_a_second_fix(tmp_path):
    source = source_repo(tmp_path)
    github = FakeGitHub()
    reviewer = CallableReviewer(lambda request, _cycle: finding_result(request))
    codex = EditingCodex([change_app("defect\n"), change_app("still-defect\n")])
    outcome = ObjectiveRunner(codex, reviewer, github).execute(
        "Change the app", profile(source), tmp_path / "state", "run-repeat")
    assert outcome.state == "human_decision_required"
    assert "repeated" in outcome.reason
    assert codex.calls == 2


def check_incomplete_run_requires_human_recovery(tmp_path):
    run_dir = tmp_path / "state" / "run-8"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text('{"event":"RUN_STARTED"}\n', encoding="utf-8")
    with raises(ObjectiveRunError, match="incomplete prior run"):
        DurableRun(tmp_path / "state", "run-8")
    result = json.loads((run_dir / "result.json").read_text())
    assert result["state"] == "human_decision_required"


class TestObjectiveRunner(unittest.TestCase):
    def invoke(self, check):
        with tempfile.TemporaryDirectory() as directory:
            check(Path(directory))

    def test_approved_exact_sha_stops_for_human_merge(self):
        self.invoke(check_approved_exact_sha_stops_for_human_merge)

    def test_requested_fix_is_validated_pushed_and_rereviewed(self):
        self.invoke(check_requested_fix_is_validated_pushed_and_rereviewed)

    def test_unexpected_remote_head_fails_closed(self):
        self.invoke(check_unexpected_remote_head_fails_closed)

    def test_red_review_never_enters_fix_loop(self):
        self.invoke(check_red_review_never_enters_fix_loop)

    def test_out_of_scope_edit_fails_without_pr(self):
        self.invoke(check_out_of_scope_edit_fails_without_pr)

    def test_failed_validation_fails_without_pr(self):
        self.invoke(check_failed_validation_fails_without_pr)

    def test_mutating_validation_fails_without_pr(self):
        self.invoke(check_mutating_validation_fails_without_pr)

    def test_ignored_runtime_artifacts_are_not_changes(self):
        self.invoke(check_ignored_runtime_artifacts_are_not_changes)

    def test_tracked_deletion_survives_ignored_artifact_filter(self):
        self.invoke(check_tracked_deletion_survives_ignored_artifact_filter)

    def test_github_push_uses_trusted_repository_url(self):
        self.invoke(check_github_push_uses_trusted_repository_url)

    def test_github_http_error_fails_closed(self):
        self.invoke(check_github_http_error_fails_closed)

    def test_pr_head_is_owner_qualified_and_verified(self):
        self.invoke(check_pr_head_is_owner_qualified_and_verified)

    def test_git_subprocess_excludes_ambient_secrets(self):
        self.invoke(check_git_subprocess_excludes_ambient_secrets)

    def test_git_failure_names_only_trusted_operation(self):
        self.invoke(check_git_failure_names_only_trusted_operation)

    def test_terminal_run_id_cannot_be_reused(self):
        self.invoke(check_terminal_run_id_cannot_be_reused)

    def test_repeated_finding_stops_without_a_second_fix(self):
        self.invoke(check_repeated_finding_stops_without_a_second_fix)

    def test_incomplete_run_requires_human_recovery(self):
        self.invoke(check_incomplete_run_requires_human_recovery)
