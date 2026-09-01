"""Executable contract for the report on the harness's own worth.

The harness costs a review round per node. Its claim is that an independent
reviewer with explicit criteria catches things a green test suite does not.
That claim was recorded in every graph this repository has produced and read
back by nothing until now.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_harness import effectiveness, journal

REPOSITORY = Path(__file__).resolve().parents[1]


def review(result: str, *, evidence: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "result": result,
        "reviewer_id": "reviewer",
        "checked_criteria": ["c"],
        "evidence": [{"criterion": "c", "kind": "reproduction", "summary": "s"}],
        "submitted_evidence": evidence,
    }


def node(
    node_id: str,
    *,
    status: str = "verified",
    attempts: int = 1,
    max_attempts: int = 2,
    evidence: list[dict[str, str]] | None = None,
    verification: dict[str, Any] | None = None,
    attempt_history: list[dict[str, Any]] | None = None,
    granted: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": node_id,
        "status": status,
        "attempts": attempts,
        "max_attempts": max_attempts,
        "evidence": evidence if evidence is not None else [],
        "verification": verification,
    }
    if attempt_history is not None:
        record["attempt_history"] = attempt_history
    if granted is not None:
        record["granted_attempts"] = granted
    return record


def graph(*nodes: dict[str, Any], objective: str = "an objective") -> dict[str, Any]:
    return {"version": 1, "objective": objective, "nodes": list(nodes)}


class EffectivenessContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.home = Path(self._directory.name)
        self._environment = mock.patch.dict(
            os.environ, {"GRAPH_HARNESS_HOME": str(self.home)}
        )
        self._environment.start()
        self.addCleanup(self._environment.stop)

    def _write(self, name: str, document: dict[str, Any]) -> Path:
        path = self.home / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_a_failure_after_test_evidence_is_counted_separately(self) -> None:
        # The point of the whole report. A reviewer that fails a node whose
        # executor had test coverage caught something the suite did not.
        document = graph(
            node(
                "caught",
                status="failed",
                evidence=[{"kind": "test", "summary": "247 pass"}],
                verification=review(
                    "fail", evidence=[{"kind": "test", "summary": "247 pass"}]
                ),
            ),
            node(
                "plain",
                status="failed",
                evidence=[{"kind": "observation", "summary": "looked at it"}],
                verification=review(
                    "fail", evidence=[{"kind": "observation", "summary": "looked"}]
                ),
            ),
        )

        found = effectiveness.report_from_documents([document])

        self.assertEqual(2, found["reviews"]["failed"])
        self.assertEqual(1, found["reviews"]["failed_despite_test_evidence"])

    def test_an_archived_attempt_carries_its_own_review(self) -> None:
        # A node that failed and then passed holds the failure in its
        # attempt history. Reading only the current verdict would count the
        # harness as having caught nothing.
        document = graph(
            node(
                "retried",
                status="verified",
                attempts=2,
                verification=review("pass", evidence=[]),
                attempt_history=[
                    {
                        "attempt": 1,
                        "evidence": [{"kind": "test", "summary": "green"}],
                        "verification": review(
                            "fail", evidence=[{"kind": "test", "summary": "green"}]
                        ),
                    }
                ],
            )
        )

        found = effectiveness.report_from_documents([document])

        self.assertEqual(1, found["reviews"]["failed"])
        self.assertEqual(1, found["reviews"]["passed"])
        self.assertEqual(1, found["reviews"]["failed_despite_test_evidence"])

    def test_each_outcome_metric_is_produced_from_a_constructed_graph(self) -> None:
        document = graph(
            node("first-pass"),
            node("reworked", attempts=3),
            node("stuck", status="failed", attempts=2, max_attempts=2),
            node(
                "granted",
                status="failed",
                attempts=2,
                max_attempts=3,
                granted=[{"granted_by": "rick", "reason": "r"}],
            ),
            node("retired", status="superseded", attempts=1),
        )

        found = effectiveness.report_from_documents([document])

        self.assertEqual(5, found["nodes"]["total"])
        self.assertEqual(2, found["nodes"]["verified"])
        self.assertEqual(1, found["nodes"]["verified_first_attempt"])
        self.assertEqual({1: 2, 2: 2, 3: 1}, found["nodes"]["attempt_histogram"])
        self.assertEqual(1, found["escalations"]["budget_exhausted"])
        self.assertEqual(1, found["escalations"]["granted_attempts"])
        self.assertEqual(1, found["escalations"]["superseded"])

    def test_one_node_in_an_archive_and_its_successor_is_counted_once(self) -> None:
        early = graph(node("a", status="failed", attempts=1))
        late = graph(node("a", status="verified", attempts=2))

        found = effectiveness.report_from_documents([early, late])

        self.assertEqual(1, found["nodes"]["total"])
        self.assertEqual(1, found["nodes"]["verified"])

    def test_a_finished_record_wins_over_a_stale_one_with_more_attempts(self) -> None:
        # A graph abandoned mid-round leaves nodes frozen at running with a
        # higher attempt count than the successor that verified them.
        # Comparing attempts first reported the stale record, and made the
        # totals depend on the alphabetical order of archive filenames.
        stale = graph(node("a", status="running", attempts=3))
        finished = graph(node("a", status="verified", attempts=1))

        for order in ([stale, finished], [finished, stale]):
            found = effectiveness.report_from_documents(order)

            self.assertEqual(1, found["nodes"]["verified"])
            self.assertEqual({1: 1}, found["nodes"]["attempt_histogram"])

    def test_a_failure_dropped_by_a_rebuild_is_still_counted(self) -> None:
        # A rebuild that re-created a node without its history silently
        # replaced the failed record, so a recorded review failure vanished
        # from the count. That is the same laundering by rebuild the protocol
        # says the escape hatches exist to prevent.
        abandoned = graph(
            node(
                "a",
                status="failed",
                attempts=1,
                evidence=[{"kind": "test", "summary": "green"}],
                verification=review(
                    "fail", evidence=[{"kind": "test", "summary": "green"}]
                ),
            )
        )
        rebuilt = graph(node("a", status="verified", attempts=1))

        found = effectiveness.report_from_documents([abandoned, rebuilt])

        self.assertEqual(1, found["nodes"]["total"])
        self.assertEqual(1, found["reviews"]["failed"])
        self.assertEqual(1, found["reviews"]["failed_despite_test_evidence"])
        self.assertEqual(1, found["reviews"]["nodes_with_such_a_failure"])

    def test_the_same_review_seen_in_two_archives_is_counted_once(self) -> None:
        one = graph(
            node(
                "a",
                status="failed",
                attempts=1,
                evidence=[{"kind": "test", "summary": "green"}],
                verification=review(
                    "fail", evidence=[{"kind": "test", "summary": "green"}]
                ),
            )
        )

        found = effectiveness.report_from_documents([one, json.loads(json.dumps(one))])

        self.assertEqual(1, found["reviews"]["failed"])

    def test_two_objectives_keep_their_nodes_apart(self) -> None:
        first = graph(node("gate"), objective="one")
        second = graph(node("gate"), objective="two")

        found = effectiveness.report_from_documents([first, second])

        self.assertEqual(2, found["nodes"]["total"])
        self.assertEqual(2, found["objectives"])

    def test_every_rate_is_reported_beside_its_denominator(self) -> None:
        document = graph(node("a"), node("b", attempts=2))

        path = self._write("task-graph.json", document)

        found = effectiveness.report([path], home=self.home)

        self.assertEqual(0.5, found["nodes"]["first_pass_rate"])
        self.assertEqual(2, found["nodes"]["verified"])
        self.assertIn("conformance", found)
        # No rate anywhere without the count it was taken over, so nobody can
        # quote a percentage from three objectives as though it were a
        # statistic.
        rates = [
            (section, key)
            for section in ("nodes", "reviews")
            for key in found[section]
            if key.endswith("_rate")
        ]
        self.assertTrue(rates)
        for section, key in rates:
            self.assertIn(effectiveness.DENOMINATORS[key], found[section])

    def test_a_rate_with_no_denominator_is_none_rather_than_zero(self) -> None:
        found = effectiveness.report_from_documents([graph()])

        self.assertIsNone(found["nodes"]["first_pass_rate"])
        self.assertEqual(0, found["nodes"]["verified"])

    def test_the_report_writes_nothing(self) -> None:
        journal.append(journal.record("session_open", client="claude"), self.home)
        path = self._write("task-graph.json", graph(node("a")))
        before = self._state()

        effectiveness.report([path], home=self.home)

        self.assertEqual(before, self._state())

    def test_an_unreadable_graph_is_skipped_rather_than_fatal(self) -> None:
        broken = self.home / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        good = self._write("good.json", graph(node("a")))

        found = effectiveness.report(
            [broken, good, self.home / "missing.json"], home=self.home
        )

        self.assertEqual(1, found["nodes"]["total"])
        # Both the malformed file and the missing one are named, so a
        # reader can tell a thin report from a complete one.
        self.assertEqual(2, len(found["unreadable"]))

    def test_this_repository_records_a_defect_review_caught(self) -> None:
        # The claim, measured against the only history there is.
        graphs = sorted(REPOSITORY.glob("task-graph*.json"))
        self.assertTrue(graphs)

        found = effectiveness.report(graphs, home=self.home)

        self.assertGreaterEqual(found["reviews"]["failed_despite_test_evidence"], 1)

    def _state(self) -> dict[str, str]:
        state: dict[str, str] = {}
        for path in sorted(self.home.rglob("*")):
            if path.is_file():
                state[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return state


if __name__ == "__main__":
    unittest.main()
