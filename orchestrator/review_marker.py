"""Deterministic machine-readable review marker producer for LM-2nd.

This is the smallest adapter around the existing independent reviewer that
posts a trusted, exact-SHA marker to the PR so the poller can wake Whizzy
without Ian copying ChatGPT text.

Marker format (strict, poller-compatible):
  LAYMATCHED-AI-REVIEW
  STATUS: APPROVED | CHANGES_REQUIRED
  PR: <number>
  HEAD: <40-64 hex>
  REVIEWER: independent
  FINDINGS:
  F-001 <title>: <remediation> ...

Requirements:
- marker is generated locally by trusted code (this module) from the
  validated ReviewRequest/ReviewResult's exact SHAs and Verdict enum,
  not from model free prose
- exact SHA comes from trusted Git/GitHub state (request.head_sha /
  head_sha param), not from model output alone (we still verify model
  returned same SHA)
- GitHub post failure fails closed
- duplicate marker for same review/SHA avoided (check existing comments)
- no LLM, no paid polling, no AWS during marker generation/post
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any

from .reviewer import ReviewRequest, ReviewResult, Verdict

_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def build_marker(repo: str, pr: int, head_sha: str, verdict: Verdict, findings: list[Any] | tuple[Any, ...] = ()) -> str:
    """Build deterministic marker string from trusted state.

    verdict must be APPROVED or CHANGES_REQUESTED (mapped to CHANGES_REQUIRED).
    findings are trusted Finding objects; only id/title/remediation used, bounded.
    """
    if not _REPO_RE.fullmatch(repo):
        raise ValueError(f"invalid repo: {repo!r}")
    if not isinstance(pr, int) or pr < 1:
        raise ValueError(f"invalid pr: {pr!r}")
    if not _SHA_RE.fullmatch(head_sha):
        raise ValueError(f"invalid head_sha: {head_sha!r}")
    if verdict not in (Verdict.APPROVED, Verdict.CHANGES_REQUESTED):
        raise ValueError(f"unsupported verdict for marker: {verdict!r}")

    status = "APPROVED" if verdict == Verdict.APPROVED else "CHANGES_REQUIRED"
    lines = [
        "LAYMATCHED-AI-REVIEW",
        f"STATUS: {status}",
        f"PR: {pr}",
        f"HEAD: {head_sha.lower()}",
        "REVIEWER: independent",
    ]
    if status == "CHANGES_REQUIRED":
        lines.append("FINDINGS:")
        # findings are data, never shell; truncate to avoid prompt injection via marker size
        for f in list(findings)[:20]:
            fid = getattr(f, "finding_id", str(f))[:64].replace("\n", " ").replace("\r", " ")
            title = getattr(f, "title", "")[:200].replace("\n", " ").replace("\r", " ")
            remediation = getattr(f, "remediation", "")[:300].replace("\n", " ").replace("\r", " ")
            # Single line per finding, bounded
            line = f"{fid} {title}: {remediation}".strip()
            # Ensure it starts with F- or at least not empty; fallback to id
            if not line:
                line = fid
            lines.append(line[:500])
        if not findings:
            lines.append("(no findings)")
    return "\n".join(lines) + "\n"


def build_marker_from_review(repo: str, pr: int, request: ReviewRequest, result: ReviewResult) -> str:
    """Build marker from validated request/result — exact SHA binding.

    result.reviewed_head_sha must equal request.head_sha (validated).
    """
    # Validate exact SHA binding — trusted Git state, not model prose
    if request.head_sha.lower() != result.reviewed_head_sha.lower():
        raise ValueError("review head SHA does not match request head SHA")
    if not _SHA_RE.fullmatch(request.head_sha):
        raise ValueError("invalid request head SHA")
    return build_marker(repo, pr, request.head_sha.lower(), result.verdict, list(result.findings))


def _gh_api_comment_exists(repo: str, pr: int, head_sha: str, marker_status: str) -> bool:
    """Check if a marker for same PR/HEAD/STATUS already exists (avoid duplicate).

    Uses gh CLI with GET; returns True if duplicate found, False otherwise.
    Fail open on GH error (we will still try to post; duplicate avoidance is best-effort).
    """
    try:
        # Use gh api to list comments and look for marker header + HEAD
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/issues/{pr}/comments", "--paginate"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return False
        import json
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return False
        if not isinstance(data, list):
            return False
        needle_head = head_sha.lower()
        needle_status = marker_status
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("body"), str):
                body = item["body"]
                if "LAYMATCHED-AI-REVIEW" in body and needle_head in body.lower() and needle_status in body:
                    # Found marker for same HEAD+STATUS
                    return True
        return False
    except (OSError, subprocess.TimeoutExpired):
        return False


def post_marker_via_gh(repo: str, pr: int, marker: str, *, check_duplicate: bool = True) -> None:
    """Post marker to PR via gh CLI — fail closed on failure.

    Raises RuntimeError on failure; caller should fail the run closed.
    No LLM, no AWS, no paid service — just gh API POST.
    """
    if check_duplicate:
        # Extract STATUS and HEAD from marker for duplicate check
        status = "APPROVED" if "STATUS: APPROVED" in marker else "CHANGES_REQUIRED" if "STATUS: CHANGES_REQUIRED" in marker else ""
        head = ""
        for line in marker.splitlines():
            if line.upper().startswith("HEAD:"):
                head = line.split(":", 1)[1].strip().lower()
                break
        if status and head and _gh_api_comment_exists(repo, pr, head, status):
            # Duplicate already exists — avoid re-posting
            return

    # Use gh pr comment (or gh api). gh pr comment is simpler and handles auth via gh's credential.
    # We try gh pr comment first, fallback to gh api POST.
    # Use --body-file - to avoid shell quoting issues with marker content
    try:
        result = subprocess.run(
            ["gh", "pr", "comment", str(pr), "--repo", repo, "--body", marker],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode == 0:
            return
        # Fallback to gh api POST
        import json
        payload = json.dumps({"body": marker})
        result2 = subprocess.run(
            ["gh", "api", f"repos/{repo}/issues/{pr}/comments", "--method", "POST", "--input", "-"],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
        if result2.returncode != 0:
            raise RuntimeError(f"gh post failed: {result.stderr[:200]} {result2.stderr[:200]}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"gh post exception: {exc}") from None


def post_marker_via_github_port(github_port: Any, workspace: Path, repo: str, pr: int, marker: str) -> None:
    """Post marker via ObjectiveRunner's GitHubPort (urllib) — fail closed.

    github_port must have .comment(workspace, repo, pr, body) method.
    This is used inside ObjectiveRunner after a review cycle.
    """
    try:
        github_port.comment(workspace, repo, pr, marker)
    except Exception as exc:
        raise RuntimeError(f"GitHub comment failed closed: {type(exc).__name__}") from None
