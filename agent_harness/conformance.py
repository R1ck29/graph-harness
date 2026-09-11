"""Fold session observations into a verdict per session.

The session hooks answer one question at one moment: did the session that
just finished skip the graph? This answers the same question about every
session on record, which is what makes a rate rather than an anecdote.

What a session is judged *on* is shared by calling the same functions rather
than by keeping two copies in step: its edit records decide whether anything
is attributable to it, and its sizes are counted from those same records. How
a session is *placed in time* is not shared — ``session_hooks.previous_bypass``
walks the journal again for its own purposes, and the two windowing
implementations agree only by inspection. Where the hooks stay silent, this
reports what it saw and how much it trusts it, because a report a person reads
can carry doubt that a one-line warning cannot.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

from . import codex_sessions, journal, session_hooks, worktree

# Version 3 adds `bounds`, which says what a verdict's two timestamps
# describe. Version 2 was the shape before that: `read_only` became
# `unattributed`, `changed_lines` became `edits`, and `outside_edits` and the
# tree context fields were added. A reader handed a report needs to know which
# shape it holds, and the number is the only thing that says so.
SCHEMA_VERSION = 3

# What a verdict's `started_at` and `ended_at` describe. A reader comparing
# two sessions has to know this: one pair is when the session was observed to
# open and close, the other is the lifetime of a row in another tool's store,
# and only the first can support a claim that two sessions ran together.
BOUNDS_OBSERVED = "observed"
BOUNDS_THREAD_LIFETIME = "thread_lifetime"

# What a verdict is worth. A session the client recorded closing was observed
# to its end; one that only opened may still have been running when the record
# was read. Snapshots reach no confidence branch, because they reach no
# branch at all.
HIGH, MEDIUM, LOW = "high", "medium", "low"

VERDICTS = (
    "conformant",
    "bypass",
    "bypass_suspected",
    # Something may well have changed, but no edit record says this session's
    # agent did it. Replaces `read_only`, which claimed more than the evidence
    # supports about every session recorded before the edit hook existed.
    "unattributed",
    "incomplete",
    "unobserved",
)


def _windows(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Collect each session's boundaries, transitions and journal positions."""

    sessions: dict[str, dict[str, Any]] = {}
    transitions: list[tuple[int, str]] = []
    attributed: dict[str, list[int]] = {}
    for position, entry in enumerate(records):
        repo = entry.get("repo")
        event = entry.get("event")
        if not isinstance(repo, str):
            continue
        if event == "graph_transition":
            # A run that names its session is evidence for that session
            # wherever it landed. Records from separate processes are not
            # ordered by causality, so a run flushed after its own session
            # closed would otherwise fall outside every window and the
            # session that used the graph would be reported as one that
            # did not.
            named = entry.get("session_id")
            if isinstance(named, str) and named:
                attributed.setdefault(named, []).append(position)
            else:
                transitions.append((position, repo))
            continue
        session = entry.get("session_id")
        if not isinstance(session, str) or event not in journal.OWNED_EVENTS:
            continue
        state = sessions.setdefault(
            session,
            {
                "session_id": session,
                "client": entry.get("client"),
                "repo": repo,
                "opened": None,
                "ended": None,
                # Journal positions, not timestamps: `_judge` reports the
                # timestamps from the records themselves under the same two
                # names, and one key meaning two things in one module is how
                # a reader ends up comparing a position against a clock.
                "open_position": None,
                "end_position": None,
                "closed": False,
                "records": [],
            },
        )
        state["records"].append(entry)
        if event == "edit":
            continue
        if event == "session_open":
            if state["opened"] is None:
                state["opened"] = entry
                state["open_position"] = position
        else:
            state["ended"] = entry
            state["end_position"] = position
            state["closed"] = state["closed"] or event == "session_close"
    for session, state in sessions.items():
        started, ended = state["open_position"], state["end_position"]
        state["transitions"] = sum(
            1
            for position in attributed.get(session, ())
            # A named run counts from its session's own start onwards; an
            # identifier is reused when a client resumes.
            if started is not None and position > started
        ) + sum(
            1
            for position, repo in transitions
            if repo == state["repo"]
            and started is not None
            and ended is not None
            and started < position < ended
        )
    return sessions


