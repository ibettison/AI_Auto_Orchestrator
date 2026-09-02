"""Manual commissioning entry point for one live, read-only review."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .reviewer import OpenAIResponsesReviewer, ReviewRequest


def main() -> int:
    parser = argparse.ArgumentParser(description="Commission one live OpenAI review; no tools or mutations are available.")
    parser.add_argument("--request-json", required=True, type=Path, help="path to a JSON-encoded ReviewRequest")
    args = parser.parse_args()
    try:
        request = ReviewRequest(**json.loads(args.request_json.read_text(encoding="utf-8")))
        result = OpenAIResponsesReviewer.from_environment().review(request)
        print(json.dumps(asdict(result), default=lambda value: value.value if hasattr(value, "value") else str(value), sort_keys=True))
        return 0
    except Exception:
        # Keep commissioning failures concise and credential-free.
        print("live review failed closed", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
