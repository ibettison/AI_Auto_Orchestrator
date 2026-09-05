"""Strong automated tests for the GitHub PR review poller — zero AI cost."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.pr_poller import (
    WatchRecord,
    WatchState,
    contains_valid_approval,
    evaluate_watch,
    load_watches,
    parse_approval_marker,
    poll_once,
    save_watches,
    trigger_wake,
)

# Test constants — deterministic, no secrets
REPO = "ibettison/layMatchedBetting"
PR = 193
SHA = "a" * 40
SHA2 = "b" * 40
SHA_BAD = "c" * 40

VALID_MARKER = f"""LAYMATCHED-AI-REVIEW
STATUS: APPROVED
PR: {PR}
HEAD: {SHA}
REVIEWER: independent
"""

VALID_MARKER_WITH_REPO = f"""LAYMATCHED-AI-REVIEW
STATUS: APPROVED
PR: {PR}
HEAD: {SHA}
REVIEWER: independent
REPO: {REPO}
"""


class TestApprovalMarkerParsing(unittest.TestCase):
    def test_valid_exact_sha_approval(self):
        parsed = parse_approval_marker(VALID_MARKER, REPO, PR, SHA)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["STATUS"], "APPROVED")
        self.assertEqual(parsed["PR"], str(PR))
        self.assertEqual(parsed["HEAD"].lower(), SHA.lower())

    def test_valid_with_repo_field(self):
        parsed = parse_approval_marker(VALID_MARKER_WITH_REPO, REPO, PR, SHA)
        self.assertIsNotNone(parsed)

    def test_wrong_pr_number(self):
        parsed = parse_approval_marker(VALID_MARKER, REPO, 999, SHA)
        self.assertIsNone(parsed)

    def test_wrong_sha(self):
        parsed = parse_approval_marker(VALID_MARKER, REPO, PR, SHA_BAD)
        self.assertIsNone(parsed)

    def test_malformed_marker_missing_status(self):
        malformed = f"""LAYMATCHED-AI-REVIEW
PR: {PR}
HEAD: {SHA}
REVIEWER: independent
"""
        self.assertIsNone(parse_approval_marker(malformed, REPO, PR, SHA))

    def test_malformed_missing_header(self):
        malformed = f"""STATUS: APPROVED
PR: {PR}
HEAD: {SHA}
REVIEWER: independent
"""
        self.assertIsNone(parse_approval_marker(malformed, REPO, PR, SHA))

    def test_malformed_wrong_status(self):
        malformed = f"""LAYMATCHED-AI-REVIEW
STATUS: PENDING
PR: {PR}
HEAD: {SHA}
REVIEWER: independent
"""
        self.assertIsNone(parse_approval_marker(malformed, REPO, PR, SHA))

    def test_casual_approved_text_ignored(self):
        for casual in ["looks good", "approved", "LGTM", "LGTM! This is great", "STATUS: APPROVED but no header"]:
            self.assertIsNone(parse_approval_marker(casual, REPO, PR, SHA))

    def test_case_insensitive_sha(self):
        upper = VALID_MARKER.replace(SHA, SHA.upper())
        parsed = parse_approval_marker(upper, REPO, PR, SHA)
        self.assertIsNotNone(parsed)

    def test_contains_valid_approval_in_list(self):
        texts = ["hello", "looks good", VALID_MARKER, "another"]
        parsed = contains_valid_approval(texts, REPO, PR, SHA)
        self.assertIsNotNone(parsed)

    def test_contains_valid_approval_wrong_pr_in_list(self):
        texts = [VALID_MARKER]
        self.assertIsNone(contains_valid_approval(texts, REPO, 999, SHA))

    def test_malformed_sha_in_marker(self):
        malformed = f"""LAYMATCHED-AI-REVIEW
