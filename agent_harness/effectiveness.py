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
from pathlib import Path
from typing import Any, Iterable

from . import conformance

SCHEMA_VERSION = 1

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
        histogram[_count(node)] = histogram.get(_count(node), 0) + 1

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
    unreadable: list[str] = []
    for path in graphs:
        try:
            loaded = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unreadable.append(str(path))
            continue
        if isinstance(loaded, dict):
            documents.append(loaded)
        else:
            unreadable.append(str(path))
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


def _count(node: dict[str, Any]) -> int:
    value = node.get("attempts")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _limit(node: dict[str, Any]) -> int:
    value = node.get("max_attempts")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _rate(part: int, whole: int) -> float | None:
    """Return a rate, or None when there is nothing to take it over.

    Zero over zero is not zero. Reporting it as zero would read as a harness
    that never caught anything rather than one that has not been used yet.
    """

    return round(part / whole, 3) if whole else None
