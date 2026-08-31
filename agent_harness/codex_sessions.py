"""Read Codex session boundaries from the client's own local state.

Codex documents the same hook events as Claude Code, but on the clients
measured here those hooks are parsed and never executed, so nothing the
harness installs can observe a Codex session. Codex does record every session
it runs — command line, terminal, desktop, and subagent alike — in a local
SQLite file, and that record is enough to say when a session ran and where.

Only session metadata is read. Prompts, titles, previews, and transcripts are
user content and are never opened, in line with the redaction rule in
``docs/security.md``.

Nothing here is a documented Codex interface. The store is versioned by a
generation number in its filename and its schema is undocumented, so a missing
column is reported rather than worked around: silently reporting "no Codex
sessions" would look exactly like a machine that runs none.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from .errors import HarnessError

CODEX_HOME_VARIABLE = "CODEX_HOME"
STATE_NAME = re.compile(r"state_([0-9]+)\.sqlite")

REQUIRED_COLUMNS = ("id", "cwd", "created_at", "updated_at", "source")

# Read-only, and bounded so a lock held by a running Codex cannot stall a hook.
CONNECT_TIMEOUT_SECONDS = 5.0

# Codex writes this column as a plain label for a user-driven session and as a
# JSON object for a subagent, so the raw value is classified rather than shown.
SUBAGENT_PREFIX = "{"


def codex_home(home: str | os.PathLike[str] | None = None) -> Path:
    """Return the Codex state directory this machine uses."""

    override = os.environ.get(CODEX_HOME_VARIABLE)
    if override:
        return Path(override)
    root = Path(home) if home is not None else Path.home()
    return root / ".codex"


def state_path(home: str | os.PathLike[str] | None = None) -> Path | None:
    """Return the highest-generation Codex state file, or None when absent.

    The generation is part of the filename. Codex starts a fresh file rather
    than migrating in place, so reading a lower generation would report a
    truncated history as if it were the whole one.
    """

    directory = codex_home(home)
    try:
        candidates = list(directory.glob("state_*.sqlite"))
    except OSError:
        return None
    generations: list[tuple[int, Path]] = []
    for candidate in candidates:
        matched = STATE_NAME.fullmatch(candidate.name)
        if matched is not None:
            generations.append((int(matched.group(1)), candidate))
    if not generations:
        return None
    return max(generations)[1]


def sessions(home: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    """Return every recorded Codex session as metadata only.

    Returns an empty list when Codex is not installed. Raises only when a
    store exists but cannot be read as expected, because that is a condition
    the operator needs to see rather than a machine with no Codex on it.
    """

    path = state_path(home)
    if path is None:
        return []
    return list(_read_sessions(path))


def _read_only_uri(path: Path) -> str:
    """Return a SQLite URI that keeps ``mode=ro`` for any legal path.

    ``#`` and ``?`` are legal in a directory name and are URI delimiters, so
    interpolating a path directly truncates the URI at the first one and
    discards the read-only flag. SQLite then opens a writable connection and
    creates an empty database at the truncated path, which turns a read-only
    reader into a writer and misreports a healthy store as one whose schema
    has changed.
    """

    return "file:" + quote(str(path), safe="/\\:") + "?mode=ro"


def _connect(path: Path) -> sqlite3.Connection:
    try:
        return sqlite3.connect(
            _read_only_uri(path),
            uri=True,
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
    except sqlite3.Error as exc:
        raise HarnessError(
            f"cannot read the Codex session store at {path}: {exc}"
        ) from exc


def _read_sessions(path: Path) -> Iterator[dict[str, Any]]:
    connection = _connect(path)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
        missing = [name for name in REQUIRED_COLUMNS if name not in columns]
        if missing:
            raise HarnessError(
                f"the Codex session store at {path} has no "
                f"{', '.join(missing)} in its threads table; this harness reads an "
                "undocumented Codex format that has changed, so Codex sessions "
                "cannot be reported until it is updated"
            )
        rows = connection.execute(
            "SELECT id, cwd, created_at, updated_at, source FROM threads "
            "ORDER BY created_at"
        )
        for identifier, cwd, created_at, updated_at, source in rows:
            yield {
                "client": "codex",
                "session_id": str(identifier),
                "repo": str(cwd),
                "started_at": _epoch(created_at),
                "ended_at": _epoch(updated_at),
                "kind": _kind(source),
            }
    except sqlite3.Error as exc:
        raise HarnessError(
            f"cannot read the Codex session store at {path}: {exc}"
        ) from exc
    finally:
        connection.close()


def _epoch(value: Any) -> int | None:
    """Return a whole-second timestamp, tolerating the millisecond columns."""

    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    # Codex carries both second and millisecond columns; a value far past the
    # plausible second range is a millisecond one.
    return number // 1000 if number > 100_000_000_000 else number


def _kind(source: Any) -> str:
    """Classify a session without exposing the raw column.

    A subagent session is recorded as a JSON object rather than a label, so a
    consumer that treated the column as an enum would leak a structure it
    cannot read and would misreport the session kind.
    """

    if not isinstance(source, str) or not source:
        return "unknown"
    if source.startswith(SUBAGENT_PREFIX):
        return "subagent"
    return source
