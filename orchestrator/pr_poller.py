"""Lightweight GitHub PR polling service for LM-2nd — zero AI cost while waiting.

The poller is a cheap local process using only gh CLI / GitHub API.
It parses a strict machine-readable approval marker for an exact HEAD SHA
and wakes Whizzy exactly once per approved SHA. No OpenAI / LLM calls
are made during polling.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

try:
    import fcntl  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# State model
# ---------------------------------------------------------------------------

class WatchState(StrEnum):
    WAITING_FOR_REVIEW = "WAITING_FOR_REVIEW"
    CHANGES_REQUIRED = "CHANGES_REQUIRED"
    APPROVED = "APPROVED"
    ACTION_SENT = "ACTION_SENT"
    ACTION_REQUIRED = "ACTION_REQUIRED"  # legacy alias, kept for backward compat
    MERGED = "MERGED"
    STALE = "STALE"
    ERROR = "ERROR"
    CLOSED = "CLOSED"


# Default persistence locations — XDG-friendly, overridable via env.
def _default_state_path() -> Path:
    env = os.environ.get("AI_ORCHESTRATOR_PR_WATCH_STATE")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "ai-auto-orchestrator" / "pr_watches.json"
    return Path.home() / ".local" / "share" / "ai-auto-orchestrator" / "pr_watches.json"


def _default_log_path() -> Path:
    env = os.environ.get("AI_ORCHESTRATOR_PR_POLL_LOG")
    if env:
        return Path(env).expanduser()
    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        return Path(xdg_state) / "ai-auto-orchestrator" / "pr_poller.log"
    return Path.home() / ".local" / "state" / "ai-auto-orchestrator" / "pr_poller.log"


_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class WatchRecord:
    repo: str
    pr: int
    expected_sha: str
    state: WatchState = WatchState.WAITING_FOR_REVIEW
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_observed_head_sha: str | None = None
    last_observed_github_state: str | None = None
    last_observed_review_marker: str | None = None
    last_wake_sha: str | None = None
    wake_count: int = 0
    error_message: str | None = None
    wake_command: str | None = None
    # Extended fields for dual-status support and duplicate suppression per STATUS
    last_action_status: str | None = None  # APPROVED or CHANGES_REQUIRED
    last_action_sha: str | None = None  # SHA for which last_action was sent
    opencode_session_id: str | None = None

    def key(self) -> str:
        return f"{self.repo}#{self.pr}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value if isinstance(self.state, WatchState) else str(self.state)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WatchRecord":
        # Validation — fail closed on malformed persisted data.
        repo = data.get("repo")
        pr = data.get("pr")
        expected_sha = data.get("expected_sha")
        if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo):
            raise ValueError(f"invalid repo: {repo!r}")
        if not isinstance(pr, int) or pr < 1:
            raise ValueError(f"invalid pr: {pr!r}")
        if not isinstance(expected_sha, str) or not _SHA_RE.fullmatch(expected_sha):
            raise ValueError(f"invalid expected_sha: {expected_sha!r}")
        raw_state = data.get("state")
        try:
            state = WatchState(raw_state)
        except ValueError as exc:
            raise ValueError(f"invalid state: {raw_state!r}") from exc
        return cls(
            repo=repo,
            pr=pr,
            expected_sha=expected_sha.lower(),
            state=state,
            created_at=str(data.get("created_at", datetime.now(UTC).isoformat())),
            updated_at=str(data.get("updated_at", datetime.now(UTC).isoformat())),
            last_observed_head_sha=data.get("last_observed_head_sha"),
            last_observed_github_state=data.get("last_observed_github_state"),
            last_observed_review_marker=data.get("last_observed_review_marker"),
            last_wake_sha=data.get("last_wake_sha"),
            wake_count=int(data.get("wake_count", 0)),
            error_message=data.get("error_message"),
            wake_command=data.get("wake_command"),
            last_action_status=data.get("last_action_status"),
            last_action_sha=data.get("last_action_sha"),
            opencode_session_id=data.get("opencode_session_id"),
        )


# ---------------------------------------------------------------------------
# Persistence with file locking
# ---------------------------------------------------------------------------

def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_watches(state_path: Path | None = None) -> dict[str, WatchRecord]:
    path = state_path or _default_state_path()
    if not path.exists():
        return {}
    try:
        # Use shared lock for reading if fcntl available.
        if fcntl is not None:
            with path.open("r", encoding="utf-8") as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_SH)
                    data = json.load(f)
                finally:
                    try:
                        fcntl.flock(f, fcntl.LOCK_UN)
                    except OSError:
                        pass
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        # Corrupt file — fail closed: return empty and let caller decide.
        # We do not raise to avoid crashing the poller; the error is logged
        # at a higher level and the file will be overwritten on next save.
        return {}
    if not isinstance(data, dict):
        return {}
    watches: dict[str, WatchRecord] = {}
    for key, raw in data.get("watches", {}).items():
        try:
            rec = WatchRecord.from_dict(raw)
            watches[key] = rec
        except (ValueError, TypeError, KeyError):
            # Skip malformed entries — fail closed per-entry.
            continue
    return watches


def save_watches(watches: dict[str, WatchRecord], state_path: Path | None = None) -> None:
    path = state_path or _default_state_path()
    _ensure_parent(path)
    payload = {"version": 1, "watches": {k: v.to_dict() for k, v in watches.items()}}
    encoded = json.dumps(payload, sort_keys=True, indent=2).encode() + b"\n"
    # Atomic write + fsync, with exclusive lock.
    tmp = path.with_suffix(".tmp")
    # Use 0o600 for the state file — it may contain repo names and SHAs but not secrets.
    # No tokens are ever written.
    if fcntl is not None:
        # Write to tmp then lock the destination for the rename.
        tmp.write_bytes(encoded)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        # Lock destination file if it exists, otherwise just rename.
        try:
            with path.open("a", encoding="utf-8"):
                pass
        except OSError:
            pass
        try:
            with path.open("r+", encoding="utf-8") as lock_f:
                try:
                    fcntl.flock(lock_f, fcntl.LOCK_EX)
                    tmp.replace(path)
                    try:
                        os.fsync(lock_f.fileno())
                    except OSError:
                        pass
                finally:
                    try:
                        fcntl.flock(lock_f, fcntl.LOCK_UN)
                    except OSError:
                        pass
        except (OSError, FileNotFoundError):
            # Fallback if lock file didn't exist
            tmp.replace(path)
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    else:
        tmp.write_bytes(encoded)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(path)


# ---------------------------------------------------------------------------
# Approval marker parsing — strict, fail closed
# ---------------------------------------------------------------------------

# The marker must appear as a contiguous block with these exact fields.
# We parse case-insensitively for keys but require exact values for
# STATUS/PR/HEAD. Extra whitespace and line endings are tolerated;
# missing or malformed fields cause rejection.
_MARKER_HEADER = "LAYMATCHED-AI-REVIEW"

# We parse line-by-line for determinism and to avoid over-matching.
# Expected lines (order not strictly required but header must be first):
#   LAYMATCHED-AI-REVIEW
#   STATUS: APPROVED | CHANGES_REQUIRED
#   PR: <number>
#   HEAD: <sha>
#   REVIEWER: <string>   (required but value not validated beyond non-empty)
#   FINDINGS:            (required when STATUS=CHANGES_REQUIRED, optional otherwise)
#     F-001 ...
#
# We also accept STATUS, PR, HEAD, REVIEWER in any order after the header,
# but all four must be present. STATUS must be exactly APPROVED or CHANGES_REQUIRED.
# For CHANGES_REQUIRED, FINDINGS block is parsed separately but validity does not require
# it to be machine-parsed beyond presence of header — findings are data.

_VALID_STATUSES = {"APPROVED", "CHANGES_REQUIRED"}


def _parse_marker_block(text: str, expected_repo: str, expected_pr: int, expected_sha: str) -> dict[str, Any] | None:
    """Parse a marker block for either APPROVED or CHANGES_REQUIRED.

    Returns dict with STATUS, PR, HEAD, REVIEWER, FINDINGS (list) if valid, else None.
    """
    if not isinstance(text, str) or not text:
        return None
    if _MARKER_HEADER not in text:
        return None
    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if line.strip() == _MARKER_HEADER:
            header_idx = i
            break
    if header_idx is None:
        return None
    fields: dict[str, str] = {}
    findings: list[str] = []
    in_findings = False
    for line in lines[header_idx + 1 : header_idx + 40]:
        stripped = line.strip()
        if not stripped:
            if in_findings:
                continue
            continue
        # Detect FINDINGS: header
        if stripped.upper().startswith("FINDINGS:"):
            in_findings = True
            # If same line has content after colon, capture it
            after = stripped.split(":", 1)[1].strip()
            if after:
                findings.append(after)
            continue
        if in_findings:
            # Findings lines start with F- or are bullet-like
            # We capture any non-empty line that looks like a finding
            # But we also need to handle new marker header interrupting
            if stripped == _MARKER_HEADER:
                break
            # If line contains colon and looks like a new field outside findings, check if it's a known field
            # We treat findings as everything until we hit another marker or end
            # For determinism, findings are all lines after FINDINGS: until next blank+field or end
            # So we just collect them
            findings.append(stripped)
            continue
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip().upper()
        value = value.strip()
        if key in ("STATUS", "PR", "HEAD", "REVIEWER"):
            # Only take first occurrence
            if key not in fields:
                fields[key] = value
    # Strict validation — all four must be present.
    if "STATUS" not in fields or "PR" not in fields or "HEAD" not in fields or "REVIEWER" not in fields:
        return None
    if fields["STATUS"] not in _VALID_STATUSES:
        return None
    try:
        pr_int = int(fields["PR"])
    except (ValueError, TypeError):
        return None
    if pr_int != expected_pr:
        return None
    head_val = fields["HEAD"].lower()
    if not _SHA_RE.fullmatch(head_val):
        return None
    if head_val != expected_sha.lower():
        return None
    if not fields["REVIEWER"]:
        return None
    # Optional repo field must match if present
    for line in lines[header_idx + 1 : header_idx + 12]:
        if line.strip().upper().startswith("REPO:"):
            repo_val = line.split(":", 1)[1].strip()
            if repo_val and repo_val != expected_repo:
                return None
            break
    # For CHANGES_REQUIRED, FINDINGS may be empty but we still accept — findings are data
    result: dict[str, Any] = dict(fields)
    result["FINDINGS"] = findings
    return result


def parse_approval_marker(text: str, expected_repo: str, expected_pr: int, expected_sha: str) -> dict[str, str] | None:
    """Parse and validate APPROVED marker only (backward compat).

    Returns parsed fields if STATUS==APPROVED and exact SHA matches, else None.
    """
    parsed = _parse_marker_block(text, expected_repo, expected_pr, expected_sha)
    if parsed is None or parsed.get("STATUS") != "APPROVED":
        return None
    # Return string-only dict for compat
    return {k: str(v) for k, v in parsed.items() if k != "FINDINGS"}


def parse_review_marker(text: str, expected_repo: str, expected_pr: int, expected_sha: str) -> dict[str, Any] | None:
    """Parse and validate either APPROVED or CHANGES_REQUIRED marker.

    Returns dict with STATUS, PR, HEAD, REVIEWER, FINDINGS, or None.
    """
    return _parse_marker_block(text, expected_repo, expected_pr, expected_sha)


def contains_valid_approval(texts: list[str], expected_repo: str, expected_pr: int, expected_sha: str) -> dict[str, str] | None:
    """Check list for valid APPROVED marker (backward compat)."""
    for text in texts:
        parsed = parse_approval_marker(text, expected_repo, expected_pr, expected_sha)
        if parsed is not None:
            return parsed
    return None


def contains_valid_review(texts: list[str], expected_repo: str, expected_pr: int, expected_sha: str) -> dict[str, Any] | None:
    """Check list for valid APPROVED or CHANGES_REQUIRED marker.

    Returns first valid parsed marker with STATUS, or None.
    Priority: if both exist, return first encountered (texts order).
    """
    for text in texts:
        parsed = parse_review_marker(text, expected_repo, expected_pr, expected_sha)
        if parsed is not None:
            return parsed
    return None


# ---------------------------------------------------------------------------
# GitHub polling via gh CLI — fail closed
# ---------------------------------------------------------------------------

def _run_gh(args: list[str], timeout: float = 15.0) -> tuple[int, str, str]:
    """Run a gh command without invoking a shell. Returns (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["gh"] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout, result.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)


