#!/usr/bin/env python3
"""Claude Stop hook: refuse an invalid or falsely completed task graph."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MINIMUM_PYTHON = (3, 10)


def ensure_supported_python(version: tuple[int, ...]) -> None:
    """Refuse to certify completion from an interpreter the project untests.

    The package declares `requires-python >= 3.10` and CI covers only
    supported releases, so an older interpreter is an unverified
    configuration. Exiting 2 keeps the guard fail-closed and puts an
    actionable message in front of the model instead of an import traceback.
    """

    if version >= MINIMUM_PYTHON:
        return
    running = ".".join(str(part) for part in version[:3])
    print(
        "Graph guard needs Python "
        f"{MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]} or newer but the configured "
        f"hook command ran {running} from {sys.executable}. Point the Stop "
        "hook command at the interpreter used to install the harness, such as "
        ".venv/bin/python, then stop again.",
        file=sys.stderr,
    )
    raise SystemExit(2)


if __name__ == "__main__":
    # Run before importing the harness so an unsupported interpreter reports the
    # actionable message rather than an import traceback. Importing this module
    # for tests skips the check so the suite can load it on any interpreter.
    # sys.version_info is (major, minor, micro, releaselevel, serial) and the
    # fourth field is a string, so pass only the numeric prefix.
    ensure_supported_python(sys.version_info[:3])

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path.cwd())).resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_harness.errors import HarnessError  # noqa: E402
from agent_harness.paths import workspace_path  # noqa: E402
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
    try:
        graph_path = workspace_path("task-graph.json", ROOT)
        if not graph_path.exists():
            return 0
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
