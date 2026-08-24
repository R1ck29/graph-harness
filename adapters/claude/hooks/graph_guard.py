#!/usr/bin/env python3
"""Claude Stop hook: refuse an invalid or falsely completed task graph."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path.cwd())).resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_harness.errors import HarnessError  # noqa: E402
from agent_harness.storage import GraphStore  # noqa: E402


def main() -> int:
    try:
        raw = sys.stdin.read(1_048_577)
        if len(raw.encode("utf-8")) > 1_048_576:
            print("Graph guard input exceeds 1 MiB", file=sys.stderr)
            return 2
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        print("Graph guard input must be a JSON object", file=sys.stderr)
        return 2
    if payload.get("stop_hook_active"):
        return 0
    graph_path = ROOT / "task-graph.json"
    if not graph_path.exists():
        return 0
    try:
        graph = GraphStore(graph_path).load()
        graph.completion_check()
    except (HarnessError, OSError) as exc:
        print(
            f"Graph completion is not verified: {exc}. Continue the protocol or ask the user to pause explicitly.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