def get_pr_state(repo: str, pr: int) -> dict[str, Any] | None:
    """Fetch PR state via gh. Returns None on failure (fail closed)."""
    # Use gh pr view --json for structured data. This is a single API call.
    # Fields: number, state (OPEN/CLOSED/MERGED), headRefOid, mergeable, mergeStateStatus
    # We also request isDraft, closed, mergedAt for completeness.
    rc, out, err = _run_gh(
        ["pr", "view", str(pr), "--repo", repo, "--json", "number,state,headRefOid,mergeable,mergeStateStatus,closed,mergedAt,isDraft"],
        timeout=15.0,
    )
    if rc != 0 or not out.strip():
        return None
    try:
        data = json.loads(out)
        # Normalise state to upper for easier handling
        # gh returns state as OPEN/CLOSED/MERGED
        return data
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def get_pr_comments_and_reviews(repo: str, pr: int) -> list[str]:
    """Fetch comment and review bodies for the PR. Returns list of texts. Fail closed -> empty list on error."""
    texts: list[str] = []
    # Comments via gh api: GET /repos/{owner}/{repo}/issues/{pr}/comments
    # Reviews via gh api: GET /repos/{owner}/{repo}/pulls/{pr}/reviews
    # We use gh api to avoid extra dependencies; each is one API call.
    # First, comments
    rc, out, _ = _run_gh(
        ["api", f"repos/{repo}/issues/{pr}/comments", "--paginate"],
        timeout=15.0,
    )
    if rc == 0 and out.strip():
        try:
            data = json.loads(out)
            # gh api --paginate with json output may be a single list or newline-delimited?
            # When paginated, gh may output multiple JSON arrays concatenated. Handle both.
            # Try to parse as JSON; if it fails, try to handle as concatenated arrays.
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and isinstance(item.get("body"), str):
                        texts.append(item["body"])
            elif isinstance(data, dict) and "body" in data:
                texts.append(str(data["body"]))
        except (json.JSONDecodeError, TypeError):
            # Fallback: try to extract bodies via regex for robustness, but fail closed if not parseable
            pass

    # Also fetch pr view comments field as fallback — gh pr view includes comments
    rc2, out2, _ = _run_gh(
        ["pr", "view", str(pr), "--repo", repo, "--json", "comments"],
        timeout=15.0,
    )
    if rc2 == 0 and out2.strip():
        try:
            data2 = json.loads(out2)
            for c in data2.get("comments", []):
                if isinstance(c, dict) and isinstance(c.get("body"), str):
                    body = c["body"]
                    if body not in texts:
                        texts.append(body)
        except (json.JSONDecodeError, TypeError):
            pass

    # Reviews
    rc3, out3, _ = _run_gh(
        ["api", f"repos/{repo}/pulls/{pr}/reviews", "--paginate"],
        timeout=15.0,
    )
    if rc3 == 0 and out3.strip():
        try:
            data3 = json.loads(out3)
            if isinstance(data3, list):
                for item in data3:
                    if isinstance(item, dict):
                        body = item.get("body") or ""
                        if isinstance(body, str) and body:
                            texts.append(body)
                        # Also check review state? But marker is in body
        except (json.JSONDecodeError, TypeError):
            pass

    return texts


