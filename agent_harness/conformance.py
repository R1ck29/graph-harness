"""Fold session observations into a verdict per session.

The session hooks answer one question at one moment: did the session that
just finished skip the graph? This answers the same question about every
session on record, which is what makes a rate rather than an anecdote.

The two share their rules deliberately, by calling the same functions rather
than by keeping two copies in step: a session is judged on its edit records, a
run of ``graphctl`` inside its window counts for it however it was launched,
and sizes are counted from those same records. Where the hooks stay silent,
this reports what it saw and how much it trusts it, because a report a person
reads can carry doubt that a one-line warning cannot.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

from . import codex_sessions, journal, session_hooks, worktree

SCHEMA_VERSION = 1

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
        if not isinstance(session, str) or event not in {
            "session_open",
            "turn_end",
            "session_close",
            "edit",
        }:
            continue
        state = sessions.setdefault(
            session,
            {
                "session_id": session,
                "client": entry.get("client"),
                "repo": repo,
                "opened": None,
                "ended": None,
                "started_at": None,
                "ended_at": None,
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
                state["started_at"] = position
        else:
            state["ended"] = entry
            state["ended_at"] = position
            state["closed"] = state["closed"] or event == "session_close"
    for session, state in sessions.items():
        started = state["started_at"]
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
            and state["started_at"] is not None
            and state["ended_at"] is not None
            and state["started_at"] < position < state["ended_at"]
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
    verdict["outside_edits"] = len(
        session_hooks.edits(state["records"], include_outside=True)
    ) - len(session_hooks.edits(state["records"]))
    verdict["confidence"] = HIGH if state["closed"] else MEDIUM
    # The tree is reported as context and decides nothing. Both counts below
    # come from the snapshots, and no branch here reads them.
    before, after = (opened or {}).get("snapshot"), (ended or {}).get("snapshot")
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
    """Flag sessions whose repository had another session running with them.

    Concurrent sessions share one working tree, so neither one's change can be
    attributed to it alone. Saying so is more useful than quietly reporting a
    number that may belong to the other.
    """

    by_repo: dict[str, list[dict[str, Any]]] = {}
    for verdict in verdicts:
        verdict["contested"] = False
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

    Both ends of both sessions must be recorded in the same form. The Codex
    store keeps whole seconds while the hooks keep an ISO timestamp, and
    comparing one against the other would invent an overlap.
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
            "verdict": "bypass_suspected",
            "note": (
                "Codex hooks do not run on the measured clients, so this "
                "session's working tree was never observed."
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
