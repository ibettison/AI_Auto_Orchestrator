"""Prepare one SHA-bound live-review request without contacting OpenAI."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from .reviewer import ReviewInputPreparer, ReviewRequest


_OPENAI_SECRET_DIRECTORY = Path("/opt/ai-orchestrator/secrets")


def _repository_is_clean(repository: Path) -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repository,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0 and not result.stdout


def _evidence(value: str) -> Mapping[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("validation evidence must be a JSON object")
    return parsed


def _write_request(output: Path, request: ReviewRequest) -> None:
    payload = json.dumps(asdict(request), indent=2, sort_keys=True) + "\n"
    file_descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as stream:
            file_descriptor = -1
            stream.write(payload)
    finally:
        if file_descriptor != -1:
            os.close(file_descriptor)


def prepare_request(args: argparse.Namespace) -> ReviewRequest:
    repository = Path(args.repository).resolve()
    if not repository.is_dir() or not _repository_is_clean(repository):
        raise ValueError("repository must exist and have a clean Git worktree")
    evidence = _evidence(args.validation_evidence_json)
    return ReviewInputPreparer(max_diff_bytes=args.max_diff_bytes).prepare(
        review_id=args.review_id,
        run_id=args.run_id,
        repository=str(repository),
        objective=args.objective,
        base_sha=args.base_sha,
        expected_head_sha=args.head_sha,
        validation_evidence=evidence,
        cycle=args.cycle,
        risk=args.risk,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare one immutable live-review request; makes no OpenAI call.")
    parser.add_argument("--repository", required=True, help="absolute local Git repository path")
    parser.add_argument("--base-sha", required=True, help="exact immutable base commit SHA")
    parser.add_argument("--head-sha", required=True, help="exact immutable head commit SHA")
    parser.add_argument("--review-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--objective", required=True)
    parser.add_argument("--validation-evidence-json", required=True, help="JSON object containing validation evidence")
    parser.add_argument("--output", required=True, type=Path, help="explicit output path for the ReviewRequest JSON")
    parser.add_argument("--cycle", type=int, default=1)
    parser.add_argument("--risk", choices=("green", "amber", "red"), default="green")
    parser.add_argument("--max-diff-bytes", type=int, default=256 * 1024)
    args = parser.parse_args(argv)
    try:
        output = args.output.resolve()
        if output == _OPENAI_SECRET_DIRECTORY or output.is_relative_to(_OPENAI_SECRET_DIRECTORY):
            raise ValueError("refusing to write beneath the OpenAI secrets directory")
        request = prepare_request(args)
        _write_request(output, request)
        return 0
    except Exception:
        print("live-review request preparation failed closed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
