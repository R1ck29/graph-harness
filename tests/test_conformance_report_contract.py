from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_harness import conformance, journal

REPOSITORY = Path(__file__).resolve().parents[1]
GRAPHCTL = REPOSITORY / "scripts" / "graphctl.py"

SOUND = {"git": True, "head": "a" * 40, "degraded": False}


class ConformanceReportTests(unittest.TestCase):
    """A rate is only worth reading if each verdict behind it is defensible."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.home = self._directory.name

    def _append(self, event: str, **fields: Any) -> None:
        self.assertTrue(journal.append(journal.record(event, **fields), self.home))

    def _snapshot(self, digest: str, files: int = 0, lines: int = 0) -> dict[str, Any]:
        return {**SOUND, "digest": digest, "files": files, "lines": lines}

    def _session(
        self,
        session_id: str,
        *,
        repo: str = "/repo",
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        transitions: int = 0,
        edits: int = 0,
        close: bool = True,
        open_it: bool = True,
    ) -> None:
        if open_it:
            self._append(
                "session_open",
                client="claude",
                session_id=session_id,
                repo=repo,
                snapshot=before if before is not None else self._snapshot("same"),
            )
        for index in range(edits):
            self._append(
                "edit",
                client="claude",
                session_id=session_id,
                repo=repo,
                tool="Edit",
                path_id=f"{index:012x}",
            )
        for index in range(transitions):
            self._append(
                "graph_transition",
                client="claude",
                session_id=session_id,
                repo=repo,
                command=f"start-{index}",
            )
        if close:
            self._append(
                "session_close",
                client="claude",
                session_id=session_id,
                repo=repo,
                snapshot=after if after is not None else self._snapshot("same"),
            )

    def _report(self, **options: Any) -> dict[str, Any]:
        return conformance.report(self.home, **options)

    def _verdicts(self, report: dict[str, Any]) -> dict[str, str]:
        return {item["session_id"]: item["verdict"] for item in report["sessions"]}

    def test_every_verdict_is_produced_by_a_constructed_journal(self) -> None:
        self._session(
            "conformant",
            before=self._snapshot("before"),
            after=self._snapshot("after"),
            edits=3,
            transitions=1,
        )
        self._session(
            "bypass",
            before=self._snapshot("before"),
            after=self._snapshot("after"),
            edits=3,
        )
        self._session("unattributed")
        self._session("incomplete", close=False)
        self._append(
            "session_close",
            client="claude",
            session_id="unobserved",
            repo="/repo",
            snapshot=self._snapshot("after", files=3, lines=40),
        )

        report = self._report()

        self.assertEqual(
            {
                "conformant": "conformant",
                "bypass": "bypass",
                "unattributed": "unattributed",
                "incomplete": "incomplete",
                "unobserved": "unobserved",
            },
            self._verdicts(report),
        )
        self.assertEqual(2, report["judged_sessions"])
        self.assertEqual(0.5, report["conformance_rate"])

    def test_a_degraded_snapshot_no_longer_changes_any_verdict(self) -> None:
        # This asserted `unobserved` while the tree decided things: a snapshot
        # that failed could not be compared, so nothing could be concluded.
        # Nothing is concluded from a snapshot now, so a failed one is simply
        # not consulted and the edit records still answer the question. A
        # degraded observation used to be able to hide a real bypass.
        self._session(
            "degraded",
            before={**self._snapshot("before"), "degraded": True},
            after={**self._snapshot("after"), "degraded": True},
            edits=3,
        )

        self.assertEqual({"degraded": "bypass"}, self._verdicts(self._report()))

    def test_edits_outside_the_repository_are_reported_apart_from_its_own(
        self,
    ) -> None:
        self._append(
            "session_open",
            client="claude",
            session_id="mixed",
            repo="/repo",
            snapshot=self._snapshot("before"),
        )
        for index in range(2):
            self._append(
                "edit",
                client="claude",
                session_id="mixed",
                repo="/repo",
                tool="Edit",
                path_id=f"in{index:010x}",
            )
        for index in range(5):
            self._append(
                "edit",
                client="claude",
                session_id="mixed",
                repo="/repo",
                tool="Edit",
                path_id=f"out{index:09x}",
                outside=True,
            )
        self._append(
            "session_close",
            client="claude",
            session_id="mixed",
            repo="/repo",
            snapshot=self._snapshot("after"),
        )

        found = [
            item for item in self._report()["sessions"] if item["session_id"] == "mixed"
        ][0]

        self.assertEqual("bypass", found["verdict"])
        self.assertEqual(2, found["changed_files"])
        self.assertEqual(2, found["edits"])
        self.assertEqual(5, found["outside_edits"])

    def test_a_session_that_only_edited_outside_is_unattributed(self) -> None:
        self._session("scratchy", edits=0)
        for index in range(6):
            self._append(
                "edit",
                client="claude",
                session_id="scratchy",
                repo="/repo",
                tool="Edit",
                path_id=f"{index:012x}",
                outside=True,
            )

        found = [
            item
            for item in self._report()["sessions"]
            if item["session_id"] == "scratchy"
        ][0]

        self.assertEqual("unattributed", found["verdict"])
        self.assertEqual(0, found["edits"])
        self.assertEqual(6, found["outside_edits"])

    def test_a_rate_counts_only_sessions_that_could_be_judged(self) -> None:
        self._session("unattributed")
        self._session("incomplete", close=False)

        report = self._report()

        self.assertEqual(0, report["judged_sessions"])
        self.assertIsNone(report["conformance_rate"])

    def test_overlapping_sessions_in_one_repository_are_flagged(self) -> None:
        self._append(
            "session_open",
            client="claude",
            session_id="first",
            repo="/repo",
            snapshot=self._snapshot("before"),
        )
        self._session(
            "second",
            before=self._snapshot("before"),
            after=self._snapshot("after"),
            edits=3,
        )
        self._append(
            "session_close",
            client="claude",
            session_id="first",
            repo="/repo",
            snapshot=self._snapshot("after", files=3, lines=40),
        )

        contested = {
            item["session_id"]: item["contested"] for item in self._report()["sessions"]
        }

        self.assertTrue(contested["first"])
        self.assertTrue(contested["second"])

    def test_sessions_in_different_repositories_are_not_contested(self) -> None:
        self._session(
            "here",
            repo="/one",
            before=self._snapshot("before"),
            after=self._snapshot("after"),
            edits=3,
        )
        self._session(
            "there",
            repo="/two",
            before=self._snapshot("before"),
            after=self._snapshot("after"),
            edits=3,
        )

        for item in self._report()["sessions"]:
            self.assertFalse(item["contested"], item)

    def test_a_transition_outside_a_session_window_is_not_credited_to_it(self) -> None:
        self._append(
            "graph_transition",
            client="claude",
            session_id="someone-else",
            repo="/repo",
            command="start",
        )
        self._session(
            "bypass",
            before=self._snapshot("before"),
            after=self._snapshot("after"),
            edits=3,
        )

        self.assertEqual({"bypass": "bypass"}, self._verdicts(self._report()))

    def test_a_run_recorded_after_its_session_closed_still_counts_for_it(
        self,
    ) -> None:
        # Records from separate processes are not ordered by causality. Placing
        # a run by position alone reported the session that used the graph as
        # one that did not.
        self._session(
            "worker",
            before=self._snapshot("before"),
            after=self._snapshot("after"),
            edits=5,
        )
        self._append(
            "graph_transition",
            client="claude",
            session_id="worker",
            repo="/repo",
            command="start",
        )

        self.assertEqual({"worker": "conformant"}, self._verdicts(self._report()))

    def test_edit_counts_are_reported_and_min_files_filters_on_them(self) -> None:
        self._session(
            "small",
            before=self._snapshot("before"),
            after=self._snapshot("after"),
            edits=1,
        )
        self._session(
            "large",
            before=self._snapshot("before"),
            after=self._snapshot("after"),
            edits=6,
        )

        full = self._report()
        filtered = self._report(min_files=2)

        sizes = {item["session_id"]: item["changed_files"] for item in full["sessions"]}
        self.assertEqual({"small": 1, "large": 6}, sizes)
        self.assertEqual(
            ["large"], [item["session_id"] for item in filtered["sessions"]]
        )

    def test_one_repository_can_be_singled_out(self) -> None:
        self._session("here", repo="/one")
        self._session("there", repo="/two")

        report = self._report(repo="/two")

        self.assertEqual(["there"], [item["session_id"] for item in report["sessions"]])

    def test_a_codex_session_is_reported_as_suspected_and_never_judged(self) -> None:
        codex = Path(self.home) / ".codex"
        codex.mkdir(parents=True)
        connection = sqlite3.connect(codex / "state_1.sqlite")
        with connection:
            connection.execute(
                "CREATE TABLE threads (id TEXT, cwd TEXT, created_at INTEGER, "
                "updated_at INTEGER, source TEXT)"
            )
            connection.execute("INSERT INTO threads VALUES ('c1','/repo',1,2,'exec')")
            connection.execute(
                "INSERT INTO threads VALUES ('c2','/repo',3,4,'{\"subagent\":{}}')"
            )
        connection.close()

        report = self._report()

        self.assertEqual({"c1": "bypass_suspected"}, self._verdicts(report))
        self.assertEqual("low", report["sessions"][0]["confidence"])
        self.assertEqual(0, report["judged_sessions"])
        self.assertEqual([], self._report(include_codex=False)["sessions"])

    def test_an_unreadable_codex_store_does_not_break_the_report(self) -> None:
        codex = Path(self.home) / ".codex"
        codex.mkdir(parents=True)
        (codex / "state_1.sqlite").write_text("not a database", encoding="utf-8")
        self._session("here")

        self.assertEqual({"here": "unattributed"}, self._verdicts(self._report()))

    def test_the_report_writes_nothing(self) -> None:
        self._session(
            "bypass",
            before=self._snapshot("before"),
            after=self._snapshot("after"),
            edits=3,
        )
        before = {
            path: path.read_bytes()
            for path in Path(self.home).rglob("*")
            if path.is_file()
        }

        self._report()

        after = {
            path: path.read_bytes()
            for path in Path(self.home).rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_the_command_reports_and_prunes_through_the_cli(self) -> None:
        self._session(
            "bypass",
            before=self._snapshot("before"),
            after=self._snapshot("after"),
            edits=3,
        )
        environment = dict(os.environ)
        environment["GRAPH_HARNESS_HOME"] = self.home
        directory = journal.journal_directory(self.home)
        for month in ("2020-01", "2020-02"):
            (directory / f"{month}.jsonl").write_text("", encoding="utf-8")

        reported = subprocess.run(
            [sys.executable, str(GRAPHCTL), "conformance", "--no-codex"],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        pruned = subprocess.run(
            [
                sys.executable,
                str(GRAPHCTL),
                "conformance",
                "--prune",
                "--keep-months",
                "1",
            ],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

        self.assertEqual(0, reported.returncode, reported.stderr)
        self.assertEqual(
            "bypass", json.loads(reported.stdout)["sessions"][0]["verdict"]
        )
        self.assertEqual(0, pruned.returncode, pruned.stderr)
        self.assertEqual(
            ["2020-01.jsonl", "2020-02.jsonl"], json.loads(pruned.stdout)["pruned"]
        )

    def test_the_command_leaves_the_graph_alone(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as workspace:
            graph = Path(workspace) / "task-graph.json"
            environment = dict(os.environ)
            environment["GRAPH_HARNESS_HOME"] = self.home
            created = subprocess.run(
                [
                    sys.executable,
                    str(GRAPHCTL),
                    "--graph",
                    str(graph),
                    "init",
                    "--objective",
                    "leave me alone",
                    "--criterion",
                    "unchanged",
                ],
                cwd=REPOSITORY,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
            )
            self.assertEqual(0, created.returncode, created.stderr)
            before = graph.read_bytes()

            subprocess.run(
                [sys.executable, str(GRAPHCTL), "--graph", str(graph), "conformance"],
                cwd=REPOSITORY,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
            )

            self.assertEqual(before, graph.read_bytes())


if __name__ == "__main__":
    unittest.main()
