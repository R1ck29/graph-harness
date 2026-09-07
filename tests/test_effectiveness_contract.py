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
import subprocess
import sys
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
        self.home = Path(self._directory.name).resolve()
        self._environment = mock.patch.dict(
            os.environ, {"GRAPH_HARNESS_HOME": str(self.home)}
        )
        self._environment.start()
        self.addCleanup(self._environment.stop)
        # The report confines graph paths to the workspace, so the workspace
        # is this temporary home. Making one inside the repository instead
        # would leave a directory behind on every run.
        previous = os.getcwd()
        os.chdir(self.home)
        self.addCleanup(os.chdir, previous)

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
        # reader can tell a thin report from a complete one, and each says
        # which of the two it was — one is fixed by editing the file and the
        # other by pointing somewhere else.
        self.assertEqual(
            [
                {"path": str(broken), "reason": "malformed"},
                {"path": str(self.home / "missing.json"), "reason": "unreadable"},
            ],
            found["unreadable"],
        )

    def test_this_repository_records_a_defect_review_caught(self) -> None:
        # The claim, measured against the only history there is.
        graphs = sorted(REPOSITORY.glob("task-graph*.json"))
        if not graphs:
            # The archives are gitignored working history, not repository
            # content, so a fresh clone, a CI checkout and a detached
            # worktree all have none. Asserting they exist failed the suite
            # for everyone but this machine, which is a defect in the test
            # rather than a finding about the harness.
            self.skipTest("no graph archives in this checkout")
        # These graphs live in the repository, so the repository is the
        # workspace for this reading of them.
        previous = os.getcwd()
        os.chdir(REPOSITORY)
        self.addCleanup(os.chdir, previous)

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


