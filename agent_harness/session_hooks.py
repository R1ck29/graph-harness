"""Session and turn boundary hooks shared by both supported clients.

Claude Code and Codex deliver the same hook payload on standard input, so one
implementation serves both. What differs is that the Codex clients measured
here parse their hook configuration and never execute it; those sessions are
recovered from Codex's own store instead.

The bypass warning is reported at the next session start rather than at the
end of the offending one. ``SessionEnd`` runs on a short shared budget, its
exit code is ignored, and it does not run at all when the process is killed,
so nothing said there reliably reaches anyone. ``SessionStart`` can print to
standard error and be seen without blocking the session.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from . import journal, worktree
from .errors import HarnessError
from .paths import repository_key

MAX_HOOK_INPUT_BYTES = 1_048_576

# A session below both thresholds is recorded but not warned about. The
# protocol governs non-trivial work, and a harness that objects to a one-line
# fix trains people to ignore it.
WARN_MIN_FILES = 2
WARN_MIN_LINES = 20

# How far back a session start will look. The scan walks past sessions not
# worth reporting, and each one it weighs costs a git call, so without a bound
# one start could weigh a whole retained history and then announce something
# from weeks ago as though it had just happened. What is older than this
# belongs to `graphctl conformance`, which is built to show a history.
MAX_CANDIDATES = 5

# A stale lock from a killed hook should cost a moment, not the full wait a
# graph write is given: losing this race only means saying nothing.
REPORT_LOCK_TIMEOUT_SECONDS = 1.0


def read_payload(stream: Any) -> dict[str, Any] | None:
    """Return the hook payload, or None when it is unusable."""

    try:
        raw = stream.read(MAX_HOOK_INPUT_BYTES + 1)
    except (OSError, ValueError):
        return None
    if len(raw.encode("utf-8")) > MAX_HOOK_INPUT_BYTES:
        return None
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def client_name() -> str:
    """Name the client running this hook from its own environment."""

    if os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_PROJECT_DIR"):
        return "claude"
    if os.environ.get("CODEX_HOME"):
        return "codex"
    return "unknown"


def repository(payload: dict[str, Any]) -> Path:
    """Return the directory a session's work belongs to.

    ``CLAUDE_PROJECT_DIR`` is preferred over the payload's ``cwd`` because the
    payload reports where the process happens to be at hook time, which moves
    when the session changes directory, while the observation needs one stable
    identity for the whole session.
    """

    for candidate in (
        os.environ.get("CLAUDE_PROJECT_DIR"),
        payload.get("cwd"),
    ):
        if isinstance(candidate, str) and candidate:
            return Path(repository_key(candidate))
    return Path(repository_key())


def observe(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Build and append one boundary record, returning what was recorded."""

    root = repository(payload)
    entry = journal.record(
        event,
        client=client_name(),
        session_id=payload.get("session_id"),
        repo=str(root),
        source=payload.get("source"),
        reason=payload.get("reason"),
        snapshot=worktree.snapshot(root),
    )
    journal.append(entry)
    return entry


def judgeable(opened: dict[str, Any], ended: dict[str, Any]) -> bool:
    """Report whether two boundaries are sound enough to draw a conclusion from.

    A degraded snapshot is not evidence in either direction. Two degraded ones
    can hash equal and hide a real edit, and one degraded endpoint can lose
    ``HEAD`` and manufacture a change that never happened. Silence is the only
    honest answer when the observation itself failed.
    """

    before, after = opened.get("snapshot"), ended.get("snapshot")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    if before.get("degraded") or after.get("degraded"):
        return False
    return bool(before.get("git")) and bool(after.get("git"))


