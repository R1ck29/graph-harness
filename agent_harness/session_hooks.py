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

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import journal, worktree
from .errors import HarnessError
from .paths import repository_key, repository_key_fast

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

# Only the hash of an edited path is recorded, never the path. A path is
# content enough: `clients/acme/contract.md` names a customer in its filename,
# and docs/security.md forbids recording user content. Twelve hex characters
# distinguish the files one session touched without carrying back anything
# that could identify them.
PATH_ID_CHARACTERS = 12

# The editing tools this harness treats as evidence. Kept here so the
# installer writes one matcher and the reader tests one list.
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


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


def repository(payload: dict[str, Any], fast: bool = False) -> Path:
    """Return the directory a session's work belongs to.

    ``CLAUDE_PROJECT_DIR`` is preferred over the payload's ``cwd`` because the
    payload reports where the process happens to be at hook time, which moves
    when the session changes directory, while the observation needs one stable
    identity for the whole session.

    *fast* selects the key derivation that does not spawn git. It is for the
    edit hook, which runs after every editing tool call; the boundary hooks
    run once each and take the git call.
    """

    derive = repository_key_fast if fast else repository_key
    for candidate in (
        os.environ.get("CLAUDE_PROJECT_DIR"),
        payload.get("cwd"),
    ):
        if isinstance(candidate, str) and candidate:
            return Path(derive(candidate))
    return Path(derive())


def path_id(repo: str | os.PathLike[str], path: str) -> str:
    """Return a stable, non-reversible identifier for one edited path.

    Relative to the repository so the same file yields one value however the
    client named it, and hashed so the journal can count distinct files
    without holding anything that says which files they were.
    """

    try:
        relative = str(Path(path).resolve().relative_to(Path(repo).resolve()))
    except (OSError, ValueError):
        # A path outside the repository is still one distinct file. It is
        # hashed whole rather than dropped, because a session that edits
        # outside the project has still edited something.
        relative = str(path)
    return hashlib.sha256(relative.encode("utf-8", "replace")).hexdigest()[
        :PATH_ID_CHARACTERS
    ]


def record_edit() -> int:
    """Record that an editing tool ran, and always return 0.

    This is the cheapest producer here, because it runs after every edit
    rather than once per turn: read stdin, derive the repository key, hash one
    string, append one line. It takes no snapshot, which would cost about
    130 ms and several git calls each time.

    It reports success unconditionally. A `PostToolUse` hook that failed would
    surface as a broken tool call, and the record is lost either way.
    """

    payload = read_payload(sys.stdin)
    if payload is None:
        return 0
    tool = payload.get("tool_name")
    # A read is not authored work. The producer accepted any tool carrying a
    # file path, so a Read was recorded as an edit; leaving the filtering to
    # the installer's matcher made the record mean whatever the client
    # happened to be configured with.
    if not isinstance(tool, str) or tool not in EDIT_TOOLS:
        return 0
    target = payload.get("tool_input")
    named = target.get("file_path") if isinstance(target, dict) else None
    if not isinstance(named, str) or not named:
        return 0
    try:
        root = repository(payload, fast=True)
        journal.append(
            journal.record(
                "edit",
                client=client_name(),
                session_id=payload.get("session_id"),
                repo=str(root),
                tool=tool,
                path_id=path_id(root, named),
            )
        )
    except Exception:  # noqa: BLE001 - a lost record must not break an edit
        return 0
    return 0


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


def observation_sound(records: list[dict[str, Any]]) -> bool:
    """Report whether a session was observed well enough to be judged at all.

    Two ways it is not. A degraded snapshot is not evidence in either
    direction. And a snapshot taken while git had a merge, rebase, cherry-pick
    or revert still open describes a tree git filled in, not one the session
    wrote; because the size charged is a maximum across boundaries, a single
    such boundary would otherwise accuse a session that aborted the operation
    and left the repository exactly as it found it.
    """

    snapshots = _snapshots(records)
    if not snapshots:
        return False
    return not any(
        shot.get("degraded") or shot.get("operation") or not shot.get("git")
        for shot in snapshots
    )


def _snapshots(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        snapshot
        for snapshot in (record.get("snapshot") for record in records)
        if isinstance(snapshot, dict)
    ]


def edited(records: list[dict[str, Any]]) -> bool:
    """Report whether one session's own records show it changed code.

    Two terms, and both are needed. An edit event is direct evidence that this
    session's agent wrote a file. A working tree that differs between two
    consecutive turn boundaries covers edits made through the shell, which is
    how a session that ignores the protocol is likely to edit.

    Neither term is a head move. Comparing the two ends of a session would
    also miss work that was committed before it closed, which is why the
    comparison runs over every boundary rather than over the outer pair.
    """

    if any(record.get("event") == "edit" for record in records):
        return True
    snapshots = _snapshots(records)
    if not any(
        worktree.dirty_changed(before, after)
        for before, after in zip(snapshots, snapshots[1:])
    ):
        return False
    # The tree moved, but moving it is not the same as adding to it. Throwing
    # away dirt that was already there — `git checkout -- .`, `git clean`, a
    # hard reset — changes the digest while authoring nothing, and was
    # classified as a bypass of zero files and zero lines.
    files, lines = session_size(records)
    return files > 0 or lines > 0


def session_size(records: list[dict[str, Any]]) -> tuple[int, int]:
    """Return how much one session changed, as a magnitude and never a trigger.

    The maximum across boundaries rather than the difference between the two
    ends: a session that edits and then commits returns the tree to clean, and
    the outer pair would size that work at zero. A tree that was already dirty
    when the session started is not the session's doing, so the first
    boundary is the baseline rather than absolute counts.
    """

    distinct = {
        record.get("path_id")
        for record in records
        if record.get("event") == "edit" and isinstance(record.get("path_id"), str)
    }
    snapshots = _snapshots(records)
    if not snapshots:
        return (len(distinct), 0)
    first = snapshots[0]
    files = max(
        (
            worktree.recorded_count(shot.get("files"))
            - worktree.recorded_count(first.get("files"))
            for shot in snapshots
        ),
        default=0,
    )
    lines = max(
        (
            worktree.recorded_count(shot.get("lines"))
            - worktree.recorded_count(first.get("lines"))
            for shot in snapshots
        ),
        default=0,
    )
    return (max(len(distinct), files, 0), max(lines, 0))


def worth_reporting(records: list[dict[str, Any]]) -> tuple[bool, int, int]:
    """Judge whether one session's work is large enough to mention, and how large."""

    if not observation_sound(records) or not edited(records):
        return (False, 0, 0)
    files, lines = session_size(records)
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
    owned: dict[str, list[dict[str, Any]]] = {}
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
        if event not in {"session_open", "turn_end", "session_close", "edit"}:
            continue
        owned.setdefault(session, []).append(entry)
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
        reportable, files, lines = worth_reporting(owned.get(session, []))
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


def edit_entrypoint() -> int:
    """Console entry point for the post-edit hook."""

    from .claude_hook import ensure_supported_python

    ensure_supported_python(sys.version_info[:3])
    return record_edit()