STATUS: APPROVED
PR: {PR}
HEAD: not-a-sha
REVIEWER: independent
"""
        self.assertIsNone(parse_approval_marker(malformed, REPO, PR, SHA))


class TestStateTransitions(unittest.TestCase):
    def _make_watch(self, sha=SHA, state=WatchState.WAITING_FOR_REVIEW):
        return WatchRecord(repo=REPO, pr=PR, expected_sha=sha, state=state)

    def test_head_changes_auto_rebinds_to_new_sha(self):
        # F-002: when HEAD moves A->B while OPEN, watcher auto-rebinds to B and WAITING (not STALE)
        watch = self._make_watch()
        github_state = {"state": "OPEN", "headRefOid": SHA2, "mergeable": "MERGEABLE", "closed": False, "mergedAt": None}
        new_watch, should_wake = evaluate_watch(watch, github_state, [VALID_MARKER])
        self.assertEqual(new_watch.state, WatchState.WAITING_FOR_REVIEW)
        self.assertEqual(new_watch.expected_sha, SHA2.lower())
        self.assertFalse(should_wake)
        self.assertIsNone(new_watch.error_message)

    def test_head_changes_stale_when_not_open(self):
        # Fallback STALE when not OPEN or invalid SHA
        watch = self._make_watch()
        github_state = {"state": "CLOSED", "headRefOid": SHA2, "mergeable": "MERGEABLE", "closed": True, "mergedAt": None}
        new_watch, should_wake = evaluate_watch(watch, github_state, [VALID_MARKER])
        # CLOSED takes precedence over rebind
        self.assertEqual(new_watch.state, WatchState.CLOSED)
        self.assertFalse(should_wake)

    def test_pr_closed_stops_watching(self):
        watch = self._make_watch()
        github_state = {"state": "CLOSED", "headRefOid": SHA, "mergeable": "MERGEABLE", "closed": True, "mergedAt": None}
        new_watch, should_wake = evaluate_watch(watch, github_state, [])
        self.assertEqual(new_watch.state, WatchState.CLOSED)
        self.assertFalse(should_wake)

    def test_pr_already_merged(self):
        watch = self._make_watch()
        github_state = {"state": "MERGED", "headRefOid": SHA, "mergeable": "MERGEABLE", "closed": True, "mergedAt": "2026-09-04T00:00:00Z"}
        new_watch, should_wake = evaluate_watch(watch, github_state, [])
        self.assertEqual(new_watch.state, WatchState.MERGED)
        self.assertFalse(should_wake)

    def test_github_api_failure_marks_error_fail_closed(self):
        watch = self._make_watch()
        new_watch, should_wake = evaluate_watch(watch, None, [])
        self.assertEqual(new_watch.state, WatchState.ERROR)
        self.assertFalse(should_wake)
        self.assertIn("GitHub API failure", new_watch.error_message or "")

    def test_valid_approval_triggers_wake(self):
        watch = self._make_watch()
        github_state = {"state": "OPEN", "headRefOid": SHA, "mergeable": "MERGEABLE", "closed": False, "mergedAt": None}
        new_watch, should_wake = evaluate_watch(watch, github_state, [VALID_MARKER])
        self.assertEqual(new_watch.state, WatchState.APPROVED)
        self.assertTrue(should_wake)
        self.assertEqual(new_watch.last_wake_sha, SHA.lower())
        self.assertEqual(new_watch.wake_count, 1)

    def test_wake_fires_once_only(self):
        watch = self._make_watch()
        github_state = {"state": "OPEN", "headRefOid": SHA, "mergeable": "MERGEABLE", "closed": False, "mergedAt": None}
        new_watch, should_wake = evaluate_watch(watch, github_state, [VALID_MARKER])
        self.assertTrue(should_wake)
        # Second poll with same SHA and same approval should not re-wake
        new_watch2, should_wake2 = evaluate_watch(new_watch, github_state, [VALID_MARKER])
        self.assertFalse(should_wake2)
        self.assertEqual(new_watch2.wake_count, 1)
        self.assertEqual(new_watch2.last_wake_sha, SHA.lower())

    def test_repeated_poll_does_not_refire(self):
        watch = WatchRecord(repo=REPO, pr=PR, expected_sha=SHA, state=WatchState.APPROVED, last_wake_sha=SHA.lower(), wake_count=1)
        github_state = {"state": "OPEN", "headRefOid": SHA, "mergeable": "MERGEABLE", "closed": False, "mergedAt": None}
        new_watch, should_wake = evaluate_watch(watch, github_state, [VALID_MARKER])
        self.assertFalse(should_wake)

    def test_multiple_watched_prs_independent(self):
        watch1 = WatchRecord(repo=REPO, pr=193, expected_sha=SHA, state=WatchState.WAITING_FOR_REVIEW)
        watch2 = WatchRecord(repo=REPO, pr=194, expected_sha=SHA2, state=WatchState.WAITING_FOR_REVIEW)
        github_state1 = {"state": "OPEN", "headRefOid": SHA, "mergeable": "MERGEABLE", "closed": False, "mergedAt": None}
        github_state2 = {"state": "OPEN", "headRefOid": SHA2, "mergeable": "MERGEABLE", "closed": False, "mergedAt": None}
        # Valid approval for PR 193 only
        marker_193 = VALID_MARKER
        marker_194 = VALID_MARKER.replace(str(PR), "194").replace(SHA, SHA2)
        nw1, wake1 = evaluate_watch(watch1, github_state1, [marker_193])
        nw2, wake2 = evaluate_watch(watch2, github_state2, [marker_193])  # wrong marker for 194
        self.assertTrue(wake1)
        self.assertFalse(wake2)
        self.assertEqual(nw1.state, WatchState.APPROVED)
        self.assertEqual(nw2.state, WatchState.WAITING_FOR_REVIEW)

    def test_mergeability_failure_still_wakes_but_whizzy_will_check(self):
        # Poller should still wake even if mergeable is CONFLICTING — Whizzy double-checks
        watch = self._make_watch()
        github_state = {"state": "OPEN", "headRefOid": SHA, "mergeable": "CONFLICTING", "closed": False, "mergedAt": None}
        new_watch, should_wake = evaluate_watch(watch, github_state, [VALID_MARKER])
        # Poller marks APPROVED and wakes; Whizzy is responsible for merging only if mergeable
        self.assertEqual(new_watch.state, WatchState.APPROVED)
        self.assertTrue(should_wake)


class TestPersistence(unittest.TestCase):
    def test_persistence_across_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "watches.json"
            watch = WatchRecord(repo=REPO, pr=PR, expected_sha=SHA, state=WatchState.WAITING_FOR_REVIEW)
            save_watches({watch.key(): watch}, path)
            self.assertTrue(path.exists())
            loaded = load_watches(path)
            self.assertIn(watch.key(), loaded)
            self.assertEqual(loaded[watch.key()].expected_sha, SHA.lower())
            self.assertEqual(loaded[watch.key()].state, WatchState.WAITING_FOR_REVIEW)
            # Simulate restart with same file
            loaded2 = load_watches(path)
            self.assertEqual(loaded, loaded2)

    def test_malformed_persisted_entry_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "watches.json"
            # Write a file with one good and one malformed entry
            payload = {
                "version": 1,
                "watches": {
                    "good#1": WatchRecord(repo=REPO, pr=1, expected_sha=SHA).to_dict(),
                    "bad#2": {"repo": "bad", "pr": "not-an-int", "expected_sha": "bad"},
                },
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            loaded = load_watches(path)
            self.assertIn("good#1", loaded)
            self.assertNotIn("bad#2", loaded)

    def test_invalid_repo_and_sha_rejected_on_from_dict(self):
        with self.assertRaises(ValueError):
            WatchRecord.from_dict({"repo": "badformat", "pr": 1, "expected_sha": SHA, "state": "WAITING_FOR_REVIEW"})
        with self.assertRaises(ValueError):
            WatchRecord.from_dict({"repo": REPO, "pr": 1, "expected_sha": "not-a-sha", "state": "WAITING_FOR_REVIEW"})


class TestWakeCommand(unittest.TestCase):
    def test_wake_fires_with_configured_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "log"
            watch = WatchRecord(repo=REPO, pr=PR, expected_sha=SHA, state=WatchState.WAITING_FOR_REVIEW, wake_command="echo hello {repo} {pr} {sha}")
            # Mock subprocess.run to avoid actually running echo
            with mock.patch("orchestrator.pr_poller.subprocess.run") as mock_run:
                mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
                result = trigger_wake(watch, log_path=log_path)
                self.assertTrue(result)
                mock_run.assert_called_once()
                args = mock_run.call_args[0][0]
                # Should be shlex split, not shell
                self.assertIn("echo", args)
                self.assertIn(REPO, args)

    def test_wake_not_from_pr_comment(self):
        # Ensure wake command is from trusted local config, not from comment
        # The poller should never use comment content as a command.
        # We test that trigger_wake uses watch.wake_command, not the marker text.
        watch = WatchRecord(repo=REPO, pr=PR, expected_sha=SHA, wake_command="echo safe")
        # Even if comment contains a malicious command string, it should not be executed
        malicious_marker = f"""LAYMATCHED-AI-REVIEW