class ReportIntegrityTests(unittest.TestCase):
    """What the reports refuse, and what they say about themselves."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.home = Path(self._directory.name).resolve()
        self._environment = mock.patch.dict(
            os.environ, {"GRAPH_HARNESS_HOME": str(self.home)}
        )
        self._environment.start()
        self.addCleanup(self._environment.stop)
        self.workspace = self.home / "work"
        self.workspace.mkdir()
        previous = os.getcwd()
        os.chdir(self.workspace)
        self.addCleanup(os.chdir, previous)

    def _graph(self, name: str, document: dict[str, Any]) -> Path:
        path = self.workspace / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_a_graph_above_the_storage_bound_is_refused_not_read(self) -> None:
        # The graph store already refuses a file this size; a report that
        # read one anyway would be the one place the bound does not hold.
        from agent_harness import storage

        oversized = self.workspace / "task-graph.huge.json"
        padding = "x" * (storage.MAX_GRAPH_BYTES + 1024)
        oversized.write_text(
            json.dumps({"objective": padding, "nodes": []}), encoding="utf-8"
        )
        good = self._graph("task-graph.json", graph(node("a")))

        found = effectiveness.report([oversized, good], home=self.home)

        self.assertEqual(1, found["nodes"]["total"])
        self.assertEqual(1, found["graphs_read"])
        self.assertEqual(
            [{"path": str(oversized), "reason": "too_large"}], found["unreadable"]
        )

    def test_a_graph_outside_the_workspace_is_refused(self) -> None:
        # Every other subcommand confines its paths to the workspace; a
        # report that read anywhere on disk would be the exception.
        outside = self.home / "elsewhere.json"  # a sibling of the workspace
        outside.write_text(json.dumps(graph(node("a"))), encoding="utf-8")
        good = self._graph("task-graph.json", graph(node("b")))

        found = effectiveness.report([outside, good], home=self.home)

        self.assertEqual(1, found["nodes"]["total"])
        self.assertEqual(
            [{"path": str(outside), "reason": "outside_workspace"}],
            found["unreadable"],
        )

    # os.mkfifo does not exist on Windows, and the CI matrix runs there.
    # The guard the test drives is the same on both, but only POSIX can build
    # the file that proves it.
    @unittest.skipIf(os.name == "nt", "POSIX named pipe")
    def test_a_graph_that_is_not_a_regular_file_cannot_hang_the_report(self) -> None:
        # A size bound is not a time bound: a FIFO has a size of zero, passes
        # any byte limit, and then blocks the read for ever. Driven in a
        # subprocess under a timeout, because this defect hangs and a test
        # that hangs is not a test.
        fifo = self.workspace / "task-graph.fifo.json"
        os.mkfifo(fifo)
        self._graph("task-graph.json", graph(node("a")))

        result = subprocess.run(
            [
                sys.executable,
                str(REPOSITORY / "scripts/graphctl.py"),
                "effectiveness",
            ],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "GRAPH_HARNESS_HOME": str(self.home)},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        found = json.loads(result.stdout)
        self.assertEqual(1, found["graphs_read"])
        self.assertEqual(
            [{"path": str(fifo), "reason": "not_a_regular_file"}],
            found["unreadable"],
        )

    def test_a_graph_of_undecodable_bytes_is_refused_not_raised(self) -> None:
        # A UnicodeDecodeError is a ValueError, not an OSError. Naming the
        # refusals split one `except (OSError, ValueError)` into a read step
        # and a parse step, and this case fell through the gap: a binary file
        # named like a graph raised out of the report, which is the one thing
        # this loader exists to prevent.
        binary = self.workspace / "task-graph.bin.json"
        binary.write_bytes(bytes([0xFF, 0xFE, 0x00, 0x80]) * 50)
        good = self._graph("task-graph.json", graph(node("a")))

        found = effectiveness.report([binary, good], home=self.home)

        self.assertEqual(1, found["graphs_read"])
        self.assertEqual(
            [{"path": str(binary), "reason": "malformed"}], found["unreadable"]
        )

    def test_a_name_too_long_for_the_filesystem_is_refused_not_raised(self) -> None:
        # Confining a path resolves it, and resolving can fail on the path
        # itself: ENAMETOOLONG is an OSError, not the HarnessError the
        # confinement raises, so it escaped the report and this command
        # raised instead of naming the file.
        too_long = self.workspace / ("task-graph." + "x" * 300 + ".json")
        good = self._graph("task-graph.json", graph(node("a")))

        found = effectiveness.report([too_long, good], home=self.home)

        self.assertEqual(1, found["graphs_read"])
        self.assertEqual(
            [{"path": str(too_long), "reason": "unreadable"}], found["unreadable"]
        )

    def test_a_symlinked_graph_says_symlink_rather_than_outside(self) -> None:
        # Confinement refuses a link-like path whatever it points at, so a
        # symlink whose target sits inside the workspace came back as
        # `outside_workspace` — telling the reader to point somewhere else
        # when the fix is to replace the link. A reason that misdirects is
        # worse than the bare path it replaced.
        target = self.workspace / "real.json"
        target.write_text(json.dumps(graph(node("a"))), encoding="utf-8")
        link = self.workspace / "task-graph.link.json"
        try:
            link.symlink_to(target)
        except OSError as exc:  # pragma: no cover - platform dependent
            self.skipTest(f"symlinks unavailable: {exc}")
        good = self._graph("task-graph.json", graph(node("b")))

        found = effectiveness.report([link, good], home=self.home)

        self.assertEqual(1, found["graphs_read"])
        self.assertEqual(
            [{"path": str(link), "reason": "symlink"}], found["unreadable"]
        )

    def test_a_path_genuinely_outside_still_says_so(self) -> None:
        # The new reason must not swallow the old one.
        outside = self.home / "elsewhere.json"
        outside.write_text(json.dumps(graph(node("a"))), encoding="utf-8")

        found = effectiveness.report([outside], home=self.home)

        self.assertEqual(
            [{"path": str(outside), "reason": "outside_workspace"}],
            found["unreadable"],
        )

    def test_a_path_with_an_embedded_nul_is_refused_not_raised(self) -> None:
        # A NUL in a path raises ValueError out of `lstat`, not OSError, so
        # the guard that catches it is a `ValueError` arm. A mutation audit
        # found the arm unpinned: removing it left the whole suite green,
        # and the fix could have been reverted without anything noticing.
        # Unreachable through argv, which cannot carry a NUL, but this
        # function is callable directly and its contract is never to raise.
        found = effectiveness.report(["task-graph.\x00.json"], home=self.home)

        self.assertEqual(0, found["graphs_read"])
        self.assertEqual(
            [{"path": "task-graph.\x00.json", "reason": "unreadable"}],
            found["unreadable"],
        )

    def test_a_graph_that_is_valid_json_but_not_an_object_is_refused(self) -> None:
        # A top-level array parses cleanly, so it passes every guard above
        # and is caught only by the type check. The same audit found that
        # check unpinned: the malformed-JSON tests all use text that fails
        # to parse, which exercises a different arm entirely.
        listed = self.workspace / "task-graph.list.json"
        listed.write_text("[1, 2, 3]", encoding="utf-8")
        scalar = self.workspace / "task-graph.scalar.json"
        scalar.write_text("42", encoding="utf-8")
        good = self._graph("task-graph.json", graph(node("a")))

        found = effectiveness.report([listed, scalar, good], home=self.home)

        self.assertEqual(1, found["graphs_read"])
        self.assertEqual(
            [
                {"path": str(listed), "reason": "malformed"},
                {"path": str(scalar), "reason": "malformed"},
            ],
            found["unreadable"],
        )

    # Windows locks a process's working directory, so `os.rmdir` on it
    # raises and the child would exit non-zero — the test would fail on
    # the CI matrix's windows legs unconditionally, not only under
    # mutation. The guard is unreachable there for the same reason: the
    # operating system forbids the precondition.
    @unittest.skipIf(os.name == "nt", "POSIX: the working directory can be removed")
    def test_a_deleted_working_directory_is_refused_not_raised(self) -> None:
        # The last unpinned guard, and the one I wrongly called unreachable.
        # Confinement resolves the working directory first, and `Path.cwd()`
        # raises FileNotFoundError once that directory is gone; the symlink
        # check does not take it first, because `is_link_like` answers False
        # for a missing path rather than raising. Without the OSError arm
        # around `workspace_path`, `_load` raises instead of refusing.
        #
        # Driven in a subprocess: a process whose working directory has been
        # deleted is a hostile state to leave lying around for sibling tests.
        program = (
            "import os, sys, tempfile;"
            f"sys.path.insert(0, {str(REPOSITORY)!r});"
            "from agent_harness import effectiveness;"
            "d = tempfile.mkdtemp();"
            "os.chdir(d);"
            "os.rmdir(d);"
            "print(effectiveness._load('task-graph.json'))"
        )

        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "GRAPH_HARNESS_HOME": str(self.home)},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("(None, 'unreadable')", result.stdout.strip())

    def test_every_path_passed_is_accounted_for(self) -> None:
        # graphs_read plus unreadable must equal what was handed in, so a
        # thin report cannot be mistaken for a complete one.
        good = self._graph("task-graph.json", graph(node("a")))
        broken = self.workspace / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        missing = self.workspace / "absent.json"

        found = effectiveness.report([good, broken, missing], home=self.home)

        self.assertEqual(3, found["graphs_read"] + len(found["unreadable"]))
        self.assertEqual(1, found["graphs_read"])

    def test_each_report_names_the_version_of_itself(self) -> None:
        from agent_harness import conformance

        found = effectiveness.report(
            [self._graph("task-graph.json", graph())], home=self.home
        )

        self.assertEqual(effectiveness.SCHEMA_VERSION, found["schema_version"])
        self.assertEqual(
            conformance.SCHEMA_VERSION, conformance.report(self.home)["schema_version"]
        )
        # Both numbers moved because both shapes moved. Conformance:
        # read_only became unattributed, edits replaced changed_lines, and
        # outside_edits and the tree context were added after its version 1
        # was committed. Effectiveness: unreadable was a list of path strings
        # and is now a list of {path, reason} objects, which a reader cannot
        # parse without knowing which one they hold. An earlier attempt
        # raised this number with no shape change behind it and was failed
        # for it, so the pairing below is the thing under test, not the
        # numbers: each version must be at least 2, and unreadable must
        # actually carry the newer shape.
        self.assertGreaterEqual(conformance.SCHEMA_VERSION, 2)
        self.assertGreaterEqual(effectiveness.SCHEMA_VERSION, 2)
        self.assertEqual([], found["unreadable"])
        refused = effectiveness.report([self.workspace / "gone.json"], home=self.home)
        self.assertEqual(
            [{"path": str(self.workspace / "gone.json"), "reason": "unreadable"}],
            refused["unreadable"],
        )