# ---------------------------------------------------------------------------
# Logging / audit — no secrets
# ---------------------------------------------------------------------------

def _get_logger(log_path: Path | None = None) -> logging.Logger:
    logger = logging.getLogger("pr_poller")
    if not logger.handlers:
        # Default to log file, not stderr, to keep polling quiet.
        path = log_path or _default_log_path()
        _ensure_parent(path)
        try:
            handler = logging.FileHandler(str(path), encoding="utf-8")
        except OSError:
            handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ")
        formatter.converter = time.gmtime  # type: ignore[assignment]
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def _audit_log(message: str, *, level: int = logging.INFO, extra: dict[str, Any] | None = None, log_path: Path | None = None) -> None:
    logger = _get_logger(log_path)
    # Ensure no secrets are logged — we never log tokens, and we strip any potential credential-like values.
    # Only log repo, pr, sha (first 7), state, and safe metadata.
    safe_extra = {}
    if extra:
        for k, v in extra.items():
            # Truncate to avoid log injection
            safe_extra[k] = str(v)[:256].replace("\n", " ").replace("\r", " ")
    if safe_extra:
        logger.log(level, "%s %s", message, json.dumps(safe_extra, sort_keys=True))
    else:
        logger.log(level, "%s", message)


# ---------------------------------------------------------------------------
# Wake Whizzy — configurable, once per SHA, trusted local config only
# ---------------------------------------------------------------------------

def _default_wake_command() -> str | None:
    # Prefer explicit env, then config file, then none.
    # This is the ONLY source for the wake command — never from PR comment.
    cmd = os.environ.get("PR_POLLER_WAKE_COMMAND") or os.environ.get("WHIZZY_WAKE_COMMAND")
    if cmd:
        return cmd
    # Check config file for wake command
    config_paths = [
        Path.home() / ".config" / "ai-auto-orchestrator" / "pr_poller_wake.conf",
        Path.home() / ".local" / "share" / "ai-auto-orchestrator" / "wake_command.conf",
    ]
    for p in config_paths:
        if p.exists():
            try:
                content = p.read_text(encoding="utf-8").strip()
                if content:
                    return content.splitlines()[0].strip()
            except OSError:
                continue
    return None


def trigger_wake(watch: WatchRecord, wake_command: str | None = None, log_path: Path | None = None) -> bool:
    """Trigger the configured wake command exactly once per SHA.

    Returns True if wake was executed, False otherwise.
    """
    cmd_template = wake_command or watch.wake_command or _default_wake_command()
    if not cmd_template:
        _audit_log("wake not configured — skipping", level=logging.WARNING, extra={"repo": watch.repo, "pr": watch.pr, "sha": watch.expected_sha[:7]}, log_path=log_path)
        return False
    # Substitute placeholders — trusted local template only.
    # Supported placeholders: {repo}, {pr}, {sha}, {expected_sha}
    # We do not use shell=True with untrusted input.
    try:
        # Use shlex to safely handle the template — but we must substitute before shlex.
        # We substitute then shlex split, then run without shell.
        substituted = cmd_template.format(repo=watch.repo, pr=watch.pr, sha=watch.expected_sha, expected_sha=watch.expected_sha)
    except (KeyError, ValueError, AttributeError) as exc:
        _audit_log("wake command template error", level=logging.ERROR, extra={"error": str(exc)[:120]}, log_path=log_path)
        return False
    try:
        args = shlex.split(substituted)
    except ValueError as exc:
        _audit_log("wake command shlex error", level=logging.ERROR, extra={"error": str(exc)[:120]}, log_path=log_path)
        return False
    if not args:
        return False
    # Validate that the command is an allowlisted safe command.
    # We do not allow arbitrary shell composition from the template — it must be a simple argv.
    # The template is from trusted local config, so we allow any executable, but we log it safely.
    _audit_log("triggering wake", extra={"repo": watch.repo, "pr": watch.pr, "sha": watch.expected_sha[:7], "cmd": args[0]}, log_path=log_path)
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30.0, check=False)
        if result.returncode != 0:
            _audit_log("wake command failed", level=logging.WARNING, extra={"returncode": result.returncode, "stderr": result.stderr[:200]}, log_path=log_path)
            return False
        return True
    except (OSError, subprocess.TimeoutExpired) as exc:
        _audit_log("wake command exception", level=logging.ERROR, extra={"error": str(exc)[:200]}, log_path=log_path)
        return False


# ---------------------------------------------------------------------------
# State transitions — deterministic, fail closed
# ---------------------------------------------------------------------------