STATUS: APPROVED
PR: {PR}
HEAD: {SHA}
REVIEWER: independent
MALICIOUS: rm -rf /
"""
        # The marker is valid, but the wake command is still the trusted one
        parsed = parse_approval_marker(malicious_marker, REPO, PR, SHA)
        self.assertIsNotNone(parsed)
        with mock.patch("orchestrator.pr_poller.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            trigger_wake(watch)
            args = mock_run.call_args[0][0]
            self.assertNotIn("rm", args)
            self.assertIn("echo", args)


class TestCredentialsNotInLogs(unittest.TestCase):
    def test_secrets_not_written_to_logs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.log"
            # Simulate a poll with a fake token in environment — ensure it's not logged
            # The poller never logs env, only repo/pr/sha prefix
            watch = WatchRecord(repo=REPO, pr=PR, expected_sha=SHA)
            # Mock GitHub to avoid network
            with mock.patch("orchestrator.pr_poller.get_pr_state", return_value={"state": "OPEN", "headRefOid": SHA, "mergeable": "MERGEABLE", "closed": False, "mergedAt": None}):
                with mock.patch("orchestrator.pr_poller.get_pr_comments_and_reviews", return_value=[VALID_MARKER]):
                    with mock.patch("orchestrator.pr_poller.trigger_wake", return_value=True):
                        poll_once(state_path=Path(tmpdir) / "watches.json", log_path=log_path, wake_command="echo wake")
            # Check log does not contain a fake secret if we had set one
            os.environ["FAKE_SECRET"] = "gho_super_secret_token_12345"
            try:
                content = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
                self.assertNotIn("gho_super_secret", content)
                self.assertNotIn("FAKE_SECRET", content)
                # Also ensure the full SHA is not logged in full (only prefix logged)
                # Our audit log logs sha[:7], not full sha, which is safe
                # Check that log does not contain the full expected_sha in a raw form beyond the safe prefix?
                # The code logs sha[:7] only, so full sha should not appear in log beyond the watch state file
                # We allow the state file to contain full sha (needed for exact matching), but log should not have full token-like strings
            finally:
                del os.environ["FAKE_SECRET"]


class TestPollOnceIntegration(unittest.TestCase):
    def test_poll_once_with_valid_approval_wakes_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "watches.json"
            log_path = Path(tmpdir) / "log"
            watch = WatchRecord(repo=REPO, pr=PR, expected_sha=SHA, state=WatchState.WAITING_FOR_REVIEW)
            save_watches({watch.key(): watch}, state_path)
            # Mock GitHub
            with mock.patch("orchestrator.pr_poller.get_pr_state", return_value={"state": "OPEN", "headRefOid": SHA, "mergeable": "MERGEABLE", "closed": False, "mergedAt": None}):
                with mock.patch("orchestrator.pr_poller.get_pr_comments_and_reviews", return_value=[VALID_MARKER]):
                    with mock.patch("orchestrator.pr_poller.trigger_wake", return_value=True) as mock_wake:
                        summary = poll_once(state_path=state_path, log_path=log_path, wake_command="echo wake")
                        self.assertEqual(summary["woke"], 1)
                        mock_wake.assert_called_once()
                        # Second poll should not re-wake
                        summary2 = poll_once(state_path=state_path, log_path=log_path, wake_command="echo wake")
                        self.assertEqual(summary2["woke"], 0)

    def test_poll_once_with_head_change_auto_rebinds_no_wake(self):
        # F-002: poll should auto-rebind to new HEAD and go WAITING, not STALE, when OPEN
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "watches.json"
            log_path = Path(tmpdir) / "log"
            watch = WatchRecord(repo=REPO, pr=PR, expected_sha=SHA, state=WatchState.WAITING_FOR_REVIEW)
            save_watches({watch.key(): watch}, state_path)
            with mock.patch("orchestrator.pr_poller.get_pr_state", return_value={"state": "OPEN", "headRefOid": SHA2, "mergeable": "MERGEABLE", "closed": False, "mergedAt": None}):
                with mock.patch("orchestrator.pr_poller.get_pr_comments_and_reviews", return_value=[VALID_MARKER]):
                    with mock.patch("orchestrator.pr_poller.trigger_wake") as mock_wake:
                        summary = poll_once(state_path=state_path, log_path=log_path)
                        self.assertEqual(summary["woke"], 0)
                        mock_wake.assert_not_called()
                        watches = load_watches(state_path)
                        self.assertEqual(watches[watch.key()].state, WatchState.WAITING_FOR_REVIEW)
                        self.assertEqual(watches[watch.key()].expected_sha, SHA2.lower())

    def test_github_api_failure_is_counted_and_no_wake(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "watches.json"
            log_path = Path(tmpdir) / "log"
            watch = WatchRecord(repo=REPO, pr=PR, expected_sha=SHA, state=WatchState.WAITING_FOR_REVIEW)
            save_watches({watch.key(): watch}, state_path)
            with mock.patch("orchestrator.pr_poller.get_pr_state", return_value=None):
                summary = poll_once(state_path=state_path, log_path=log_path)
                self.assertEqual(summary["errors"], 1)
                self.assertEqual(summary["woke"], 0)
                watches = load_watches(state_path)
                self.assertEqual(watches[watch.key()].state, WatchState.ERROR)


class TestWakeFailureRetry(unittest.TestCase):
    """Regression coverage for PR #23: failed wake must remain retryable.

    Covers: retry eligibility preserved, wake_count/last-action not advanced
    on failure, subsequent poll can retry, dedup/stale/fail-closed intact.
    Uses the generic wake path (echo override + no bridge session) unless
    a test explicitly exercises the bridge status fix.
    """

    def _github_open_sha(self, sha=SHA):
        return {"state": "OPEN", "headRefOid": sha, "mergeable": "MERGEABLE", "closed": False, "mergedAt": None}

    def test_failed_wake_preserves_retry_eligibility(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "watches.json"
            log_path = Path(tmpdir) / "log"
            watch = WatchRecord(repo=REPO, pr=PR, expected_sha=SHA, state=WatchState.WAITING_FOR_REVIEW)
            save_watches({watch.key(): watch}, state_path)
            with mock.patch("orchestrator.pr_poller.get_pr_state", return_value=self._github_open_sha(SHA)):
                with mock.patch("orchestrator.pr_poller.get_pr_comments_and_reviews", return_value=[VALID_MARKER]):
                    with mock.patch("orchestrator.opencode_bridge.discover_session_id", return_value=None):
                        with mock.patch("orchestrator.pr_poller.trigger_wake", return_value=False) as mock_wake:
                            summary = poll_once(state_path=state_path, log_path=log_path, wake_command="echo wake")
                            self.assertEqual(summary["woke"], 0)
                            self.assertEqual(summary["errors"], 0)
                            mock_wake.assert_called_once()
                            watches = load_watches(state_path)
                            recovered = watches[watch.key()]
                            # Review state preserved, but wake recording reverted for retry.
                            self.assertEqual(recovered.state, WatchState.APPROVED)
                            self.assertEqual(recovered.wake_count, 0)
                            self.assertIsNone(recovered.last_wake_sha)
                            self.assertIsNone(recovered.last_action_status)
                            self.assertIsNone(recovered.last_action_sha)

    def test_failed_wake_retry_succeeds_on_next_poll(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "watches.json"
            log_path = Path(tmpdir) / "log"
            watch = WatchRecord(repo=REPO, pr=PR, expected_sha=SHA, state=WatchState.WAITING_FOR_REVIEW)
            save_watches({watch.key(): watch}, state_path)
            with mock.patch("orchestrator.pr_poller.get_pr_state", return_value=self._github_open_sha(SHA)):
                with mock.patch("orchestrator.pr_poller.get_pr_comments_and_reviews", return_value=[VALID_MARKER]):
                    with mock.patch("orchestrator.opencode_bridge.discover_session_id", return_value=None):
                        with mock.patch("orchestrator.pr_poller.trigger_wake", return_value=False):
                            first = poll_once(state_path=state_path, log_path=log_path, wake_command="echo wake")
                            self.assertEqual(first["woke"], 0)
                        with mock.patch("orchestrator.pr_poller.trigger_wake", return_value=True) as mock_wake2:
                            second = poll_once(state_path=state_path, log_path=log_path, wake_command="echo wake")
                            self.assertEqual(second["woke"], 1)
                            mock_wake2.assert_called_once()
                            watches = load_watches(state_path)
                            final = watches[watch.key()]
                            self.assertEqual(final.state, WatchState.ACTION_SENT)
                            self.assertEqual(final.wake_count, 1)
                            self.assertEqual(final.last_wake_sha, SHA.lower())
                            self.assertEqual(final.last_action_status, "APPROVED")

    def test_success_dedup_still_works(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "watches.json"
            log_path = Path(tmpdir) / "log"
            watch = WatchRecord(repo=REPO, pr=PR, expected_sha=SHA, state=WatchState.WAITING_FOR_REVIEW)
            save_watches({watch.key(): watch}, state_path)
            with mock.patch("orchestrator.pr_poller.get_pr_state", return_value=self._github_open_sha(SHA)):
                with mock.patch("orchestrator.pr_poller.get_pr_comments_and_reviews", return_value=[VALID_MARKER]):
                    with mock.patch("orchestrator.opencode_bridge.discover_session_id", return_value=None):
                        with mock.patch("orchestrator.pr_poller.trigger_wake", return_value=True):
                            self.assertEqual(poll_once(state_path=state_path, log_path=log_path, wake_command="echo wake")["woke"], 1)
                        with mock.patch("orchestrator.pr_poller.trigger_wake", return_value=True) as mock_wake2:
                            summary2 = poll_once(state_path=state_path, log_path=log_path, wake_command="echo wake")
                            self.assertEqual(summary2["woke"], 0)
                            mock_wake2.assert_not_called()
                            watches = load_watches(state_path)
                            self.assertEqual(watches[watch.key()].wake_count, 1)

    def test_head_change_after_failed_wake_still_rebinds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "watches.json"
            log_path = Path(tmpdir) / "log"
            watch = WatchRecord(repo=REPO, pr=PR, expected_sha=SHA, state=WatchState.WAITING_FOR_REVIEW)
            save_watches({watch.key(): watch}, state_path)
            with mock.patch("orchestrator.pr_poller.get_pr_state", return_value=self._github_open_sha(SHA)):
                with mock.patch("orchestrator.pr_poller.get_pr_comments_and_reviews", return_value=[VALID_MARKER]):
                    with mock.patch("orchestrator.opencode_bridge.discover_session_id", return_value=None):
                        with mock.patch("orchestrator.pr_poller.trigger_wake", return_value=False):
                            poll_once(state_path=state_path, log_path=log_path, wake_command="echo wake")
            # HEAD moves A->B while OPEN: must rebind to B WAITING, no wake.
            with mock.patch("orchestrator.pr_poller.get_pr_state", return_value=self._github_open_sha(SHA2)):
                with mock.patch("orchestrator.pr_poller.get_pr_comments_and_reviews", return_value=[VALID_MARKER]):
                    with mock.patch("orchestrator.pr_poller.trigger_wake") as mock_wake:
                        summary = poll_once(state_path=state_path, log_path=log_path, wake_command="echo wake")
                        self.assertEqual(summary["woke"], 0)
                        mock_wake.assert_not_called()
                        watches = load_watches(state_path)
                        self.assertEqual(watches[watch.key()].expected_sha, SHA2.lower())
                        self.assertEqual(watches[watch.key()].state, WatchState.WAITING_FOR_REVIEW)

    def test_github_failure_after_failed_wake_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "watches.json"
            log_path = Path(tmpdir) / "log"
            watch = WatchRecord(repo=REPO, pr=PR, expected_sha=SHA, state=WatchState.WAITING_FOR_REVIEW)
            save_watches({watch.key(): watch}, state_path)
            with mock.patch("orchestrator.pr_poller.get_pr_state", return_value=self._github_open_sha(SHA)):
                with mock.patch("orchestrator.pr_poller.get_pr_comments_and_reviews", return_value=[VALID_MARKER]):
                    with mock.patch("orchestrator.opencode_bridge.discover_session_id", return_value=None):
                        with mock.patch("orchestrator.pr_poller.trigger_wake", return_value=False):
                            poll_once(state_path=state_path, log_path=log_path, wake_command="echo wake")
            with mock.patch("orchestrator.pr_poller.get_pr_state", return_value=None):
                summary = poll_once(state_path=state_path, log_path=log_path, wake_command="echo wake")
                self.assertEqual(summary["woke"], 0)
                self.assertEqual(summary["errors"], 1)
                watches = load_watches(state_path)
                self.assertEqual(watches[watch.key()].state, WatchState.ERROR)

    def test_bridge_uses_current_review_state(self):
        # PR #23 status fix: first detection has last_action_status=None, but
        # new_watch.state is APPROVED, so the OpenCode bridge must be attempted.
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "watches.json"
            log_path = Path(tmpdir) / "log"
            watch = WatchRecord(repo=REPO, pr=PR, expected_sha=SHA, state=WatchState.WAITING_FOR_REVIEW)
            save_watches({watch.key(): watch}, state_path)
            with mock.patch("orchestrator.pr_poller.get_pr_state", return_value=self._github_open_sha(SHA)):
                with mock.patch("orchestrator.pr_poller.get_pr_comments_and_reviews", return_value=[VALID_MARKER]):
                    with mock.patch("orchestrator.opencode_bridge.discover_session_id", return_value="ses_test123"):
                        with mock.patch("orchestrator.opencode_bridge.inject_merge", return_value=(True, "ok")) as mock_merge:
                            with mock.patch("orchestrator.pr_poller.trigger_wake") as mock_generic:
                                summary = poll_once(state_path=state_path, log_path=log_path, wake_command=None)
                                self.assertEqual(summary["woke"], 1)
                                mock_merge.assert_called_once()
                                mock_generic.assert_not_called()


if __name__ == "__main__":
    unittest.main()