def _judge(state: dict[str, Any]) -> dict[str, Any]:
    """Decide one session's verdict from its own boundaries."""

    opened, ended = state["opened"], state["ended"]
    verdict = {
        "session_id": state["session_id"],
        "client": state["client"] or "unknown",
        "repo": state["repo"],
        "started_at": (opened or {}).get("ts"),
        "ended_at": (ended or {}).get("ts"),
        "transitions": state["transitions"],
        "changed_files": 0,
        "edits": 0,
        "outside_edits": 0,
        "confidence": LOW,
        "bounds": BOUNDS_OBSERVED,
    }
    if opened is None:
        # Something happened in this session, but its beginning was never
        # recorded, so there is no baseline to compare against.
        verdict["verdict"] = "unobserved"
        return verdict
    if ended is None:
        verdict["verdict"] = "incomplete"
        return verdict
    files, calls = session_hooks.session_size(state["records"])
    verdict["changed_files"] = files
    verdict["edits"] = calls
    # Reported beside the project's own, never folded into it: a session that
    # wrote only to a scratchpad has not touched this repository, and saying
    # so is more useful than either counting it or hiding it.
    verdict["outside_edits"] = (
        len(session_hooks.edits(state["records"], include_outside=True)) - calls
    )
    verdict["confidence"] = HIGH if state["closed"] else MEDIUM
    # The tree is reported as context and decides nothing. Both counts below
    # come from the snapshots, and no branch here reads them.
    before, after = opened.get("snapshot"), ended.get("snapshot")
    verdict["tree_changed"] = worktree.dirty_changed(before, after)
    verdict["tree_files"], verdict["tree_lines"] = _tree_context(before, after)
    if not session_hooks.edited(state["records"]):
        # Without an edit record nothing says who changed what, and the tree
        # cannot tell a session that read code from one that wrote it through
        # the shell. Saying so is the only honest verdict; calling it
        # read_only would be a claim about a session nobody observed writing.
        verdict["verdict"] = "unattributed"
        return verdict
    verdict["verdict"] = "conformant" if state["transitions"] else "bypass"
    return verdict


def _tree_context(before: Any, after: Any) -> tuple[int, int]:
    """Describe how the tree moved, for a reader. Never for a decision."""

    if not isinstance(before, dict) or not isinstance(after, dict):
        return (0, 0)
    files = worktree.recorded_count(after.get("files")) - worktree.recorded_count(
        before.get("files")
    )
    lines = worktree.recorded_count(after.get("lines")) - worktree.recorded_count(
        before.get("lines")
    )
    return (max(files, 0), max(lines, 0))


def _contested(verdicts: list[dict[str, Any]]) -> None:
    """Flag sessions whose repository had another *observed* session with them.

    Concurrent sessions share one working tree, so neither one's change can be
    attributed to it alone. Saying so is more useful than quietly reporting a
    number that may belong to the other.

    Only sessions whose window was observed take part, which is what the
    `bounds` field on each verdict says. A Codex verdict's window comes from
    the store's `created_at` and `updated_at`, and `updated_at` is the thread
    row's last-modified time rather than the moment the session ended: on a
    real store of 137 threads the median span is six minutes but thirteen
    span more than a day and the longest is twenty-four. Letting those
    windows contest anything marked 118 of those 137 as contested by each
    other, and marked a fully observed session — one with its own edit
    records and a graph transition inside its window — as contested by a
    Codex row whose lifetime merely spanned it. That session's changes are
    attributable to it alone, so the flag was saying something false.
    """

    by_repo: dict[str, list[dict[str, Any]]] = {}
    for verdict in verdicts:
        verdict["contested"] = False
        if verdict.get("bounds") == BOUNDS_OBSERVED:
            by_repo.setdefault(verdict["repo"], []).append(verdict)
    for group in by_repo.values():
        for first in group:
            for second in group:
                if first is second:
                    continue
                if _overlaps(first, second):
                    first["contested"] = True
                    break