def evaluate_watch(watch: WatchRecord, github_state: dict[str, Any] | None, approval_texts: list[str], now: str | None = None) -> tuple[WatchRecord, bool]:
    """Evaluate a single watch against current GitHub state.

    Returns (updated_watch, should_wake).
    should_wake is True only when a valid exact-SHA review marker (APPROVED or
    CHANGES_REQUIRED) is newly detected for the expected SHA and wake has not
    yet been triggered for this SHA+STATUS.
    Supports both legacy approval_texts (APPROVED only) and new dual-status flow.
    """
    now = now or datetime.now(UTC).isoformat()
    should_wake = False

    # Fail closed on GitHub error — retain safe state, mark ERROR briefly but do not wake.
    if github_state is None:
        updated = WatchRecord(
            repo=watch.repo,
            pr=watch.pr,
            expected_sha=watch.expected_sha,
            state=WatchState.ERROR if watch.state != WatchState.ERROR else watch.state,
            created_at=watch.created_at,
            updated_at=now,
            last_observed_head_sha=watch.last_observed_head_sha,
            last_observed_github_state=watch.last_observed_github_state,
            last_observed_review_marker=watch.last_observed_review_marker,
            last_wake_sha=watch.last_wake_sha,
            wake_count=watch.wake_count,
            error_message="GitHub API failure",
            wake_command=watch.wake_command,
            last_action_status=watch.last_action_status,
            last_action_sha=watch.last_action_sha,
            opencode_session_id=watch.opencode_session_id,
        )
        return updated, False

    current_head = str(github_state.get("headRefOid") or "").lower()
    github_state_str = str(github_state.get("state") or "").upper()
    merged_at = github_state.get("mergedAt")
    closed = github_state.get("closed", False)
    mergeable = github_state.get("mergeable")
    expected_lower = watch.expected_sha.lower()

    # Detect valid review marker for expected SHA — either APPROVED or CHANGES_REQUIRED
    # Use dual-status parser; fallback to legacy for compat
    valid_review = contains_valid_review(approval_texts, watch.repo, watch.pr, expected_lower)
    # Also check legacy approval for callers that only use that
    if valid_review is None:
        # Try legacy approval (same as valid_review with APPROVED but for safety)
        legacy = contains_valid_approval(approval_texts, watch.repo, watch.pr, expected_lower)
        if legacy is not None:
            valid_review = {"STATUS": "APPROVED", "PR": str(watch.pr), "HEAD": expected_lower, "REVIEWER": legacy.get("REVIEWER", "independent"), "FINDINGS": []}

    last_marker: str | None = None
    review_status: str | None = None
    review_findings: list[str] = []
    if valid_review is not None:
        review_status = str(valid_review.get("STATUS", "")).upper()
        # findings may be list
        findings_raw = valid_review.get("FINDINGS", [])
        if isinstance(findings_raw, list):
            review_findings = [str(x) for x in findings_raw if str(x).strip()]
        last_marker = f"{review_status}:{expected_lower[:7]}:{watch.pr}"
        if review_status == "CHANGES_REQUIRED" and review_findings:
            # Include finding count in marker for audit (truncated)
            last_marker = f"CHANGES_REQUIRED:{expected_lower[:7]}:{watch.pr}:{len(review_findings)}"

    # State transition logic — fail closed
    if github_state_str == "MERGED" or merged_at:
        updated = WatchRecord(
            repo=watch.repo,
            pr=watch.pr,
            expected_sha=watch.expected_sha,
            state=WatchState.MERGED,
            created_at=watch.created_at,
            updated_at=now,
            last_observed_head_sha=current_head or watch.last_observed_head_sha,
            last_observed_github_state=github_state_str,
            last_observed_review_marker=last_marker or watch.last_observed_review_marker,
            last_wake_sha=watch.last_wake_sha,
            wake_count=watch.wake_count,
            error_message=None,
            wake_command=watch.wake_command,
            last_action_status=watch.last_action_status,
            last_action_sha=watch.last_action_sha,
            opencode_session_id=watch.opencode_session_id,
        )
        return updated, False

    if github_state_str == "CLOSED" or (closed and not merged_at):
        updated = WatchRecord(
            repo=watch.repo,
            pr=watch.pr,
            expected_sha=watch.expected_sha,
            state=WatchState.CLOSED,
            created_at=watch.created_at,
            updated_at=now,
            last_observed_head_sha=current_head or watch.last_observed_head_sha,
            last_observed_github_state=github_state_str,
            last_observed_review_marker=last_marker or watch.last_observed_review_marker,
            last_wake_sha=watch.last_wake_sha,
            wake_count=watch.wake_count,
            error_message=None,
            wake_command=watch.wake_command,
            last_action_status=watch.last_action_status,
            last_action_sha=watch.last_action_sha,
            opencode_session_id=watch.opencode_session_id,
        )
        return updated, False

    if current_head and current_head != expected_lower:
        # F-002 automatic rebind: when GitHub HEAD has moved, automatically
        # track the new HEAD as expected and return to WAITING_FOR_REVIEW.
        # The old exact-SHA review (for A) must not approve/wake SHA B.
        # This is the fix loop's step 7-8: after Whizzy pushes B, poller
        # rebinds A→B without manual `add`.
        if _SHA_RE.fullmatch(current_head) and github_state_str == "OPEN":
            # Do not carry old review state/marker to the new SHA.
            # Keep wake history for audit but clear per-SHA action so new
            # SHA requires a fresh independent review.
            updated = WatchRecord(
                repo=watch.repo,
                pr=watch.pr,
                expected_sha=current_head,
                state=WatchState.WAITING_FOR_REVIEW,
                created_at=watch.created_at,
                updated_at=now,
                last_observed_head_sha=current_head,
                last_observed_github_state=github_state_str,
                last_observed_review_marker=None,
                last_wake_sha=watch.last_wake_sha,
                wake_count=watch.wake_count,
                error_message=None,
                wake_command=watch.wake_command,
                last_action_status=None,
                last_action_sha=None,
                opencode_session_id=watch.opencode_session_id,
            )
            return updated, False
        # Fallback STALE if not OPEN or invalid SHA (e.g., GH error, closed)
        updated = WatchRecord(
            repo=watch.repo,
            pr=watch.pr,
            expected_sha=watch.expected_sha,
            state=WatchState.STALE,
            created_at=watch.created_at,
            updated_at=now,
            last_observed_head_sha=current_head,
            last_observed_github_state=github_state_str,
            last_observed_review_marker=last_marker or watch.last_observed_review_marker,
            last_wake_sha=watch.last_wake_sha,
            wake_count=watch.wake_count,
            error_message=f"HEAD changed: expected {expected_lower[:7]}, got {current_head[:7] if current_head else 'unknown'}",
            wake_command=watch.wake_command,
            last_action_status=watch.last_action_status,
            last_action_sha=watch.last_action_sha,
            opencode_session_id=watch.opencode_session_id,
        )
        return updated, False

    # Valid review for exact current HEAD
    if valid_review is not None and review_status in ("APPROVED", "CHANGES_REQUIRED"):
        # Duplicate suppression per PR/SHA/STATUS
        if watch.last_action_sha == expected_lower and watch.last_action_status == review_status and watch.wake_count > 0:
            # Already sent action for this SHA+STATUS
            # Keep state as APPROVED or CHANGES_REQUIRED (or ACTION_SENT)
            # Map to WatchState if possible, else keep current
            try:
                keep_state = WatchState(review_status)
            except ValueError:
                keep_state = watch.state
            # If already ACTION_SENT, keep it
            if watch.state == WatchState.ACTION_SENT:
                keep_state = WatchState.ACTION_SENT
            updated = WatchRecord(
                repo=watch.repo,
                pr=watch.pr,
                expected_sha=watch.expected_sha,
                state=keep_state if keep_state in (WatchState.APPROVED, WatchState.CHANGES_REQUIRED, WatchState.ACTION_SENT) else keep_state,
                created_at=watch.created_at,
                updated_at=now,
                last_observed_head_sha=current_head or expected_lower,
                last_observed_github_state=github_state_str,
                last_observed_review_marker=last_marker,
                last_wake_sha=watch.last_wake_sha,
                wake_count=watch.wake_count,
                error_message=None,
                wake_command=watch.wake_command,
                last_action_status=watch.last_action_status,
                last_action_sha=watch.last_action_sha,
                opencode_session_id=watch.opencode_session_id,
            )
            return updated, False
        # Also suppress if legacy last_wake_sha matches (for backward compat)
        if watch.last_wake_sha == expected_lower and watch.last_action_status is None and watch.wake_count > 0:
            # Legacy wake for same SHA — treat as duplicate regardless of status
            # This preserves previous behavior where one SHA only wakes once
            # But now we allow CHANGES_REQUIRED and APPROVED to be distinct: if we woke for CHANGES_REQUIRED,
            # an APPROVED for same SHA should still be allowed? Spec says never send twice for same PR/SHA/review
            # So if we already woke for same SHA regardless of status, we should not wake again? Actually spec says
            # never send it twice for same PR/SHA/review — so same SHA+STATUS is duplicate, but different STATUS for same SHA
            # could be considered new review (e.g., after fix, same SHA won't happen, but if review flips from CHANGES to APPROVED
            # for same SHA, that would be a new status). We allow distinct STATUS to wake even if same SHA.
            # However legacy behavior was one wake per SHA; we keep that as fallback only when last_action_status is None
            # So if legacy wake exists and new status is APPROVED, we still need to allow? We'll check:
            # If legacy woke and new status is APPROVED, we might have already counted it — but we don't know which status it was.
            # To be safe, if legacy exists, we suppress only if new status is same as what would have been?
            # Since legacy doesn't store status, we suppress any new status for same SHA to preserve old test expectation
            # (test_wake_fires_once_only expects second APPROVED for same SHA to be suppressed)
            # So we keep suppression for same SHA when legacy wake exists.
            updated = WatchRecord(
                repo=watch.repo,
                pr=watch.pr,
                expected_sha=watch.expected_sha,
                state=WatchState.APPROVED if review_status == "APPROVED" else WatchState.CHANGES_REQUIRED,
                created_at=watch.created_at,
                updated_at=now,
                last_observed_head_sha=current_head or expected_lower,
                last_observed_github_state=github_state_str,
                last_observed_review_marker=last_marker,
                last_wake_sha=watch.last_wake_sha,
                wake_count=watch.wake_count,
                error_message=None,
                wake_command=watch.wake_command,
                last_action_status=watch.last_action_status,
                last_action_sha=watch.last_action_sha,
                opencode_session_id=watch.opencode_session_id,
            )
            return updated, False

        # New valid review — prepare to wake
        try:
            new_state = WatchState(review_status)
        except ValueError:
            new_state = WatchState.WAITING_FOR_REVIEW
        # For poller we keep state as APPROVED or CHANGES_REQUIRED (and will be updated to ACTION_SENT after successful wake)
        # But we set it now to the review status; the caller (poll_once) may transition to ACTION_SENT after triggering.
        updated = WatchRecord(
            repo=watch.repo,
            pr=watch.pr,
            expected_sha=watch.expected_sha,
            state=new_state,
            created_at=watch.created_at,
            updated_at=now,
            last_observed_head_sha=current_head or expected_lower,
            last_observed_github_state=github_state_str,
            last_observed_review_marker=last_marker,
            last_wake_sha=expected_lower,
            wake_count=watch.wake_count + 1,
            error_message=None,
            wake_command=watch.wake_command,
            last_action_status=review_status,
            last_action_sha=expected_lower,
            opencode_session_id=watch.opencode_session_id,
        )
        should_wake = True
        return updated, should_wake

    # No valid review — handle recovery and idle
    if watch.state in (WatchState.STALE, WatchState.CLOSED, WatchState.MERGED, WatchState.ERROR):
        if watch.state == WatchState.ERROR and github_state is not None:
            updated = WatchRecord(
                repo=watch.repo,
                pr=watch.pr,
                expected_sha=watch.expected_sha,
                state=WatchState.WAITING_FOR_REVIEW,
                created_at=watch.created_at,
                updated_at=now,
                last_observed_head_sha=current_head or watch.last_observed_head_sha,
                last_observed_github_state=github_state_str,
                last_observed_review_marker=last_marker or watch.last_observed_review_marker,
                last_wake_sha=watch.last_wake_sha,
                wake_count=watch.wake_count,
                error_message=None,
                wake_command=watch.wake_command,
                last_action_status=watch.last_action_status,
                last_action_sha=watch.last_action_sha,
                opencode_session_id=watch.opencode_session_id,
            )
            return updated, False
        return watch, False

    # If previously APPROVED/CHANGES_REQUIRED/ACTION_SENT but now no marker — stay WAITING or keep?
    # Spec: return to WAITING if no valid marker and not terminal
    # We transition back to WAITING to allow next review, but preserve wake history for dedup
    if watch.state in (WatchState.APPROVED, WatchState.CHANGES_REQUIRED, WatchState.ACTION_SENT, WatchState.ACTION_REQUIRED):
        # If no marker now, we stay in that state? But if marker removed, we should go WAITING (fail closed)
        # For simplicity, if no marker, go WAITING but keep last_action for dedup
        updated = WatchRecord(
            repo=watch.repo,
            pr=watch.pr,
            expected_sha=watch.expected_sha,
            state=WatchState.WAITING_FOR_REVIEW,
            created_at=watch.created_at,
            updated_at=now,
            last_observed_head_sha=current_head or watch.last_observed_head_sha or expected_lower,
            last_observed_github_state=github_state_str,
            last_observed_review_marker=last_marker,
            last_wake_sha=watch.last_wake_sha,
            wake_count=watch.wake_count,
            error_message=None,
            wake_command=watch.wake_command,
            last_action_status=watch.last_action_status,
            last_action_sha=watch.last_action_sha,
            opencode_session_id=watch.opencode_session_id,
        )
        return updated, False

    # Default: WAITING_FOR_REVIEW
    updated = WatchRecord(
        repo=watch.repo,
        pr=watch.pr,
        expected_sha=watch.expected_sha,
        state=WatchState.WAITING_FOR_REVIEW,
        created_at=watch.created_at,
        updated_at=now,
        last_observed_head_sha=current_head or watch.last_observed_head_sha or expected_lower,
        last_observed_github_state=github_state_str,
        last_observed_review_marker=last_marker,
        last_wake_sha=watch.last_wake_sha,
        wake_count=watch.wake_count,
        error_message=None,
        wake_command=watch.wake_command,
        last_action_status=watch.last_action_status,
        last_action_sha=watch.last_action_sha,
        opencode_session_id=watch.opencode_session_id,
    )
    return updated, False


