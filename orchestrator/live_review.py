"""Manual commissioning entry point for one live, read-only review."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .reviewer import OpenAIResponsesReviewer, ReviewAuditLog, ReviewRequest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Commission one live OpenAI review; no tools or mutations are available.")
    parser.add_argument("--request-json", required=True, type=Path, help="path to a JSON-encoded ReviewRequest")
    parser.add_argument("--audit-log", type=Path, help="durable, append-oriented local JSONL log (default: next to the request)")
    args = parser.parse_args(argv)
    try:
        request_path = args.request_json.resolve()
        request = ReviewRequest(**json.loads(request_path.read_text(encoding="utf-8")))
        result = OpenAIResponsesReviewer.from_environment().review(request)
        audit_path = args.audit_log.resolve() if args.audit_log else request_path.with_name(f"{request_path.stem}-reviews.jsonl")
        ReviewAuditLog(audit_path).append(request, result)
        print(json.dumps(asdict(result), default=lambda value: value.value if hasattr(value, "value") else str(value), sort_keys=True))
        return 0
    except Exception:
        # Keep commissioning failures concise and credential-free.
        print("live review failed closed", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
