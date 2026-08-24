#!/usr/bin/env python3
"""Run the dependency-free offline protocol evaluation suite."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_harness.evals import run_suite  # noqa: E402
from agent_harness.errors import HarnessError  # noqa: E402
from agent_harness.paths import atomic_write_text, workspace_path  # noqa: E402

MAX_RESULT_BYTES = 4 * 1024 * 1024


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default=str(ROOT / "evals" / "cases"))
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        result = run_suite(args.cases)
        rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output:
            output = workspace_path(args.output, ROOT)
            atomic_write_text(output, rendered, MAX_RESULT_BYTES)
    except (HarnessError, OSError, json.JSONDecodeError) as exc:
        print(f"eval runner: {exc}", file=sys.stderr)
        return 2
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
