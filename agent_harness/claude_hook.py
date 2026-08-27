"""Installed Claude Code Stop hook for task-graph completion."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MINIMUM_PYTHON = (3, 10)


def ensure_supported_python(version: tuple[int, ...]) -> None:
    """Fail closed when the hook is launched by an unsupported interpreter."""

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


def main() -> int:
    """Return 2 only when an existing graph is invalid or incomplete."""

    from .errors import HarnessError
    from .paths import workspace_path
    from .storage import GraphStore

    root = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path.cwd())).resolve()
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
        graph_path = workspace_path("task-graph.json", root)
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


def entrypoint() -> int:
    """Console entry point that checks its runtime before importing graph code."""

    ensure_supported_python(sys.version_info[:3])
    return main()