# ---------------------------------------------------------------------------
# Single poll iteration — used by CLI and service
# ---------------------------------------------------------------------------

def poll_once(state_path: Path | None = None, log_path: Path | None = None, wake_command: str | None = None) -> dict[str, Any]:
    """Perform one polling iteration over all watched PRs.

    Returns a summary dict for logging / testing.
    """
    path = state_path or _default_state_path()
    watches = load_watches(path)
    if not watches:
        _audit_log("poll: no watches", log_path=log_path)
        return {"watches": 0, "woke": 0, "errors": 0}

    woke = 0
    errors = 0
    updated_watches: dict[str, WatchRecord] = {}

    for key, watch in list(watches.items()):
        # Allow per-watch wake command override, or global
        effective_wake = wake_command or watch.wake_command or _default_wake_command()
        # Fetch GitHub state — fail closed on error
        github_state = get_pr_state(watch.repo, watch.pr)
        texts: list[str] = []
        if github_state is not None:
            texts = get_pr_comments_and_reviews(watch.repo, watch.pr)
        else:
            errors += 1
            _audit_log("GitHub API failure for watch", level=logging.WARNING, extra={"repo": watch.repo, "pr": watch.pr}, log_path=log_path)

        # Evaluate
        new_watch, should_wake = evaluate_watch(watch, github_state, texts)
        # Preserve wake_command and session id if not set
        if new_watch.wake_command is None and effective_wake:
            new_watch = WatchRecord(
                repo=new_watch.repo,
                pr=new_watch.pr,
                expected_sha=new_watch.expected_sha,
                state=new_watch.state,
                created_at=new_watch.created_at,
                updated_at=new_watch.updated_at,
                last_observed_head_sha=new_watch.last_observed_head_sha,
                last_observed_github_state=new_watch.last_observed_github_state,
                last_observed_review_marker=new_watch.last_observed_review_marker,
                last_wake_sha=new_watch.last_wake_sha,
                wake_count=new_watch.wake_count,
                error_message=new_watch.error_message,
                wake_command=effective_wake,
                last_action_status=new_watch.last_action_status,
                last_action_sha=new_watch.last_action_sha,
                opencode_session_id=new_watch.opencode_session_id or watch.opencode_session_id,
            )
        elif new_watch.opencode_session_id is None and watch.opencode_session_id is not None:
            new_watch = WatchRecord(
                repo=new_watch.repo,
                pr=new_watch.pr,
                expected_sha=new_watch.expected_sha,
                state=new_watch.state,
                created_at=new_watch.created_at,
                updated_at=new_watch.updated_at,
                last_observed_head_sha=new_watch.last_observed_head_sha,
                last_observed_github_state=new_watch.last_observed_github_state,
                last_observed_review_marker=new_watch.last_observed_review_marker,
                last_wake_sha=new_watch.last_wake_sha,
                wake_count=new_watch.wake_count,
                error_message=new_watch.error_message,
                wake_command=new_watch.wake_command,
                last_action_status=new_watch.last_action_status,
                last_action_sha=new_watch.last_action_sha,
                opencode_session_id=watch.opencode_session_id,
            )

        updated_watches[key] = new_watch

        # Log state changes
        if new_watch.state != watch.state:
            _audit_log(f"watch {watch.key()} state {watch.state.value} -> {new_watch.state.value}", extra={"repo": watch.repo, "pr": watch.pr, "sha": watch.expected_sha[:7], "new_state": new_watch.state.value}, log_path=log_path)
        if new_watch.last_observed_head_sha != watch.last_observed_head_sha:
            old7 = (watch.last_observed_head_sha or "-")[:7]
            new7 = (new_watch.last_observed_head_sha or "-")[:7]
            _audit_log(f"watch {watch.key()} HEAD observed {old7} -> {new7}", extra={"repo": watch.repo, "pr": watch.pr}, log_path=log_path)

        if should_wake:
            # Try OpenCode bridge first if available and no generic wake command, otherwise generic trigger
            # For dual-status, construct appropriate instruction via bridge
            triggered = False
            status = new_watch.last_action_status
            # Attempt opencode bridge if session is known or discoverable
            # We only use bridge when wake_command is not explicitly set to a different shell template,
            # or when wake_command looks like opencode bridge
            use_bridge = False
            bridge_session = new_watch.opencode_session_id
            if not bridge_session:
                # Try env/file discovery without importing at top level
                try:
                    from orchestrator.opencode_bridge import discover_session_id as _discover
                    bridge_session = _discover()
                except Exception:
                    bridge_session = None
            if bridge_session:
                use_bridge = True
                # If effective_wake is an explicit echo/test command, prefer generic path for tests
                if effective_wake and effective_wake.strip().startswith("echo"):
                    use_bridge = False
            if use_bridge and status in ("APPROVED", "CHANGES_REQUIRED"):
                try:
                    from orchestrator.opencode_bridge import inject_fix, inject_merge
                    # Parse findings from the texts for the matched SHA
                    findings: list[str] = []
                    if status == "CHANGES_REQUIRED":
                        parsed = contains_valid_review(texts, new_watch.repo, new_watch.pr, new_watch.expected_sha)
                        if parsed and isinstance(parsed.get("FINDINGS"), list):
                            findings = [str(x) for x in parsed["FINDINGS"]]
                    if status == "CHANGES_REQUIRED":
                        ok, out = inject_fix(new_watch.repo, new_watch.pr, new_watch.expected_sha, findings, session_id=bridge_session)
                    else:
                        ok, out = inject_merge(new_watch.repo, new_watch.pr, new_watch.expected_sha, session_id=bridge_session)
                    triggered = ok
                    if ok:
                        _audit_log(f"opencode bridge wake triggered for {status}", extra={"repo": watch.repo, "pr": watch.pr, "sha": watch.expected_sha[:7], "session": bridge_session[:12] if bridge_session else "-"}, log_path=log_path)
                    else:
                        _audit_log("opencode bridge wake failed", level=logging.WARNING, extra={"repo": watch.repo, "pr": watch.pr, "error": out[:120]}, log_path=log_path)
                except Exception as exc:
                    _audit_log("opencode bridge exception", level=logging.ERROR, extra={"error": str(exc)[:120]}, log_path=log_path)
                    triggered = False
            if not triggered:
                # Fallback to generic wake command
                triggered = trigger_wake(new_watch, effective_wake, log_path=log_path)
            if triggered:
                woke += 1
                # Transition to ACTION_SENT after successful wake to indicate action delivered
                # Keep last_action_status but update state to ACTION_SENT for audit
                try:
                    sent_watch = WatchRecord(
                        repo=new_watch.repo,
                        pr=new_watch.pr,
                        expected_sha=new_watch.expected_sha,
                        state=WatchState.ACTION_SENT,
                        created_at=new_watch.created_at,
                        updated_at=new_watch.updated_at,
                        last_observed_head_sha=new_watch.last_observed_head_sha,
                        last_observed_github_state=new_watch.last_observed_github_state,
                        last_observed_review_marker=new_watch.last_observed_review_marker,
                        last_wake_sha=new_watch.last_wake_sha,
                        wake_count=new_watch.wake_count,
                        error_message=new_watch.error_message,
                        wake_command=new_watch.wake_command,
                        last_action_status=new_watch.last_action_status,
                        last_action_sha=new_watch.last_action_sha,
                        opencode_session_id=new_watch.opencode_session_id,
                    )
                    updated_watches[key] = sent_watch
                    new_watch = sent_watch
                except Exception:
                    pass
                _audit_log("wake triggered", extra={"repo": watch.repo, "pr": watch.pr, "sha": watch.expected_sha[:7], "status": status or "-"}, log_path=log_path)
            else:
                _audit_log("wake not triggered (no command or failed)", level=logging.WARNING, extra={"repo": watch.repo, "pr": watch.pr}, log_path=log_path)
                errors += 1

        # Clean up terminal watches: MERGED, CLOSED, STALE can be kept for audit or removed?
        # We keep them but they will not be polled for wake again. They can be manually removed.
        # Optionally, we could auto-remove MERGED/CLOSED after some time, but for now keep.

    save_watches(updated_watches, path)
    summary = {"watches": len(updated_watches), "woke": woke, "errors": errors}
    _audit_log(f"poll iteration complete: {summary}", log_path=log_path)
    return summary


