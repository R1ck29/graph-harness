"""Working-tree snapshots that show whether a session changed any code.

A session that ignores the protocol may edit through the shell rather than an
editing tool, so counting a client's editing tool calls misses exactly the case
worth catching. Comparing the working tree before and after is independent of
how the edit was made, and is one of the two terms the signal rests on; the
other is an edit event, which says which session's agent did the writing.

``HEAD`` is recorded but never compared. A head move says something changed and
cannot say who changed it or why: authoring, pulling, checking out, rebasing
and a colleague's commit in another terminal are one observation. Judging on it
reported anyone who ran ``git pull`` as having bypassed the protocol. What a
commit of the session's own work leaves behind is caught instead by comparing
consecutive turn boundaries, one of which saw the tree dirty.
"""

from __future__ import annotations

import hashlib
import os
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

# Markers git leaves in its own directory while an operation it could not
# finish is still open. A conflicted merge, rebase, cherry-pick or revert
# fills the tree with content nobody authored, so a snapshot taken while one
# is open is not evidence of anything a session did.
OPERATION_MARKERS = (
    "MERGE_HEAD",
    "REBASE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "rebase-merge",
    "rebase-apply",
)

# Hashed in place of a diff git could not produce, so an unborn HEAD or a
# timed-out call is visible in the digest instead of reading as an empty diff.
DEGRADED_SENTINEL = b"\x01degraded"


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


def _operation(repo: Path, git_directory: str | None) -> str | None:
    """Name the git operation still open in *repo*, if there is one.

    A conflicted merge, rebase, cherry-pick or revert fills the working tree
    with content nobody authored, and aborting it afterwards does not undo the
    observation: the size a session is charged is a maximum across its
    boundaries, so one boundary taken mid-conflict accuses a session whose net
    effect on the repository was nothing at all.
    """

    if git_directory is None:
        return None
    root = Path(git_directory.strip())
    if not root.is_absolute():
        root = repo / root
    for marker in OPERATION_MARKERS:
        try:
            if (root / marker).exists():
                return marker
        except OSError:
            return None
    return None


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
            "operation": None,
            "digest": None,
            "files": 0,
            "lines": 0,
            "degraded": time.monotonic() >= deadline,
        }

    inside = _git(repo, "rev-parse", "--is-inside-work-tree", deadline=deadline)
    if inside is None or inside.strip() != "true":
        return absent()
    status = _git(
        repo,
        "status",
        "-z",
        "--porcelain=v1",
        "-uall",
        "--ignore-submodules=all",
        deadline=deadline,
    )
    if status is None:
        return absent()
    entries = _status_entries(status)
    digest = hashlib.sha256()
    digest.update("\n".join(f"{code} {path}" for code, path in entries).encode("utf-8"))
    degraded = False
    # ``diff HEAD`` fails outright before the first commit, which would drop the
    # content term without saying so. The staged and unstaged diffs together
    # cover the same ground and both work with an unborn HEAD.
    for arguments in (
        ("diff", "--ignore-submodules=all"),
        ("diff", "--cached", "--ignore-submodules=all"),
    ):
        rendered = _git(repo, *arguments, deadline=deadline)
        digest.update(b"\0")
        if rendered is None:
            degraded = True
            digest.update(DEGRADED_SENTINEL)
        else:
            digest.update(rendered.encode("utf-8", "replace"))
    created = 0
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
                content = target.read_bytes()
                digest.update(content)
                # A created file contributes nothing to git diff, so a session
                # that wrote ten thousand new lines was announced as changing
                # none. Counted here, where the bytes are already in hand.
                created += content.count(b"\n") + (
                    1 if content and not content.endswith(b"\n") else 0
                )
        except OSError:
            digest.update(b"?")
    git_directory = _git(repo, "rev-parse", "--git-dir", deadline=deadline)
    operation = _operation(repo, git_directory)
    rendered_head = _git(repo, "rev-parse", "HEAD", deadline=deadline)
    head = (rendered_head or "").strip() or None
    lines = created
    for counted in (
        ("diff", "--shortstat", "--ignore-submodules=all"),
        ("diff", "--cached", "--shortstat", "--ignore-submodules=all"),
    ):
        counted_output = _git(repo, *counted, deadline=deadline)
        if counted_output is None:
            degraded = True
        lines += _changed_lines(counted_output or "")
    return {
        "git": True,
        "head": head,
        "operation": operation,
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


def dirty_changed(before: Any, after: Any) -> bool:
    """Report whether the working tree itself differs between two snapshots.

    ``HEAD`` is deliberately not compared; see this module's own docstring for
    why. Named for what it measures, because the previous name said only
    "changed" and three review rounds were spent on what that was taken to
    mean.

    An absent or malformed endpoint reports no change: the caller records that
    as low confidence rather than accusing a session on missing evidence.
    """

    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    if not before.get("git") or not after.get("git"):
        return False
    return bool(before.get("digest") != after.get("digest"))


def recorded_count(value: Any) -> int:
    """Return a recorded count, treating anything else as absent.

    Every other check in this module tests the type before using a recorded
    value. Converting blindly let one corrupt or forged record raise out of a
    comparison and, because that record stayed the one being compared, silence
    every later report for its repository.
    """

    return value if isinstance(value, int) and not isinstance(value, bool) else 0
