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
from typing import Any, Callable

from . import journal, worktree
from .errors import HarnessError
from .paths import repository_key, repository_key_fast

MAX_HOOK_INPUT_BYTES = 1_048_576

# A session below both thresholds is recorded but not warned about. The
# protocol governs non-trivial work, and a harness that objects to a one-line
# fix trains people to ignore it.
#
# Both are counted from edit records. Nothing the working tree reports may
# reach this decision: six review rounds were spent on what a tree comparison
# was allowed to conclude, and every one of them found another git command
# that moves a tree without anybody authoring anything.
WARN_MIN_FILES = 2
WARN_MIN_EDITS = 5

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


def path_id(repo: str | os.PathLike[str], path: str) -> tuple[str, bool]:
    """Identify one edited path, and say whether it lies outside *repo*.

    Relative to the repository so the same file yields one value however the
    client named it, and hashed so the journal can count distinct files
    without holding anything that says which files they were.

    A path outside the repository is still recorded — a session that edited
    something has edited something — but it is flagged, because it is not
    work on *this* project. Counting a scratchpad or a note in a home
    directory toward the project's thresholds made heavy scratchpad use read
    as unrecorded work on the repository, which is an accusation the evidence
    does not support.
    """

    outside = False
    try:
        relative = str(Path(path).resolve().relative_to(Path(repo).resolve()))
    except (OSError, ValueError):
        relative, outside = str(path), True
    digest = hashlib.sha256(relative.encode("utf-8", "replace")).hexdigest()
    return digest[:PATH_ID_CHARACTERS], outside


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
    # NotebookEdit names its target `notebook_path`, not `file_path`. It was
    # in the tool list and in the installer matcher while its edits were
    # dropped, so a notebook-only session reported exactly like one that
    # never edited at all.
    named = None
    if isinstance(target, dict):
        for field in ("file_path", "notebook_path"):
            value = target.get(field)
            if isinstance(value, str) and value:
                named = value
                break
    if named is None:
        return 0
    try:
        root = repository(payload, fast=True)
        identifier, outside = path_id(root, named)
        journal.append(
            journal.record(
                "edit",
                client=client_name(),
                session_id=payload.get("session_id"),
                repo=str(root),
                tool=tool,
                path_id=identifier,
                # Recorded only when true, so an ordinary edit keeps the
                # record it always had and no reader has to know the flag.
                outside=True if outside else None,
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


def observation_sound(records: list[dict[str, Any]]) -> bool:
    """Report whether a session was observed well enough to be judged at all.

    Only the boundaries matter now. A session whose opening or closing record
    is missing cannot be placed in time, which is what attribution needs; what
    the working tree looked like is no longer part of any judgement.
    """

    return any(record.get("event") == "session_open" for record in records)


def edits(
    records: list[dict[str, Any]], include_outside: bool = False
) -> list[dict[str, Any]]:
    """Return this session's edit records, by default only the project's own."""

    return [
        record
        for record in records
        if record.get("event") == "edit"
        and (include_outside or not record.get("outside"))
    ]


def edited(records: list[dict[str, Any]]) -> bool:
    """Report whether this session's agent wrote a file.

    One term, and it is the only one that ever answered the question. An edit
    record says which session's agent invoked an editing tool; a working-tree
    comparison says a tree changed and can never say who changed it. Six
    rounds of review found six different git commands that move a tree with
    nobody authoring anything — a pull, a conflicted merge, a stash pop, a
    submodule update, a discard, an apply — and each fix for one exposed the
    next.

    The cost is stated rather than hidden: a session that edits only through
    the shell produces no edit record and is not reported. It is recorded as
    `unattributed` instead of being called read-only, because something
    plainly changed and claiming otherwise would be the same kind of lie in
    the other direction.
    """

    return bool(edits(records))


def session_size(records: list[dict[str, Any]]) -> tuple[int, int]:
    """Return how many distinct files this session wrote, and how many times.

    Counted from edit records alone, so the number that crosses the warning
    threshold cannot be moved by anything a git command did to the tree. The
    report still carries the tree's own file and line counts beside this, as
    context a person can read; nothing decides on them.
    """

    recorded = edits(records)
    distinct = {
        record.get("path_id")
        for record in recorded
        if isinstance(record.get("path_id"), str)
    }
    return (len(distinct), len(recorded))


def worth_reporting(records: list[dict[str, Any]]) -> tuple[bool, int, int]:
    """Judge whether one session's work is large enough to mention, and how large."""

    if not observation_sound(records) or not edited(records):
        return (False, 0, 0)
    files, calls = session_size(records)
    return (files >= WARN_MIN_FILES or calls >= WARN_MIN_EDITS, files, calls)


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
        if event not in journal.OWNED_EVENTS:
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
        if session in reported:
            continue
        reportable, files, calls = worth_reporting(owned.get(session, []))
        if not reportable:
            continue
        return {
            "session_id": session,
            "repo": repo,
            "ended_at": ended.get("ts"),
            "changed_files": files,
            "edits": calls,
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
        "The previous session in this repository edited "
        f"{bypass['changed_files']} file(s) in {bypass['edits']} tool call(s) "
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


def _entrypoint(handler: Callable[[], int]) -> int:
    """Check the interpreter, then run one hook.

    Written once so the floor cannot be enforced on two of the three hooks
    and forgotten on the third. The import stays inside the function, as it
    was in each of the three copies this replaces.
    """

    from .claude_hook import ensure_supported_python

    ensure_supported_python(sys.version_info[:3])
    return handler()


def start_entrypoint() -> int:
    """Console entry point for the session-start hook."""

    return _entrypoint(session_start)


def end_entrypoint() -> int:
    """Console entry point for the session-end hook."""

    return _entrypoint(session_end)


def edit_entrypoint() -> int:
    """Console entry point for the post-edit hook."""

    return _entrypoint(record_edit)
