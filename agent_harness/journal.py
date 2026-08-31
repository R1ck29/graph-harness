"""Bounded append-only observation records for harness sessions.

The graph records tasks. It cannot record that a session ignored the protocol
entirely, because a session that never runs ``graphctl`` never touches the
graph. This module records session and transition observations beside the
installed runtime so that gap becomes visible.

The records are not an audit trail and must never be described as one. Anyone
who can write the user's home directory can forge or delete them, exactly as
``docs/security.md`` already says of executor and reviewer identities.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import HarnessError
from .graph import utc_now
from .paths import is_link_like, user_data_path

# Version 2 adds the ``edit`` event. Readers accept both: a version 1 record
# carries no edit events, which reads correctly as a session observed through
# the working tree alone.
SCHEMA_VERSION = 2
JOURNAL_DIRECTORY = "journal"
# Spelled with an explicit digit range because ``\d`` also matches non-ASCII
# digits, which would let a file no writer of ours creates name a month.
MONTH_NAME = re.compile(r"[0-9]{4}-[0-9]{2}\.jsonl")

# Concurrent sessions append without coordination. A single ``os.write`` of a
# line this size does not interleave with another writer's line, so the bound
# is what keeps the file parseable rather than a storage limit.
MAX_JOURNAL_LINE_BYTES = 4096

# Retention is stated because the journal is the one part of harness state that
# grows without a task being spent, the same reason the review archive in
# ``graph.py`` carries a bound.
MAX_JOURNAL_MONTHS = 6

EVENTS = (
    "session_open",
    "turn_end",
    "session_close",
    "graph_transition",
    # Recorded when a bypass has been reported to someone, so the same one is
    # not raised again. Deciding that from timestamps alone misfired on a
    # second-precision clock, on a damaged record, and on an unrelated
    # concurrent session.
    "bypass_reported",
    # Direct evidence that this session's agent wrote a file. A working-tree
    # comparison says something changed; only this says who changed it.
    "edit",
)

# Dropped in this order to bring an over-long record under the line bound,
# least useful first. Identity fields are never shed, so a shed record still
# names the session it came from.
SHEDDABLE_FIELDS = ("snapshot", "reason", "source", "actor", "command", "repo")


def journal_directory(home: str | os.PathLike[str] | None = None) -> Path:
    """Return the directory that holds monthly journal files."""

    return user_data_path(JOURNAL_DIRECTORY, home)


def month_path(timestamp: str, home: str | os.PathLike[str] | None = None) -> Path:
    """Return the monthly journal file for an ISO-8601 UTC *timestamp*."""

    return journal_directory(home) / f"{timestamp[:7]}.jsonl"


def record(event: str, **fields: Any) -> dict[str, Any]:
    """Build one journal record with the shared identity fields filled in."""

    if event not in EVENTS:
        raise ValueError(f"unknown journal event: {event}")
    entry: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event": event,
        "ts": utc_now(),
    }
    entry.update({key: value for key, value in fields.items() if value is not None})
    return entry


def render(entry: dict[str, Any]) -> str | None:
    """Render *entry* as one bounded JSONL line, shedding fields if needed.

    Returns ``None`` when no combination of sheddable fields brings the
    record under the bound, which means either the identity fields alone
    exceed it or a caller supplied an oversized field this module does not
    know how to shed. Any large field a caller adds belongs in
    ``SHEDDABLE_FIELDS`` or must be bounded before it arrives.
    """

    shrunk = dict(entry)
    for field in (None, *SHEDDABLE_FIELDS):
        if field is not None:
            if field not in shrunk:
                continue
            shrunk.pop(field)
            shrunk["shed"] = sorted(set(shrunk.get("shed", [])) | {field})
        line = json.dumps(shrunk, sort_keys=True, separators=(",", ":")) + "\n"
        if len(line.encode("utf-8")) <= MAX_JOURNAL_LINE_BYTES:
            return line
    return None


def _last_byte(descriptor: int, offset: int) -> bytes:
    """Read one byte at *offset* on both supported platforms.

    ``os.pread`` does not exist on Windows, where the repair below would
    otherwise silently do nothing. Seeking is safe here because ``O_APPEND``
    pins the following write to the end of the file regardless of position.
    """

    reader = getattr(os, "pread", None)
    if reader is not None:
        return bytes(reader(descriptor, 1, offset))
    os.lseek(descriptor, offset, os.SEEK_SET)
    return os.read(descriptor, 1)


def _repair_prefix(descriptor: int) -> bytes:
    """Return a separator when a killed writer left the file mid-line.

    Without it the next record would be concatenated onto the partial line and
    both would be unreadable, so one interrupted session would cost two
    records instead of one.
    """

    try:
        size = os.fstat(descriptor).st_size
        if size == 0 or _last_byte(descriptor, size - 1) == b"\n":
            return b""
    except (OSError, ValueError):
        return b""
    return b"\n"


def append(entry: dict[str, Any], home: str | os.PathLike[str] | None = None) -> bool:
    """Append one record and report whether it was written.

    This never raises. An observation layer that could fail a ``graphctl``
    command, a session start, or a session stop would cost more than the
    observation is worth, so every failure degrades to ``False``.
    """

    try:
        if not isinstance(entry, dict):
            return False
        line = render(entry)
        if line is None:
            return False
        path = month_path(str(entry.get("ts") or utc_now()), home)
        path.parent.mkdir(parents=True, exist_ok=True)
        if is_link_like(path):
            return False
        with _serialized(path):
            _write_line(path, line)
        return True
    except (
        OSError,
        ValueError,
        TypeError,
        AttributeError,
        RuntimeError,
        RecursionError,
    ):
        return False


@contextmanager
def _serialized(path: Path) -> Iterator[None]:
    """Serialize appends only where the platform cannot do it in the kernel.

    On POSIX an ``O_APPEND`` write below the pipe-buffer size is positioned by
    the kernel and needs no coordination. CPython on Windows implements append
    as a seek followed by a write in user space, so two processes can resolve
    the same offset and lose a record; there the existing file lock stands in.
    """

    if os.name != "nt":
        yield
        return
    from .storage import FileLock

    with FileLock(path.with_name(path.name + ".lock")):
        yield


def _write_line(path: Path, line: str) -> None:
    """Append one already-bounded line, repairing an interrupted predecessor."""

    # Read access exists only so the writer can see whether a killed
    # predecessor left the file mid-line; O_APPEND still pins the write to the
    # end of the file.
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, _repair_prefix(descriptor) + line.encode("utf-8"))
    finally:
        os.close(descriptor)


def month_files(home: str | os.PathLike[str] | None = None) -> list[Path]:
    """Return the journal's month files, oldest first.

    Only ``YYYY-MM.jsonl`` names count. Anything else a person or another tool
    leaves in the directory must not be read as history, and must not consume
    the retention budget and push a real month out of it.
    """

    try:
        directory = journal_directory(home)
        candidates = sorted(directory.glob("*.jsonl"))
    except (HarnessError, OSError, ValueError, RuntimeError):
        return []
    return [path for path in candidates if MONTH_NAME.fullmatch(path.name)]


def read(home: str | os.PathLike[str] | None = None) -> Iterator[dict[str, Any]]:
    """Yield every record that parses and names an event, oldest month first.

    Unparseable lines are skipped rather than raised on: a truncated tail from
    a killed writer must not make the whole history unreadable. Records naming
    an event this version does not know are still yielded, so a reader from an
    older release does not silently hide a newer one's history.
    """

    for month in month_files(home):
        try:
            # A killed writer can cut a line mid-character, and a short
            # write can too. Strict decoding would turn one damaged byte
            # into an unhandled error for every reader of the history,
            # including the doctor command the recovery procedure relies
            # on, so damage is replaced rather than raised.
            handle = month.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and isinstance(entry.get("event"), str):
                    yield entry


def prune(
    keep_months: int = MAX_JOURNAL_MONTHS,
    home: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Delete journal months beyond *keep_months* and report what was removed."""

    if keep_months < 1:
        raise ValueError("keep_months must be at least 1")
    months = month_files(home)
    removed: list[str] = []
    for month in months[: max(0, len(months) - keep_months)]:
        try:
            month.unlink()
        except OSError:
            continue
        removed.append(month.name)
    return removed


def total_bytes(home: str | os.PathLike[str] | None = None) -> int:
    """Return the size of the retained journal, or zero when it is absent."""

    total = 0
    for month in month_files(home):
        try:
            total += month.stat().st_size
        except OSError:
            continue
    return total
