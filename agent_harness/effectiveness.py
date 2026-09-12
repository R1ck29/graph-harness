"""Report what the harness itself has been worth.

The graph records what happened to every node — how many attempts it took,
which reviewer failed it, on which criteria, against what evidence — and until
now nothing read any of it back. The cost of the protocol is a review round per
node and is obvious. Its benefit was anecdotal.

The number this exists for is `failed_despite_test_evidence`: reviews that
failed a node whose executor had already submitted test evidence. Those are the
defects an independent reviewer with explicit criteria found that a green suite
did not. Every other figure here is context for reading that one.

This reads and writes nothing. A report on whether the protocol was followed
that changed the record it reports on would be worth nothing.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Iterable

from . import conformance
from .errors import HarnessError
from .paths import (
    REFUSAL_NOT_A_REGULAR_FILE,
    REFUSAL_SYMLINK,
    REFUSAL_TOO_LARGE,
    REFUSAL_UNREADABLE,
    FileRefusal,
    is_link_like,
    open_bounded_regular_file,
    workspace_path,
)
from .storage import MAX_GRAPH_BYTES

# 2 because `unreadable` changed shape: it was a list of path strings and is
# now a list of {path, reason} objects, so a reader holding both documents
# needs to branch on this number to parse that field at all. An earlier
# attempt raised it to 2 with three reasons that were all false — a renamed
# key and two added ones, none of which had ever reached a commit — and it was
# reverted to 1 for that. This bump is the opposite case: the shape moved
# first and the number follows it. A version that moves without the shape
# moving invites a compatibility shim whose every branch is dead; a shape that
# moves without the version moving hands the reader no way to tell which
# parser to use.
SCHEMA_VERSION = 2

# Why one path was not read. A bare path told a reader that something was
# wrong and nothing about what to do: an archive over the size bound, one
# reached through a symlink, a FIFO and a file with a stray comma all looked
# alike, and each needs a different fix. Each name below is one refusal
# `_load` can make, so the report says which one happened.
#
# No count is written here on purpose. It has been wrong three times: the
# comment said four above five constants, the correction added a constant and
# said five above six, and each time the number was the part that drifted
# while the list stayed right. A reader can count the lines; a stale number
# only misleads. `REFUSALS` below is the same list as a value, so a test can
# hold the documentation to it instead of a person re-counting.
#
# `symlink` is separate from `outside_workspace` on purpose. Confinement
# refuses a link-like path whatever it points at, so a symlink whose target
# sits *inside* the workspace was reported as outside it — telling the reader
# to point somewhere else when the fix is to replace the link. A reason that
# misdirects is worse than the bare path it replaced.
# Four of these are the primitive's own, imported rather than restated so a
# report cannot name a refusal the reader cannot make. The other two are this
# function's: confinement is a rule about where a path may point, and
# malformed is about the bytes, neither of which the opener judges.
REFUSAL_OUTSIDE_WORKSPACE = "outside_workspace"
REFUSAL_MALFORMED = "malformed"

# Every reason `_load` can return, for anything that needs the whole set:
# the reference documents are checked against this, not against a copy.
REFUSALS = (
    REFUSAL_OUTSIDE_WORKSPACE,
    REFUSAL_SYMLINK,
    REFUSAL_NOT_A_REGULAR_FILE,
    REFUSAL_TOO_LARGE,
    REFUSAL_UNREADABLE,
    REFUSAL_MALFORMED,
)

# The same bound the graph store enforces. A report that read a file the store
# would refuse would be the one place the limit does not hold, and this one
# reads several files at once.
MAX_REPORT_GRAPH_BYTES = MAX_GRAPH_BYTES

# Which count each rate is a fraction of, so no rate is ever shown without the
# base it was taken over. With three objectives on record these are anecdotes,
# and a reader has to be able to see that.
DENOMINATORS = {
    "first_pass_rate": "verified",
    "share_of_failures_with_test_evidence": "failed",
    "conformance_rate": "judged_sessions",
}

# Which statuses mean a node is finished with. A record that reached one of
# these is preferred over one that did not, however many attempts the other
# carries: a graph abandoned mid-round leaves nodes frozen at `running` with a
# higher count than the successor where they were verified.
TERMINAL = ("verified", "superseded")


def _reviews(node: dict[str, Any]) -> list[tuple[dict[str, Any], list[Any]]]:
    """Return every recorded review of one node, with the evidence it saw.

    A node that failed and then passed keeps the failure in its attempt
    history. Reading only the current verdict would count the harness as
    having caught nothing in exactly the cases where it caught something.
    """

    found: list[tuple[dict[str, Any], list[Any]]] = []
    for archived in node.get("attempt_history") or []:
        if isinstance(archived, dict) and isinstance(
            archived.get("verification"), dict
        ):
            evidence = archived.get("evidence")
            found.append(
                (
                    archived["verification"],
                    evidence if isinstance(evidence, list) else [],
                )
            )
    current = node.get("verification")
    if isinstance(current, dict):
        evidence = node.get("evidence")
        found.append((current, evidence if isinstance(evidence, list) else []))
    return found


def _has_test_evidence(items: Iterable[Any]) -> bool:
    return any(isinstance(item, dict) and item.get("kind") == "test" for item in items)


def _submitted(verification: dict[str, Any], fallback: list[Any]) -> list[Any]:
    recorded = verification.get("submitted_evidence")
    return recorded if isinstance(recorded, list) else fallback


def report_from_documents(documents: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Fold graph documents into counts, each node counted once."""

    by_node: dict[tuple[str, str], dict[str, Any]] = {}
    reviews: dict[tuple[str, str], dict[str, tuple[dict[str, Any], list[Any]]]] = {}
    objectives: set[str] = set()
    for document in documents:
        objective = str(document.get("objective", ""))
        objectives.add(objective)
        for node in document.get("nodes") or []:
            if not isinstance(node, dict) or not isinstance(node.get("id"), str):
                continue
            key = (objective, node["id"])
            # One node appearing in an archive and in its successor is one
            # node, but every copy's reviews are kept: a rebuild that
            # re-created a node without its history would otherwise drop a
            # recorded failure from the count, which is the same laundering
            # by rebuild the protocol says the escape hatches exist to
            # prevent.
            for verification, evidence in _reviews(node):
                reviews.setdefault(key, {})[_review_key(verification, evidence)] = (
                    verification,
                    evidence,
                )
            existing = by_node.get(key)
            if existing is None or _prefer(node, existing):
                by_node[key] = node

    nodes = list(by_node.values())
    verified = [node for node in nodes if node.get("status") == "verified"]
    first_pass = [node for node in verified if _count(node) == 1]
    histogram: dict[int, int] = {}
    for node in nodes:
        attempts = _count(node)
        histogram[attempts] = histogram.get(attempts, 0) + 1

    passed = failed = caught = 0
    caught_nodes: set[tuple[str, str]] = set()
    failed_criteria: dict[str, int] = {}
    for key in by_node:
        for verification, evidence in reviews.get(key, {}).values():
            result = str(verification.get("result", "")).lower()
            if result == "pass":
                passed += 1
            elif result == "fail":
                failed += 1
                if _has_test_evidence(_submitted(verification, evidence)):
                    caught += 1
                    caught_nodes.add(key)
                for criterion in verification.get("failed_criteria") or []:
                    if isinstance(criterion, str):
                        failed_criteria[criterion] = (
                            failed_criteria.get(criterion, 0) + 1
                        )

    return {
        "schema_version": SCHEMA_VERSION,
        "objectives": len(objectives),
        "nodes": {
            "total": len(nodes),
            "verified": len(verified),
            "verified_first_attempt": len(first_pass),
            "first_pass_rate": _rate(len(first_pass), len(verified)),
            "attempt_histogram": dict(sorted(histogram.items())),
        },
        "reviews": {
            "passed": passed,
            "failed": failed,
            # The one that answers whether any of this was worth its cost.
            "failed_despite_test_evidence": caught,
            "nodes_with_such_a_failure": len(caught_nodes),
            # Named for what it measures. The denominator is review failures,
            # not defects: a defect review also missed is recorded nowhere, so
            # this can never mean review catches everything, however close to
            # one it sits.
            "share_of_failures_with_test_evidence": _rate(caught, failed),
            "most_failed_criteria": dict(
                sorted(failed_criteria.items(), key=lambda item: -item[1])[:5]
            ),
        },
        "escalations": {
            "budget_exhausted": sum(
                1
                for node in nodes
                if node.get("status") in {"failed", "invalidated"}
                and _count(node) >= _limit(node)
            ),
            "granted_attempts": sum(
                len(node.get("granted_attempts") or []) for node in nodes
            ),
            "superseded": sum(
                1 for node in nodes if node.get("status") == "superseded"
            ),
        },
    }