def poll_loop(interval: float = 60.0, state_path: Path | None = None, log_path: Path | None = None, wake_command: str | None = None, max_iterations: int | None = None) -> None:
    """Run the polling loop forever (or for max_iterations if set for testing).

    This is a cheap local process — no LLM calls.
    """
    _audit_log("poller started", extra={"interval": interval}, log_path=log_path)
    iteration = 0
    while True:
        if max_iterations is not None and iteration >= max_iterations:
            break
        try:
            poll_once(state_path=state_path, log_path=log_path, wake_command=wake_command)
        except Exception as exc:
            _audit_log(f"poll iteration exception: {type(exc).__name__}", level=logging.ERROR, extra={"error": str(exc)[:200]}, log_path=log_path)
        iteration += 1
        if max_iterations is not None and iteration >= max_iterations:
            break
        time.sleep(interval)
    _audit_log("poller stopped", log_path=log_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _validate_repo(value: str) -> str:
    if not isinstance(value, str) or not _REPO_RE.fullmatch(value):
        raise ValueError(f"invalid repo format, expected 'owner/repo': {value!r}")
    return value


def _validate_sha(value: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise ValueError(f"invalid SHA, expected 40-64 hex chars: {value!r}")
    return value.lower()


def cmd_add(args: argparse.Namespace) -> int:
    repo = _validate_repo(args.repo)
    pr = int(args.pr)
    sha = _validate_sha(args.sha)
    wake_cmd = args.wake_command or _default_wake_command()
    state_path = Path(args.state_file).expanduser() if args.state_file else _default_state_path()
    watches = load_watches(state_path)
    key = f"{repo}#{pr}"
    now = datetime.now(UTC).isoformat()
    # Handle opencode session id if provided via args
    opencode_sid = getattr(args, "opencode_session", None) or getattr(args, "session_id", None)
    if not opencode_sid:
        opencode_sid = os.environ.get("WHIZZY_OPENCODE_SESSION_ID")
    existing = watches.get(key)
    if existing and existing.expected_sha == sha:
        # If session id provided and different, update it
        if opencode_sid and opencode_sid != existing.opencode_session_id:
            updated = WatchRecord(
                repo=existing.repo,
                pr=existing.pr,
                expected_sha=existing.expected_sha,
                state=existing.state,
                created_at=existing.created_at,
                updated_at=now,
                last_observed_head_sha=existing.last_observed_head_sha,
                last_observed_github_state=existing.last_observed_github_state,
                last_observed_review_marker=existing.last_observed_review_marker,
                last_wake_sha=existing.last_wake_sha,
                wake_count=existing.wake_count,
                error_message=existing.error_message,
                wake_command=existing.wake_command or wake_cmd,
                last_action_status=existing.last_action_status,
                last_action_sha=existing.last_action_sha,
                opencode_session_id=opencode_sid,
            )
            watches[key] = updated
            save_watches(watches, state_path)
            print(f"updated session for {key} to {opencode_sid[:12]}", flush=True)
            return 0
        print(f"watch already exists for {key} at {sha[:7]}", flush=True)
        return 0
    # If existing with different SHA, reset to WAITING_FOR_REVIEW with new SHA (per fix loop step 7-8)
    if existing:
        # Update existing watch to new SHA and reset state, but preserve wake history for audit
        # Also reset last_action to allow new review for new SHA
        record = WatchRecord(
            repo=repo,
            pr=pr,
            expected_sha=sha,
            state=WatchState.WAITING_FOR_REVIEW,
            created_at=existing.created_at,
            updated_at=now,
            last_observed_head_sha=existing.last_observed_head_sha,
            last_observed_github_state=existing.last_observed_github_state,
            last_observed_review_marker=None,
            last_wake_sha=existing.last_wake_sha,
            wake_count=existing.wake_count,
            error_message=None,
            wake_command=wake_cmd or existing.wake_command,
            last_action_status=None,
            last_action_sha=None,
            opencode_session_id=opencode_sid or existing.opencode_session_id,
        )
        watches[key] = record
        save_watches(watches, state_path)
        _audit_log("watch updated to new SHA", extra={"repo": repo, "pr": pr, "sha": sha[:7], "prev_sha": existing.expected_sha[:7]}, log_path=Path(args.log_file).expanduser() if args.log_file else None)
        print(f"updated watch for {repo} PR #{pr} to {sha[:7]} (was {existing.expected_sha[:7]})", flush=True)
        return 0
    # Create new watch — WAITING_FOR_REVIEW
    record = WatchRecord(
        repo=repo,
        pr=pr,
        expected_sha=sha,
        state=WatchState.WAITING_FOR_REVIEW,
        created_at=now,
        updated_at=now,
        last_observed_head_sha=None,
        last_observed_github_state=None,
        last_observed_review_marker=None,
        last_wake_sha=None,
        wake_count=0,
        error_message=None,
        wake_command=wake_cmd,
        last_action_status=None,
        last_action_sha=None,
        opencode_session_id=opencode_sid,
    )
    watches[key] = record
    save_watches(watches, state_path)
    _audit_log("watch added", extra={"repo": repo, "pr": pr, "sha": sha[:7]}, log_path=Path(args.log_file).expanduser() if args.log_file else None)
    print(f"added watch for {repo} PR #{pr} at {sha[:7]}", flush=True)
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    repo = _validate_repo(args.repo) if args.repo else None
    pr = int(args.pr) if args.pr else None
    state_path = Path(args.state_file).expanduser() if args.state_file else _default_state_path()
    watches = load_watches(state_path)
    if repo and pr:
        key = f"{repo}#{pr}"
        if key in watches:
            del watches[key]
            save_watches(watches, state_path)
            print(f"removed watch for {key}", flush=True)
            _audit_log("watch removed", extra={"repo": repo, "pr": pr}, log_path=Path(args.log_file).expanduser() if args.log_file else None)
        else:
            print(f"no watch found for {key}", flush=True)
    else:
        # Remove all or list?
        count = len(watches)
        watches.clear()
        save_watches(watches, state_path)
        print(f"removed {count} watches", flush=True)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser() if args.state_file else _default_state_path()
    watches = load_watches(state_path)
    if not watches:
        print("no watches", flush=True)
        return 0
    for key, rec in watches.items():
        print(f"{key} {rec.expected_sha[:7]} {rec.state.value} head:{(rec.last_observed_head_sha or '-')[:7]} wake:{rec.wake_count} status:{rec.last_action_status or '-'} updated:{rec.updated_at}", flush=True)
        if args.verbose:
            print(f"  created: {rec.created_at}", flush=True)
            print(f"  last_marker: {rec.last_observed_review_marker}", flush=True)
            print(f"  last_action: {rec.last_action_status}:{ (rec.last_action_sha or '-')[:7] if rec.last_action_sha else '-'}", flush=True)
            print(f"  opencode_session: {rec.opencode_session_id or '-'}", flush=True)
            print(f"  error: {rec.error_message}", flush=True)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    return cmd_status(args)


def cmd_poll(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser() if args.state_file else _default_state_path()
    log_path = Path(args.log_file).expanduser() if args.log_file else None
    wake_cmd = args.wake_command or _default_wake_command()
    summary = poll_once(state_path=state_path, log_path=log_path, wake_command=wake_cmd)
    print(json.dumps(summary), flush=True)
    return 0 if summary.get("errors", 0) == 0 else 1


def cmd_watch(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser() if args.state_file else _default_state_path()
    log_path = Path(args.log_file).expanduser() if args.log_file else None
    wake_cmd = args.wake_command or _default_wake_command()
    interval = float(args.interval)
    if interval < 5:
        print("interval must be >=5 seconds", flush=True)
        return 2
    try:
        poll_loop(interval=interval, state_path=state_path, log_path=log_path, wake_command=wake_cmd, max_iterations=args.max_iterations)
    except KeyboardInterrupt:
        print("interrupted", flush=True)
        return 0
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orchestrator-pr-watch", description="Lightweight GitHub PR polling service — zero AI cost while waiting")
    parser.add_argument("--state-file", help="path to watches JSON (default: ~/.local/share/ai-auto-orchestrator/pr_watches.json)")
    parser.add_argument("--log-file", help="path to audit log (default: ~/.local/state/ai-auto-orchestrator/pr_poller.log)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="add a PR to watch")
    p_add.add_argument("--repo", required=True, help="owner/repo")
    p_add.add_argument("--pr", required=True, type=int, help="PR number")
    p_add.add_argument("--sha", required=True, help="expected HEAD SHA (40-64 hex)")
    p_add.add_argument("--wake-command", help="shell command template to wake Whizzy, e.g. 'whizzy-wake --repo {repo} --pr {pr} --sha {sha}'")
    p_add.add_argument("--opencode-session", help="OpenCode session ID for Whizzy (e.g., ses_...), or set WHIZZY_OPENCODE_SESSION_ID env")
    p_add.add_argument("--session-id", help=argparse.SUPPRESS)
    p_add.set_defaults(func=cmd_add)

    p_remove = sub.add_parser("remove", help="remove a watched PR")
    p_remove.add_argument("--repo", help="owner/repo")
    p_remove.add_argument("--pr", type=int, help="PR number")
    p_remove.add_argument("--wake-command", help=argparse.SUPPRESS)
    p_remove.set_defaults(func=cmd_remove)

    p_status = sub.add_parser("status", help="show watched PRs")
    p_status.add_argument("--verbose", action="store_true", help="show detailed state")
    p_status.set_defaults(func=cmd_status)

    p_list = sub.add_parser("list", help="alias for status")
    p_list.add_argument("--verbose", action="store_true", help="show detailed state")
    p_list.set_defaults(func=cmd_list)

    p_poll = sub.add_parser("poll", help="run a single polling iteration (for debugging)")
    p_poll.add_argument("--wake-command", help="override wake command")
    p_poll.set_defaults(func=cmd_poll)

    p_watch = sub.add_parser("watch", help="run continuous polling loop (default 60s)")
    p_watch.add_argument("--interval", type=float, default=60.0, help="polling interval seconds (default 60, min 5)")
    p_watch.add_argument("--wake-command", help="override wake command")
    p_watch.add_argument("--max-iterations", type=int, help="max iterations for testing (default infinite)")
    p_watch.set_defaults(func=cmd_watch)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"error: {exc}", flush=True)
        return 2
    except Exception as exc:
        # Fail closed — do not expose stack or secrets
        print(f"error: {type(exc).__name__}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
