#!/usr/bin/env python3
"""Project-compatible wrapper around the installed Claude Stop hook."""

from __future__ import annotations

try:
    from agent_harness.claude_hook import (  # noqa: F401
        MINIMUM_PYTHON,
        ensure_supported_python,
        entrypoint,
        main,
    )
except ModuleNotFoundError:
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from agent_harness.claude_hook import (  # noqa: E402,F401
        MINIMUM_PYTHON,
        ensure_supported_python,
        entrypoint,
        main,
    )


if __name__ == "__main__":
    raise SystemExit(entrypoint())
