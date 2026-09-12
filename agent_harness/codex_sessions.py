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
from .graph import utc_from_epoch

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
                "started_at": _timestamp(created_at),
                "ended_at": _timestamp(updated_at),
                "kind": _kind(source),
            }
    except sqlite3.Error as exc:
        raise HarnessError(
            f"cannot read the Codex session store at {path}: {exc}"
        ) from exc
    finally:
        connection.close()


def _timestamp(value: Any) -> str | None:
    """Return one of this harness's own timestamps, or None when there is none.

    Rendered into the hooks' form rather than passed through as the integer
    Codex stores. A Codex session's verdict is read beside a hook-observed
    one, in the same list and under the same key, and two forms in one field
    left every consumer to guard for both on its own: `_overlaps` refused to
    compare a Codex session at all, so no Codex session could ever be
    reported as contested however many ran together, and `report` ordered
    them by comparing "1788088667" against "2026-09-11T..." as text, which
    puts every Codex session before every other one whatever its date.

    Nothing is lost in the conversion: Codex keeps whole seconds and
    `utc_now` keeps whole seconds, so the two are the same instant to the
    same precision.
    """

    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError as well as the other two: the column is REAL, and
        # SQLite round-trips 9e999 to `inf`, which `int` refuses that way.
        # Raising here escaped `sessions()` and was swallowed by the blanket
        # handler in `conformance._codex_verdicts`, which drops *every* Codex
        # session rather than the one with the bad column.
        return None
    # Codex carries both second and millisecond columns; a value far past the
    # plausible second range is a millisecond one.
    if number > 100_000_000_000:
        number //= 1000
    try:
        return utc_from_epoch(number)
    except (OSError, OverflowError, ValueError):
        # All three arms are reachable, which is why the tuple is not
        # narrowed: after the millisecond division a column of 10**18 raises
        # ValueError, 10**21 raises OSError, and 10**24 raises OverflowError.
        # An undocumented column can hold a number outside the range the
        # platform can render as a date. The session is still reported — that
        # it ran, and where, is the useful part — with no bounds, which is
        # what every consumer already handles for a missing one.
        return None


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