def report(
    graphs: Iterable[str | os.PathLike[str]],
    home: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Read each graph file, fold it, and attach the conformance counts."""

    documents: list[dict[str, Any]] = []
    unreadable: list[dict[str, str]] = []
    for path in graphs:
        loaded, refusal = _load(path)
        if loaded is None:
            unreadable.append({"path": str(path), "reason": refusal})
            continue
        documents.append(loaded)
    folded = report_from_documents(documents)
    folded["graphs_read"] = len(documents)
    folded["unreadable"] = unreadable
    sessions = conformance.report(home)
    folded["conformance"] = {
        "counts": sessions["counts"],
        "judged_sessions": sessions["judged_sessions"],
        "conformance_rate": sessions["conformance_rate"],
    }
    return folded


def _review_key(verification: dict[str, Any], evidence: list[Any]) -> str:
    """Identify one review, so the same one seen in two archives counts once."""

    return json.dumps(
        [
            verification.get("result"),
            verification.get("reviewer_id"),
            verification.get("checked_criteria"),
            verification.get("failed_criteria"),
            [
                item.get("summary") if isinstance(item, dict) else item
                for item in evidence
            ],
        ],
        sort_keys=True,
        default=str,
    )


def _prefer(candidate: dict[str, Any], existing: dict[str, Any]) -> bool:
    """Report whether *candidate* is the better record of one node.

    A finished status wins over an unfinished one first. Comparing attempts
    first picked a node frozen at `running` in an abandoned round over the
    successor where it was verified, and made the totals depend on the
    alphabetical order of archive filenames.
    """

    candidate_done = candidate.get("status") in TERMINAL
    existing_done = existing.get("status") in TERMINAL
    if candidate_done != existing_done:
        return candidate_done
    return _count(candidate) > _count(existing)


def _load(path: str | os.PathLike[str]) -> tuple[dict[str, Any] | None, str]:
    """Read one graph document, or return the reason it was refused.

    Each refusal matches a rule the rest of the harness already keeps, and
    `REFUSALS` names them all. The path must not be a symlink, which
    confinement refuses whatever it points at. It must lie inside the
    workspace, because every other subcommand confines its paths and a report
    that read anywhere on disk would be the exception. The file must be a
    regular file. It must be no larger than the graph store allows, because
    this reads several at once and the store's own limit would otherwise not
    hold here. And it must parse to an object. The reason travels back with
    the result, because the caller cannot recover it and a report that only
    says "not read" leaves the user with nothing to act on.
    """

    try:
        if is_link_like(Path(path)):
            return None, REFUSAL_SYMLINK
    except (OSError, ValueError):
        # ValueError, not only OSError: a path carrying an embedded NUL
        # raises `embedded null character in path` out of `lstat`, which
        # `is_link_like` does not answer. Unreachable through argv, which
        # cannot carry one, but this function is callable directly and its
        # contract is that it never raises.
        return None, REFUSAL_UNREADABLE
    try:
        confined = workspace_path(str(path))
    except HarnessError:
        return None, REFUSAL_OUTSIDE_WORKSPACE
    except OSError:
        # Reached when the failure is in resolving, not in the path given.
        # `workspace_path` starts by resolving the working directory, and
        # `Path.cwd()` raises FileNotFoundError — an OSError — once that
        # directory has been removed out from under the process. The
        # symlink check above does not catch it first, because
        # `is_link_like` answers False for a missing path rather than
        # raising.
        #
        # I previously called this arm unreachable, on the grounds that the
        # symlink check now takes ENAMETOOLONG first. That much is true and
        # it is why the long-name probe pins the earlier arm, but it is not
        # the only way in, and a reviewer falsified the claim by deleting
        # the working directory. Without this arm `_load` raises instead of
        # refusing, which breaks the one contract it has.
        return None, REFUSAL_UNREADABLE
    try:
        descriptor = open_bounded_regular_file(confined, MAX_REPORT_GRAPH_BYTES)
    except FileRefusal as exc:
        return None, exc.reason
    except OSError:
        return None, REFUSAL_UNREADABLE
    try:
        with os.fdopen(descriptor, "rb") as handle:
            text = handle.read().decode("utf-8")
    except OSError:
        return None, REFUSAL_UNREADABLE
    except UnicodeDecodeError:
        # A UnicodeDecodeError is a ValueError, not an OSError. Splitting the
        # single `except (OSError, ValueError)` this function used to have
        # into a read step and a parse step dropped it on the floor, and a
        # binary file named like a graph then raised out of the report — the
        # one thing this loader exists to prevent. The file is present and
        # readable; its bytes are wrong, so it is malformed.
        return None, REFUSAL_MALFORMED
    try:
        loaded = json.loads(text)
    except ValueError:
        return None, REFUSAL_MALFORMED
    if not isinstance(loaded, dict):
        return None, REFUSAL_MALFORMED
    return loaded, ""


def _whole_number(value: Any) -> int:
    """Return a recorded integer, treating anything else as absent.

    Every field read here comes out of a file a person can edit, so a string,
    a float or a boolean where a count belongs must not reach arithmetic. The
    bool arm is not redundant: `True` is an `int` in Python and would count as
    one attempt.
    """

    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _count(node: dict[str, Any]) -> int:
    return _whole_number(node.get("attempts"))


def _limit(node: dict[str, Any]) -> int:
    return _whole_number(node.get("max_attempts"))


def _rate(part: int, whole: int) -> float | None:
    """Return a rate, or None when there is nothing to take it over.

    Zero over zero is not zero. Reporting it as zero would read as a harness
    that never caught anything rather than one that has not been used yet.
    """

    return round(part / whole, 3) if whole else None