def worth_reporting(repo: str, before: Any, after: Any) -> tuple[bool, int, int]:
    """Judge whether a change is large enough to mention, and how large.

    Committing returns the working tree to a clean state, so the snapshots
    alone say only that ``HEAD`` moved; judged on the tree they describe, the
    most deliberate kind of change would go unmentioned. Git is asked what
    actually moved between the two commits, which counts the work and leaves
    a pull, a checkout, or a commit made in another terminal at the size they
    really are.
    """

    if not worktree.changed(before, after):
        return (False, 0, 0)
    files, lines = worktree.change_size(before, after)
    committed_files, committed_lines = worktree.committed_size(repo, before, after)
    files = max(files, committed_files)
    lines = max(lines, committed_lines)
    return (files >= WARN_MIN_FILES or lines >= WARN_MIN_LINES, files, lines)


def previous_bypass(
    repo: str, since: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Report the newest finished session in *repo* that skipped the graph.

    Candidates are examined newest first and the scan continues past ones that
    are not worth reporting, because a session that is never reported is never
    recorded as reported: stopping at the newest would let one trivial session
    afterwards bury a real bypass permanently.

    The scan stops at the first session that used the graph. Everything older
    than that has already been answered, and repeating it is how a warning
    gets tuned out.

    Ordering is by journal position, not by timestamp. Timestamps carry one
    second of precision and a record can be missing one entirely, in which
    case it renders as a string that sorts above every real timestamp and
    takes over the selection.

    A ``graphctl`` run that names its session counts for it from that
    session's start onwards, wherever it was recorded; only an unnamed run is
    placed by position. Records from separate processes are not ordered by
    causality, so a run flushed after its own session closed would otherwise
    fall outside every window and the session that used the graph would be
    accused. One consequence is deliberate: the scan can be stopped by
    evidence recorded after the fact, so a stale identifier exported into a
    later shell makes that run answer for a finished session. The result is
    silence rather than a false accusation, which is the direction this
    module errs in throughout.
    """

    opens: dict[str, tuple[int, dict[str, Any]]] = {}
    ends: dict[str, tuple[int, dict[str, Any]]] = {}
    transitions: list[int] = []
    attributed: dict[str, list[int]] = {}
    reported: set[str] = set()
    history = list(journal.read())
    # The whole journal is read, and one index space is used for everything in
    # it. Reading part of it instead produced two defects in turn: a cut that
    # dropped the tail hid a concurrent claim, so both sessions reported the
    # same bypass; and reading claims whole while cutting transitions left a
    # graphctl run recorded late outside every window, which accused the
    # session that had used the graph.
    cut = _cut_position(history, since)
    for position, entry in enumerate(history):
        if entry.get("repo") != repo:
            continue
        event = entry.get("event")
        if event == "graph_transition":
            # A run that names its session is evidence for that session
            # wherever it was recorded. Only a run that names none has to be
            # placed by when it happened.
            named = entry.get("session_id")
            if isinstance(named, str) and named:
                attributed.setdefault(named, []).append(position)
            else:
                transitions.append(position)
            continue
        session = entry.get("session_id")
        if not isinstance(session, str):
            continue
        if event == "bypass_reported":
            reported.add(session)
            continue
        # Only which sessions may be judged depends on the cut. The asking
        # session is still running, so its own records say nothing yet.
        if position >= cut:
            continue
        if event == "session_open":
            opens.setdefault(session, (position, entry))
        elif event in {"turn_end", "session_close"}:
            ends[session] = (position, entry)

    finished = sorted(
        (
            (opens[session][0], ended_at, opens[session][1], ended)
            for session, (ended_at, ended) in ends.items()
            if session in opens
            # An end recorded before its own start describes nothing this can
            # reason about, so it is left alone rather than guessed at.
            and ended_at > opens[session][0]
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    for started_at, ended_at, opened, ended in finished[:MAX_CANDIDATES]:
        session = str(opened.get("session_id"))
        # A named run counts for its session wherever it was flushed, but only
        # from that session's own start onwards: a client reuses an identifier
        # when it resumes, and yesterday's run must not answer for today's
        # work.
        if any(
            position > started_at for position in attributed.get(session, ())
        ) or any(started_at < position < ended_at for position in transitions):
            # This session used the graph. Anything older has already been
            # answered by it, so the scan ends here rather than reaching back.
            return None
        if session in reported or not judgeable(opened, ended):
            continue
        reportable, files, lines = worth_reporting(
            repo, opened.get("snapshot"), ended.get("snapshot")
        )
        if not reportable:
            continue
        return {
            "session_id": session,
            "repo": repo,
            "ended_at": ended.get("ts"),
            "changed_files": files,
            "changed_lines": lines,
        }
    return None


def _cut_position(history: list[dict[str, Any]], since: dict[str, Any] | None) -> int:
    """Return the position of the asking session's own opening record.

    The match is made on the identity fields rather than on the whole record,
    because an over-long record is written with some fields shed and would
    then never match what the caller holds. When no match is found nothing is
    cut: excluding by name would drop every record that also lacks a name,
    including the graphctl runs that prove a session followed the protocol,
    and a session with no name can never become a candidate anyway.
    """

    if since is None:
        return len(history)
    wanted = (since.get("event"), since.get("session_id"), since.get("ts"))
    for index in range(len(history) - 1, -1, -1):
        candidate = history[index]
        key = (
            candidate.get("event"),
            candidate.get("session_id"),
            candidate.get("ts"),
        )
        if key == wanted:
            return index
    return len(history)


def _claim_bypass(repo: str, since: dict[str, Any]) -> dict[str, Any] | None:
    """Decide and claim one bypass, or return None when there is nothing to say.

    Deciding and claiming are one step under one lock. Two sessions starting
    at once would otherwise both read a journal in which the bypass is
    unclaimed and both report it.

    The claim must be written before anything is said, because the written
    record is the only thing that stops the same bypass being reported again.
    Saying it while failing to record it repeats forever.
    """

    from .storage import FileLock

    try:
        lock_path = journal.journal_directory() / "report.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except (HarnessError, OSError):
        return None
    with FileLock(lock_path, timeout=REPORT_LOCK_TIMEOUT_SECONDS):
        bypass = previous_bypass(repo, since=since)
        if bypass is None:
            return None
        claimed = journal.append(
            journal.record(
                "bypass_reported",
                client=client_name(),
                session_id=bypass["session_id"],
                repo=bypass["repo"],
            )
        )
    return bypass if claimed else None


def session_start() -> int:
    """Record a session opening and report the last bypass in this repository.

    A hook that raises is a hook the client reports as broken, and the record
    it was supposed to write is lost either way, so every step here degrades
    to doing nothing instead.
    """

    payload = read_payload(sys.stdin)
    if payload is None:
        return 0
    try:
        entry = observe("session_open", payload)
    except Exception:  # noqa: BLE001 - a lost record must not break a start
        return 0
    try:
        bypass = _claim_bypass(str(entry.get("repo")), entry)
    except Exception:  # noqa: BLE001 - reporting must not break a session start
        return 0
    if bypass is None:
        return 0
    print(
        "The previous session in this repository changed "
        f"{bypass['changed_files']} file(s) and {bypass['changed_lines']} line(s) "
        "without recording any task-graph state. If that work was non-trivial it "
        "should have gone through graphctl. Run 'graphctl conformance' for the "
        "full picture.",
        file=sys.stderr,
    )
    return 2


def session_end() -> int:
    """Record a session closing. Its exit code is ignored by the client."""

    payload = read_payload(sys.stdin)
    if payload is None:
        return 0
    try:
        observe("session_close", payload)
    except Exception:  # noqa: BLE001 - a lost record must not break a close
        return 0
    return 0


def start_entrypoint() -> int:
    """Console entry point for the session-start hook."""

    from .claude_hook import ensure_supported_python

    ensure_supported_python(sys.version_info[:3])
    return session_start()


def end_entrypoint() -> int:
    """Console entry point for the session-end hook."""

    from .claude_hook import ensure_supported_python

    ensure_supported_python(sys.version_info[:3])
    return session_end()
