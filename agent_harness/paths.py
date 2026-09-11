"""Workspace-confined paths and atomic text output for local CLIs."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

from .errors import HarnessError

INSTALL_DIRECTORY = Path(".local/share/graph-engineering-agent-harness")
HOME_VARIABLE = "GRAPH_HARNESS_HOME"

# The key is derived inside a session hook on a short budget, so asking git
# for the top level is bounded rather than trusted to return.
GIT_KEY_TIMEOUT_SECONDS = 2.0


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


def repository_key(value: str | os.PathLike[str] | None = None) -> str:
    """Return one name for the repository a record belongs to.

    Records about a single session are written by three different processes:
    a session hook, a turn hook, and the CLI. They are matched to each other
    by this string, so every producer must derive the same value from a
    different starting point. Two things make that true.

    The repository's top level is used when there is one, because the session
    hooks start from the project directory while the CLI starts from wherever
    the command was run; keyed on the directory itself, a ``graphctl`` call
    from a subdirectory would never match its own session.

    The path is then resolved, because a project reached through a symlink —
    the normal case under ``/tmp`` or ``/var`` on macOS — otherwise yields two
    names for one directory.
    """

    try:
        start = Path(value) if value is not None else Path.cwd()
    except OSError:
        return ""
    try:
        completed = subprocess.run(
            ("git", "-C", str(start), "rev-parse", "--show-toplevel"),
            capture_output=True,
            text=True,
            # Named, not inherited, for the same reason `worktree._git`
            # names it: `text=True` alone decodes with the locale's
            # preferred encoding, and git emits paths as UTF-8 bytes on
            # every platform. Under a cp1252 or cp932 locale a non-ASCII
            # repository path came back mojibake here while
            # `repository_key_fast` — which reads the path from the
            # filesystem — returned it correctly, so the two producers
            # this pair exists to keep in agreement disagreed.
            encoding="utf-8",
            errors="replace",
            timeout=GIT_KEY_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            start = Path(completed.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        # ValueError covers a path holding an embedded null byte, which
        # subprocess rejects before it reaches git. Every caller records
        # rather than acts, so a key that cannot be derived must degrade
        # here rather than depend on each of them wrapping this call.
        pass
    try:
        return str(start.resolve())
    except (OSError, ValueError):
        return str(start)


def repository_key_fast(value: str | os.PathLike[str] | None = None) -> str:
    """Return the same key as ``repository_key`` without spawning git.

    The edit hook runs after every editing tool call rather than once per
    turn, so a subprocess there is paid hundreds of times in a session. The
    top level is found by walking up for ``.git`` instead, which is the same
    directory git names: a worktree and a submodule mark theirs with a
    ``.git`` file rather than a directory, and both are found by testing for
    existence rather than for a directory.

    Falling back to the git call keeps the two in agreement when there is no
    ``.git`` to find, so no producer can drift from the others by using this.
    """

    try:
        start = Path(value) if value is not None else Path.cwd()
        resolved = start.resolve()
    except (OSError, ValueError):
        return repository_key(value)
    for candidate in (resolved, *resolved.parents):
        try:
            if (candidate / ".git").exists():
                return str(candidate)
        except OSError:
            break
    return repository_key(value)


def selected_home() -> Path:
    """Return the home directory that owns installed harness state.

    ``GRAPH_HARNESS_HOME`` exists so tests and a relocated installation can
    point the library at a different tree.  It is read once per call rather
    than cached so a test can change it between cases.
    """

    override = os.environ.get(HOME_VARIABLE)
    return Path(override).resolve() if override else Path.home().resolve()


def user_data_path(
    relative: str | os.PathLike[str], home: str | os.PathLike[str] | None = None
) -> Path:
    """Resolve a path inside the installed data directory beneath the home.

    The workspace confinement in :func:`workspace_path` deliberately refuses
    every path outside the current directory, so it cannot describe installed
    state.  This applies the same component walk re-rooted at the home the
    installer owns, which keeps the one home-directory writer in the library
    under the rule the installer already follows.
    """

    root = Path(home).resolve() if home is not None else selected_home()
    install_root = root / INSTALL_DIRECTORY
    candidate = Path(os.path.abspath(install_root / Path(relative)))
    try:
        candidate.relative_to(install_root)
        parts = candidate.relative_to(root).parts
    except ValueError as exc:
        raise HarnessError(
            f"path escapes the selected home: {candidate}; harness state stays "
            f"under {install_root}"
        ) from exc
    current = root
    for part in parts:
        current = current / part
        if is_link_like(current):
            raise HarnessError(
                f"link-like (symlink or junction) paths are not allowed: {current}"
            )
    return candidate


def blocking_ancestor(relative: str | os.PathLike[str]) -> Path | None:
    """Return the first path on the way to installed state that is not a directory.

    Answered without resolving anything, because resolving is what raises on
    POSIX and what silently succeeds on Windows: a regular file where a
    directory belongs raises ``NotADirectoryError`` out of the path walk on
    one platform and nothing at all on the other, so the same broken install
    read as a warning here and as perfect health there.

    The layout is composed here rather than by the caller. Every reader of
    installed state degrades differently on a broken one — an append reports
    False, an enumeration reports no months — and none of them can name the
    component that is actually wrong, which is the only thing worth saying.
    """

    try:
        root = selected_home() / INSTALL_DIRECTORY / Path(relative)
    except (HarnessError, OSError):
        return None
    for parent in (root, *root.parents):
        try:
            if parent.exists() and not parent.is_dir():
                return parent
        except OSError:
            return None
    return None


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
