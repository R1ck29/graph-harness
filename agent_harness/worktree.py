"""Working-tree snapshots that show whether a session changed any code.

A session that ignores the protocol may edit through the shell rather than an
editing tool, so counting a client's editing tool calls misses exactly the case
worth catching. Comparing the working tree before and after is independent of
how the edit was made.

Both terms of the comparison are required. The digest alone misses a session
that commits its work, because committing returns the tree to the state the
digest already recorded; ``HEAD`` alone misses uncommitted work.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

# A snapshot runs inside a session hook, where the client budget is short. One
# session-end hook is given about a second and a half, and a snapshot issues
# several git calls, so bounding each call alone would still allow a total far
# beyond that budget and lose the record entirely.
GIT_TIMEOUT_SECONDS = 2.0
SNAPSHOT_BUDGET_SECONDS = 4.0

# Untracked file contents are hashed so that editing an untracked file is
# visible; the status line alone would not change. Large files are represented
# by their size, which keeps a session that drops a build artifact cheap.
MAX_UNTRACKED_BYTES = 1_048_576

# Hashed in place of a diff git could not produce, so an unborn HEAD or a
# timed-out call is visible in the digest instead of reading as an empty diff.
DEGRADED_SENTINEL = b"\x01degraded"

# Recorded HEAD values reach git as arguments. The journal is forgeable by
# anyone who can write the user's home, so a value that is not a commit name
# is refused rather than passed through, where an option-shaped one would
# make git act on it.
COMMIT_NAME = re.compile(r"[0-9a-f]{40}")


def _git(repo: Path, *arguments: str, deadline: float | None = None) -> str | None:
    """Run one git command in *repo*, returning None when it cannot report.

    *deadline* is a ``time.monotonic`` value the whole snapshot must finish
    by. Once it passes, remaining calls report nothing rather than each
    spending the per-call timeout again.
    """

    remaining = GIT_TIMEOUT_SECONDS
    if deadline is not None:
        remaining = min(remaining, deadline - time.monotonic())
        if remaining <= 0:
            return None
    try:
        completed = subprocess.run(
            ("git", "-C", str(repo), *arguments),
            capture_output=True,
            text=True,
            timeout=remaining,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _status_entries(status: str) -> list[tuple[str, str]]:
    """Parse NUL-separated porcelain status into (code, path) pairs.

    The NUL form is required rather than convenient. Under git's default
    ``core.quotepath`` the newline form C-quotes any path holding non-ASCII
    bytes, a quote, or a newline, so a Japanese filename arrives as an escape
    string that no longer names a file on disk.
    """

    fields = [field for field in status.split("\0") if field]
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if len(field) < 4:
            continue
        code, path = field[:2], field[3:]
        # A rename or copy is followed by its origin path in a separate
        # field; both belong to the digest. The status letter can sit in
        # either column, so testing only the index column would leave a
        # worktree-side rename's origin to be counted as its own entry.
        if ("R" in code or "C" in code) and index < len(fields):
            path = f"{path}\0{fields[index]}"
            index += 1
        entries.append((code, path))
    return sorted(entries)


def _changed_lines(shortstat: str) -> int:
    """Return the insertions and deletions named by one --shortstat line."""

    total = 0
    for part in shortstat.split(","):
        fields = part.strip().split()
        if len(fields) >= 2 and fields[1].startswith(("insertion", "deletion")):
            try:
                total += int(fields[0])
            except ValueError:
                continue
    return total


def snapshot(directory: str | os.PathLike[str]) -> dict[str, Any]:
    """Return a comparable description of the working tree at *directory*.

    ``git`` is reported separately from the digest because a directory outside
    version control and a clean repository would otherwise be indistinguishable
    and a bypass in an unversioned directory would read as no change at all.
    """

    repo = Path(directory)
    deadline = time.monotonic() + SNAPSHOT_BUDGET_SECONDS

    def absent() -> dict[str, Any]:
        # A snapshot that ran out of budget is not the same as a directory
        # outside version control, and reporting the two identically would
        # let a slow repository read as a clean one.
        return {
            "git": False,
            "head": None,
            "digest": None,
            "files": 0,
            "lines": 0,
            "degraded": time.monotonic() >= deadline,
        }

    inside = _git(repo, "rev-parse", "--is-inside-work-tree", deadline=deadline)
    if inside is None or inside.strip() != "true":
        return absent()
    status = _git(repo, "status", "-z", "--porcelain=v1", "-uall", deadline=deadline)
    if status is None:
        return absent()
    entries = _status_entries(status)
    digest = hashlib.sha256()
    digest.update("\n".join(f"{code} {path}" for code, path in entries).encode("utf-8"))
    degraded = False
    # ``diff HEAD`` fails outright before the first commit, which would drop the
    # content term without saying so. The staged and unstaged diffs together
    # cover the same ground and both work with an unborn HEAD.
    for arguments in (("diff",), ("diff", "--cached")):
        rendered = _git(repo, *arguments, deadline=deadline)
        digest.update(b"\0")
        if rendered is None:
            degraded = True
            digest.update(DEGRADED_SENTINEL)
        else:
            digest.update(rendered.encode("utf-8", "replace"))
    for code, path in entries:
        if code != "??":
            continue
        digest.update(b"\0")
        digest.update(path.encode("utf-8", "replace"))
        target = repo / path
        try:
            size = target.stat().st_size
            digest.update(str(size).encode("ascii"))
            if target.is_file() and size <= MAX_UNTRACKED_BYTES:
                digest.update(target.read_bytes())
        except OSError:
            digest.update(b"?")
    rendered_head = _git(repo, "rev-parse", "HEAD", deadline=deadline)
    head = (rendered_head or "").strip() or None
    lines = 0
    for counted in (("diff", "--shortstat"), ("diff", "--cached", "--shortstat")):
        counted_output = _git(repo, *counted, deadline=deadline)
        if counted_output is None:
            degraded = True
        lines += _changed_lines(counted_output or "")
    return {
        "git": True,
        "head": head,
        "digest": digest.hexdigest(),
        "files": len(entries),
        "lines": lines,
        # A missing HEAD is normal before the first commit, but only when git
        # was actually asked; losing the call to the budget must not read as
        # an unborn branch, because comparing a real HEAD against that absence
        # would fabricate a change.
        "degraded": degraded
        or (rendered_head is None and time.monotonic() >= deadline),
    }


def committed_size(
    directory: str | os.PathLike[str], before: Any, after: Any
) -> tuple[int, int]:
    """Return the file and line counts of the commits between two snapshots.

    A session that commits leaves a clean working tree, so the snapshots alone
    say only that ``HEAD`` moved. Asking git what moved distinguishes work the
    session did from a pull, a checkout, or a commit made elsewhere that
    happens to have landed while it ran.
    """

    if not isinstance(before, dict) or not isinstance(after, dict):
        return (0, 0)
    start, end = before.get("head"), after.get("head")
    if not isinstance(start, str) or not isinstance(end, str) or start == end:
        return (0, 0)
    if not COMMIT_NAME.fullmatch(start) or not COMMIT_NAME.fullmatch(end):
        return (0, 0)
    repo = Path(directory)
    deadline = time.monotonic() + SNAPSHOT_BUDGET_SECONDS
    shortstat = _git(repo, "diff", "--shortstat", start, end, "--", deadline=deadline)
    if shortstat is None:
        return (0, 0)
    names = _git(repo, "diff", "--name-only", start, end, "--", deadline=deadline) or ""
    return (
        len([name for name in names.splitlines() if name]),
        _changed_lines(shortstat),
    )


def changed(before: Any, after: Any) -> bool:
    """Report whether the tree moved between two snapshots.

    An absent or malformed endpoint reports no change: the caller records that
    as low confidence rather than accusing a session on missing evidence.
    """

    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    if not before.get("git") or not after.get("git"):
        return False
    return bool(
        before.get("head") != after.get("head")
        or before.get("digest") != after.get("digest")
    )


def _count(value: Any) -> int:
    """Return a recorded count, treating anything else as absent.

    Every other check in this module tests the type before using a recorded
    value. Converting blindly let one corrupt or forged record raise out of a
    comparison and, because that record stayed the one being compared, silence
    every later report for its repository.
    """

    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def change_size(before: Any, after: Any) -> tuple[int, int]:
    """Return the changed file and line counts implied by two snapshots.

    Both are differences. A tree that was already dirty when the session
    started is not the session's doing, and charging it the absolute count
    produced reports naming files nobody in that session touched.
    """

    if not isinstance(before, dict) or not isinstance(after, dict):
        return (0, 0)
    files = max(_count(after.get("files")) - _count(before.get("files")), 0)
    lines = abs(_count(after.get("lines")) - _count(before.get("lines")))
    return (files, lines)