def _overlaps(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """Report whether two sessions were open at the same time.

    The check is on form *and* presence, and both halves are load-bearing.
    Presence, because a session whose opening or closing was never recorded
    cannot be placed against another one and guessing would invent an overlap
    rather than find one. Form, because `journal.read` validates only the
    `event` field: a forged or corrupt record carrying an integer `ts`
    reaches this key through `_judge` unchanged, and comparing it against a
    date raises. Replacing this with a presence-only check was tried and
    `report` died with a TypeError on exactly that input.

    What it is no longer for is telling two timestamp *sources* apart.
    `codex_sessions` renders its own into the form the hooks record, so that
    difference is gone — but a Codex window is a thread row's lifetime rather
    than a run, so `_contested` excludes those before reaching here. See
    there for why.
    """

    bounds = [
        first.get("started_at"),
        first.get("ended_at"),
        second.get("started_at"),
        second.get("ended_at"),
    ]
    if not all(isinstance(value, str) for value in bounds):
        return False
    first_start, first_end, second_start, second_end = (str(value) for value in bounds)
    return first_start <= second_end and second_start <= first_end


def _codex_verdicts(home: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    """Describe Codex sessions from Codex's own record.

    Codex documents the hook events this harness installs, but the clients
    measured here parse that configuration without running it, so no working
    tree is ever observed for a Codex session. What can be said is that the
    session ran, where, and whether any graph state moved while it did. That
    is reported as a suspicion rather than a finding.
    """

    try:
        sessions = codex_sessions.sessions(home)
    except Exception:  # noqa: BLE001 - a report must survive a missing client
        return []
    return [
        {
            "session_id": session["session_id"],
            "client": "codex",
            "repo": session["repo"],
            "started_at": session["started_at"],
            "ended_at": session["ended_at"],
            "transitions": 0,
            "changed_files": 0,
            "edits": 0,
            "outside_edits": 0,
            "confidence": LOW,
            "contested": False,
            # Not `observed`: `updated_at` is when the thread row was last
            # written, not when the session ended, so this window cannot
            # support a claim about what ran alongside what.
            "bounds": BOUNDS_THREAD_LIFETIME,
            "verdict": "bypass_suspected",
            "note": (
                "Codex hooks do not run on the measured clients, so this "
                "session's working tree was never observed, and the times "
                "below are the lifetime of a row in Codex's store rather "
                "than the start and end of a run."
            ),
        }
        for session in sessions
        if session.get("kind") != "subagent"
    ]


def report(
    home: str | os.PathLike[str] | None = None,
    repo: str | None = None,
    min_files: int = 0,
    include_codex: bool = True,
) -> dict[str, Any]:
    """Return one verdict per observed session, with totals.

    This reads and never writes. A command that reported on the protocol by
    changing the record it reports on would be worth nothing.
    """

    verdicts = [_judge(state) for state in _windows(journal.read(home)).values()]
    if include_codex:
        verdicts.extend(_codex_verdicts(home))
    _contested(verdicts)
    if repo is not None:
        verdicts = [item for item in verdicts if item["repo"] == repo]
    if min_files > 0:
        verdicts = [
            item
            for item in verdicts
            if item["changed_files"] >= min_files or item["verdict"] == "unobserved"
        ]
    verdicts.sort(
        key=lambda item: (str(item.get("started_at") or ""), item["session_id"])
    )
    counts = {name: 0 for name in VERDICTS}
    for item in verdicts:
        counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
    judged = counts["conformant"] + counts["bypass"]
    return {
        "schema_version": SCHEMA_VERSION,
        "sessions": verdicts,
        "counts": counts,
        # Only sessions that changed something and could be judged carry a
        # rate. Counting the rest would move the number without saying
        # anything about whether the protocol was followed.
        "judged_sessions": judged,
        "conformance_rate": (
            round(counts["conformant"] / judged, 3) if judged else None
        ),
    }
