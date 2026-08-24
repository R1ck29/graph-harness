"""Workspace-confined paths and atomic text output for local CLIs."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .errors import HarnessError


def workspace_path(
    value: str | os.PathLike[str], root: str | os.PathLike[str] | None = None
) -> Path:
    """Resolve a path inside a workspace and reject existing symlink components."""

    workspace = Path(root or Path.cwd()).resolve()
    raw = Path(value)
    candidate = Path(os.path.abspath(workspace / raw if not raw.is_absolute() else raw))
    try:
        relative = candidate.relative_to(workspace)
    except ValueError as exc:
        raise HarnessError(f"path is outside the workspace: {candidate}") from exc
    current = workspace
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise HarnessError(f"symlink paths are not allowed: {current}")
    return candidate


def atomic_write_text(path: Path, value: str, max_bytes: int) -> None:
    """Atomically write bounded UTF-8 text without following a leaf symlink."""

    rendered = value.encode("utf-8")
    if len(rendered) > max_bytes:
        raise HarnessError(f"output exceeds {max_bytes} bytes")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise HarnessError(f"symlink paths are not allowed: {path}")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
