"""Workspace-confined paths and atomic text output for local CLIs."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from .errors import HarnessError


def is_link_like(path: Path) -> bool:
    """Return whether *path* is a symlink or Windows junction.

    ``Path.is_junction`` is only available on Python 3.12 and newer.  Reading
    the reparse tag keeps the same protection on the project's minimum
    supported Python (3.10) without rejecting unrelated cloud placeholders.
    """

    if path.is_symlink():
        return True
    try:
        status = path.lstat()
    except FileNotFoundError:
        return False
    reparse_tag = getattr(status, "st_reparse_tag", None)
    if reparse_tag is not None:
        blocked_tags = {
            getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
            getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
        }
        return reparse_tag in blocked_tags
    attributes = getattr(status, "st_file_attributes", 0)
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_point)


def workspace_path(
    value: str | os.PathLike[str], root: str | os.PathLike[str] | None = None
) -> Path:
    """Resolve a path inside a workspace and reject existing link-like components."""

    workspace = Path(root or Path.cwd()).resolve()
    raw = Path(value)
    candidate = Path(os.path.abspath(workspace / raw if not raw.is_absolute() else raw))
    try:
        relative = candidate.relative_to(workspace)
    except ValueError as exc:
        raise HarnessError(
            f"path is outside the workspace: {candidate}; the workspace is the "
            f"current directory ({workspace}), so run the command from the "
            "directory that holds the graph"
        ) from exc
    current = workspace
    for part in relative.parts:
        current = current / part
        if is_link_like(current):
            raise HarnessError(
                f"link-like (symlink or junction) paths are not allowed: {current}"
            )
    return candidate


def atomic_write_text(path: Path, value: str, max_bytes: int) -> None:
    """Atomically write bounded UTF-8 text without following a leaf symlink."""

    rendered = value.encode("utf-8")
    if len(rendered) > max_bytes:
        raise HarnessError(f"output exceeds {max_bytes} bytes")
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_link_like(path):
        raise HarnessError(
            f"link-like (symlink or junction) paths are not allowed: {path}"
        )
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
