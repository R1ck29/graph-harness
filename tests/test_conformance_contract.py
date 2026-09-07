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
from unittest import mock

from agent_harness import cli, codex_sessions, journal
from agent_harness.errors import HarnessError
from tests import workspace_root

REPOSITORY = Path(__file__).resolve().parents[1]
GRAPHCTL = REPOSITORY / "scripts" / "graphctl.py"


class TransitionJournalTests(unittest.TestCase):
    """A session that skips the graph is only visible if the graph is recorded."""

    def setUp(self) -> None:
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        self._workspace = tempfile.TemporaryDirectory(dir=workspace_root())
        self.addCleanup(self._workspace.cleanup)
        self.home = self._home.name
        self.graph = Path(self._workspace.name) / "task-graph.json"

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["GRAPH_HARNESS_HOME"] = self.home
        environment["CLAUDE_CODE_SESSION_ID"] = "session-under-test"
        return subprocess.run(
            [sys.executable, str(GRAPHCTL), "--graph", str(self.graph), *arguments],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

    def _transitions(self) -> list[dict[str, Any]]:
        return [
            entry
            for entry in journal.read(self.home)
            if entry.get("event") == "graph_transition"
        ]

    def _init(self) -> None:
        created = self._run(
            "init",
            "--objective",
            "exercise the journal",
            "--criterion",
            "the transition is recorded",
        )
        self.assertEqual(0, created.returncode, created.stderr)

    def test_each_successful_state_change_records_one_transition(self) -> None:
        self._init()
        self.assertEqual(0, self._run("start", "task", "--executor-id", "e").returncode)
        self.assertEqual(
            0,
            self._run(
                "submit",
                "task",
                "--actor-id",
                "e",
                "--evidence",
                "observed",
                "--criterion",
                "the transition is recorded",
            ).returncode,
        )

        transitions = self._transitions()

        self.assertEqual(
            ["init", "start", "submit"], [entry["command"] for entry in transitions]
        )
        self.assertEqual("session-under-test", transitions[0]["session_id"])
        self.assertEqual("claude", transitions[0]["client"])
        self.assertEqual("task", transitions[1]["node"])
        self.assertEqual("running", transitions[1]["status"])

    def test_a_read_only_command_records_nothing(self) -> None:
        self._init()

        self.assertEqual(0, self._run("status").returncode)
        self.assertEqual(0, self._run("validate").returncode)
        self.assertEqual(0, self._run("doctor").returncode)

        self.assertEqual(["init"], [entry["command"] for entry in self._transitions()])

    def test_a_failing_command_records_nothing(self) -> None:
        self._init()

        refused = self._run("start", "missing-node", "--executor-id", "e")

        self.assertEqual(2, refused.returncode)
        self.assertEqual(["init"], [entry["command"] for entry in self._transitions()])

    def test_an_unwritable_journal_leaves_the_command_unchanged(self) -> None:
        self._init()

        with mock.patch("agent_harness.journal.append", return_value=False) as blocked:
            arguments = cli.build_parser().parse_args(
                ["--graph", str(self.graph), "status"]
            )
            healthy = cli.run(arguments)

        self.assertFalse(blocked.called)
        self.assertIn("counts", healthy)

    def test_a_raising_journal_cannot_fail_the_command(self) -> None:
        # journal.append is contracted never to raise, but the CLI must not
        # depend on that contract holding to keep working.
        self._init()
        started = self._run("start", "task", "--executor-id", "e")
        self.assertEqual(0, started.returncode, started.stderr)

        with mock.patch(
            "agent_harness.journal.append", side_effect=OSError("journal is gone")
        ):
            arguments = cli.build_parser().parse_args(
                ["--graph", str(self.graph), "status"]
            )
            summary = cli.run(arguments)

        self.assertIn("counts", summary)

    def test_stdout_is_identical_with_and_without_a_working_journal(self) -> None:
        self._init()
        with_journal = self._run("status")

        environment = dict(os.environ)
        environment["GRAPH_HARNESS_HOME"] = "/nonexistent/journal/home"
        without_journal = subprocess.run(
            [sys.executable, str(GRAPHCTL), "--graph", str(self.graph), "status"],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

        self.assertEqual(with_journal.returncode, without_journal.returncode)
        self.assertEqual(with_journal.stdout, without_journal.stdout)
        self.assertEqual("", without_journal.stderr)

    def test_a_journaling_failure_cannot_turn_a_success_into_an_error(self) -> None:
        # The command's work is already committed when _observe runs, so an
        # exception there would report failure for a change that happened.
        self._init()

        # journal.record is reached only from _observe, after the state change
        # has been written, so failing it isolates the observation layer.
        with mock.patch(
            "agent_harness.cli.journal.record", side_effect=OSError("journal is gone")
        ):
            outcome = cli.main(
                ["--graph", str(self.graph), "start", "task", "--executor-id", "e"]
            )

        self.assertEqual(0, outcome)
        stored = json.loads(self.graph.read_text(encoding="utf-8"))
        self.assertEqual("running", stored["nodes"][0]["status"])

    def test_a_transition_names_the_actor_that_caused_it(self) -> None:
        self._init()
        self._run("start", "task", "--executor-id", "executor-a")
        self._run(
            "submit",
            "task",
            "--actor-id",
            "executor-a",
            "--evidence",
            "observed",
            "--criterion",
            "the transition is recorded",
        )
        self._run(
            "verify",
            "task",
            "--pass",
            "--reviewer-id",
            "reviewer-b",
            "--criterion",
            "the transition is recorded",
            "--evidence",
            "checked",
        )

        transitions = self._transitions()

        self.assertEqual(
            [None, "executor-a", "executor-a", "reviewer-b"],
            [entry.get("actor") for entry in transitions],
        )
        # A verify reports a verdict rather than a status; folding a session
        # cannot tell a PASS from a FAIL unless the verdict is recorded.
        self.assertEqual("PASS", transitions[-1]["status"])


class DoctorDiagnosticsTests(unittest.TestCase):
    """doctor is the documented place to see whether the harness is wired up."""

    def setUp(self) -> None:
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        self._workspace = tempfile.TemporaryDirectory(dir=workspace_root())
        self.addCleanup(self._workspace.cleanup)
        self.home = self._home.name
        self.graph = Path(self._workspace.name) / "task-graph.json"

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["GRAPH_HARNESS_HOME"] = self.home
        return subprocess.run(
            [sys.executable, str(GRAPHCTL), "--graph", str(self.graph), *arguments],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

    def _install_runtime(self, version: str) -> None:
        config = (
            Path(self.home)
            / ".local/share/graph-engineering-agent-harness/venv/pyvenv.cfg"
        )
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(f"home = /somewhere\nversion = {version}\n", encoding="utf-8")

    def _diagnose(self) -> dict[str, Any]:
        self._run(
            "init", "--objective", "diagnose", "--criterion", "the report is honest"
        )
        diagnosed = self._run("doctor")
        self.assertEqual(0, diagnosed.returncode, diagnosed.stderr)
        report: dict[str, Any] = json.loads(diagnosed.stdout)
        return report

    def test_the_existing_contract_keys_keep_their_meaning(self) -> None:
        lock_path = self.graph.with_suffix(self.graph.suffix + ".lock")
        self._run(
            "init", "--objective", "diagnose", "--criterion", "the report is honest"
        )
        lock_path.write_text("43210\n", encoding="utf-8")
        graph_before = self.graph.read_text(encoding="utf-8")

        diagnosed = self._run("doctor")

        report = json.loads(diagnosed.stdout)
        self.assertTrue(report["graph_valid"])
        self.assertTrue(report["lock_present"])
        self.assertFalse(report["healthy"])
        self.assertEqual(str(lock_path), report["lock_path"])
        self.assertEqual(43210, report["lock_pid"])
        self.assertEqual(graph_before, self.graph.read_text(encoding="utf-8"))

    def test_the_last_observation_per_client_comes_from_recorded_sessions(
        self,
    ) -> None:
        journal.append(
            journal.record("session_open", client="claude", session_id="s1"), self.home
        )

        report = self._diagnose()

        self.assertIn("claude", report["last_observed"])
        self.assertTrue(report["journal_months"])
        self.assertGreater(report["journal_bytes"], 0)

    def test_a_client_that_never_fired_is_named_rather_than_assumed_working(
        self,
    ) -> None:
        report = self._diagnose()

        warnings = " ".join(report["warnings"])
        self.assertIn("No claude session has been observed", warnings)
        self.assertIn("No codex session has been observed", warnings)

    def test_edit_hook_liveness_is_reported_separately_from_boundaries(
        self,
    ) -> None:
        # A client can record boundaries perfectly and have no PostToolUse
        # hook at all, which leaves the working-tree comparison carrying the
        # signal alone. Reporting only session liveness would call that
        # installation healthy.
        journal.append(
            journal.record("session_open", client="claude", session_id="s1"), self.home
        )

        report = self._diagnose()

        self.assertIn("claude", report["last_observed"])
        self.assertEqual({}, report["edit_hook_last_seen"])
        self.assertIn("No claude edit has been observed", " ".join(report["warnings"]))

    def test_an_observed_edit_clears_the_edit_hook_warning(self) -> None:
        journal.append(
            journal.record("session_open", client="claude", session_id="s1"), self.home
        )
        journal.append(
            journal.record(
                "edit", client="claude", session_id="s1", tool="Edit", path_id="a" * 12
            ),
            self.home,
        )

        report = self._diagnose()

        self.assertIn("claude", report["edit_hook_last_seen"])
        self.assertNotIn(
            "No claude edit has been observed", " ".join(report["warnings"])
        )

    def test_a_runtime_below_the_supported_floor_is_warned_about(self) -> None:
        self._install_runtime("3.8.5")

        report = self._diagnose()

        self.assertEqual("3.8.5", report["runtime_version"])
        self.assertFalse(report["runtime_supported"])
        self.assertIn("below the supported 3.10", " ".join(report["warnings"]))

    def test_a_supported_runtime_produces_no_runtime_warning(self) -> None:
        self._install_runtime("3.13.11")

        report = self._diagnose()

        self.assertTrue(report["runtime_supported"])
        self.assertNotIn("below the supported", " ".join(report["warnings"]))

    def test_observation_findings_do_not_change_graph_health(self) -> None:
        report = self._diagnose()

        self.assertTrue(report["warnings"])
        self.assertTrue(report["healthy"])

    def test_doctor_writes_nothing(self) -> None:
        self._run(
            "init", "--objective", "diagnose", "--criterion", "the report is honest"
        )
        before = {
            path: path.read_bytes()
            for path in Path(self.home).rglob("*")
            if path.is_file()
        }

        self._run("doctor")

        after = {
            path: path.read_bytes()
            for path in Path(self.home).rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_a_journal_damaged_mid_character_does_not_break_the_report(self) -> None:
        # A killed writer can cut a line inside a multibyte character. Strict
        # decoding turned that into an unhandled error, which removed every
        # diagnostic key and disabled the lock recovery the security notes
        # prescribe.
        directory = (
            Path(self.home) / ".local/share/graph-engineering-agent-harness/journal"
        )
        directory.mkdir(parents=True)
        intact = json.dumps(
            {
                "schema_version": 1,
                "event": "session_open",
                "client": "claude",
                "ts": "2026-08-01T00:00:00+00:00",
            }
        ).encode("utf-8")
        truncated = json.dumps(
            {"event": "turn_end", "repo": "/Users/rick/プロジェクト"},
            ensure_ascii=False,
        ).encode("utf-8")[:-4]
        self.assertRaises(UnicodeDecodeError, truncated.decode, "utf-8")
        (directory / "2026-08.jsonl").write_bytes(intact + b"\n" + truncated + b"\n")

        report = self._diagnose()

        self.assertIn("claude", report["last_observed"])
        self.assertTrue(report["graph_valid"])
        self.assertIn("lock_path", report)

    def test_doctor_does_not_import_the_installer_scripts(self) -> None:
        source = (REPOSITORY / "agent_harness" / "cli.py").read_text(encoding="utf-8")

        self.assertNotIn("import scripts", source)
        self.assertNotIn("from scripts", source)


class CodexSessionReaderTests(unittest.TestCase):
    """Codex hooks do not run on the measured clients; its own store is the source."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.home = Path(self._directory.name)
        self.codex = self.home / ".codex"
        self.codex.mkdir()
        self.addCleanup(os.environ.pop, "CODEX_HOME", None)
        os.environ.pop("CODEX_HOME", None)

    def _store(self, name: str, columns: str, rows: list[tuple[object, ...]]) -> Path:
        path = self.codex / name
        connection = sqlite3.connect(path)
        with connection:
            connection.execute(f"CREATE TABLE threads ({columns})")
            placeholders = ", ".join("?" for _ in rows[0]) if rows else ""
            for row in rows:
                connection.execute(f"INSERT INTO threads VALUES ({placeholders})", row)
        connection.close()
        return path

    def test_no_codex_installation_reports_no_sessions_rather_than_failing(
        self,
    ) -> None:
        self.assertIsNone(codex_sessions.state_path(self.home))
        self.assertEqual([], codex_sessions.sessions(self.home))

    def test_the_highest_generation_store_is_the_one_read(self) -> None:
        columns = (
            "id TEXT, cwd TEXT, created_at INTEGER, updated_at INTEGER, source TEXT"
        )
        self._store("state_2.sqlite", columns, [("old", "/a", 1, 2, "cli")])
        self._store("state_10.sqlite", columns, [("new", "/b", 3, 4, "exec")])

        selected = codex_sessions.state_path(self.home)

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual("state_10.sqlite", selected.name)
        self.assertEqual(
            ["new"],
            [item["session_id"] for item in codex_sessions.sessions(self.home)],
        )

    def test_metadata_is_extracted_and_message_content_is_not_read(self) -> None:
        self._store(
            "state_5.sqlite",
            "id TEXT, cwd TEXT, created_at INTEGER, updated_at INTEGER, "
            "source TEXT, first_user_message TEXT, title TEXT, preview TEXT",
            [
                (
                    "01a0",
                    "/repo",
                    1788088667,
                    1788088674,
                    "exec",
                    "a private prompt",
                    "a private title",
                    "a private preview",
                )
            ],
        )

        observed = codex_sessions.sessions(self.home)

        self.assertEqual(
            [
                {
                    "client": "codex",
                    "session_id": "01a0",
                    "repo": "/repo",
                    "started_at": 1788088667,
                    "ended_at": 1788088674,
                    "kind": "exec",
                }
            ],
            observed,
        )
        rendered = json.dumps(observed)
        self.assertNotIn("private", rendered)

    def test_a_subagent_source_is_classified_without_exposing_the_blob(self) -> None:
        self._store(
            "state_5.sqlite",
            "id TEXT, cwd TEXT, created_at INTEGER, updated_at INTEGER, source TEXT",
            [
                ("a", "/r", 1, 2, '{"subagent":{"thread_id":"x"}}'),
                ("b", "/r", 3, 4, "vscode"),
                ("c", "/r", 5, 6, None),
            ],
        )

        kinds = [item["kind"] for item in codex_sessions.sessions(self.home)]

        self.assertEqual(["subagent", "vscode", "unknown"], kinds)

    def test_a_millisecond_timestamp_is_reduced_to_seconds(self) -> None:
        self._store(
            "state_5.sqlite",
            "id TEXT, cwd TEXT, created_at INTEGER, updated_at INTEGER, source TEXT",
            [("a", "/r", 1788088667000, 1788088674000, "exec")],
        )

        observed = codex_sessions.sessions(self.home)[0]

        self.assertEqual(1788088667, observed["started_at"])
        self.assertEqual(1788088674, observed["ended_at"])

    def test_a_store_missing_a_required_column_fails_by_name(self) -> None:
        self._store(
            "state_5.sqlite",
            "id TEXT, created_at INTEGER, updated_at INTEGER, source TEXT",
            [("a", 1, 2, "exec")],
        )

        with self.assertRaisesRegex(HarnessError, "has no cwd in its threads table"):
            codex_sessions.sessions(self.home)

    def test_a_store_without_the_threads_table_fails_rather_than_degrading(
        self,
    ) -> None:
        path = self.codex / "state_5.sqlite"
        connection = sqlite3.connect(path)
        with connection:
            connection.execute("CREATE TABLE logs (id INTEGER)")
        connection.close()

        with self.assertRaisesRegex(HarnessError, "threads table"):
            codex_sessions.sessions(self.home)

    def test_the_store_is_opened_read_only(self) -> None:
        self._store(
            "state_5.sqlite",
            "id TEXT, cwd TEXT, created_at INTEGER, updated_at INTEGER, source TEXT",
            [("a", "/r", 1, 2, "exec")],
        )
        path = self.codex / "state_5.sqlite"
        before = path.read_bytes()

        codex_sessions.sessions(self.home)
        connection = codex_sessions._connect(path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("INSERT INTO threads VALUES ('x','/r',1,2,'exec')")
        finally:
            connection.close()

        self.assertEqual(before, path.read_bytes())

    def test_a_home_containing_uri_delimiters_stays_read_only(self) -> None:
        # A path holding # or ? truncates an interpolated SQLite URI at that
        # character, which drops mode=ro and makes SQLite create an empty
        # database at the truncated path.
        for awkward in ("has#hash", "has?query"):
            with self.subTest(directory=awkward):
                root = Path(self._directory.name) / awkward
                codex = root / ".codex"
                codex.mkdir(parents=True)
                path = codex / "state_1.sqlite"
                connection = sqlite3.connect(path)
                with connection:
                    connection.execute(
                        "CREATE TABLE threads (id TEXT, cwd TEXT, "
                        "created_at INTEGER, updated_at INTEGER, source TEXT)"
                    )
                    connection.execute(
                        "INSERT INTO threads VALUES ('a','/r',1,2,'exec')"
                    )
                connection.close()
                before = sorted(
                    entry.name for entry in Path(self._directory.name).iterdir()
                )

                observed = codex_sessions.sessions(root)

                self.assertEqual(["a"], [item["session_id"] for item in observed])
                self.assertEqual(
                    before,
                    sorted(
                        entry.name for entry in Path(self._directory.name).iterdir()
                    ),
                )

    def test_an_explicit_codex_home_wins_over_the_user_home(self) -> None:
        elsewhere = self.home / "other-codex"
        elsewhere.mkdir()
        os.environ["CODEX_HOME"] = str(elsewhere)

        self.assertEqual(elsewhere, codex_sessions.codex_home(self.home))


if __name__ == "__main__":
    unittest.main()


class EscapeHatchTransitionTests(unittest.TestCase):
    """The two escape hatches must be as visible as the failures they follow."""

    def setUp(self) -> None:
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        self._workspace = tempfile.TemporaryDirectory(dir=workspace_root())
        self.addCleanup(self._workspace.cleanup)
        self.home = Path(self._home.name)
        self.workspace = Path(self._workspace.name)
        self._environment = mock.patch.dict(
            os.environ, {"GRAPH_HARNESS_HOME": str(self.home)}
        )
        self._environment.start()
        self.addCleanup(self._environment.stop)

    def _run(self, *arguments: str) -> int:
        previous = os.getcwd()
        os.chdir(self.workspace)
        try:
            return cli.main(list(arguments))
        finally:
            os.chdir(previous)

    def _commands(self) -> list[str]:
        return [
            str(entry.get("command"))
            for entry in journal.read(self.home)
            if entry.get("event") == "graph_transition"
        ]

    def _exhaust(self) -> None:
        self._run("init", "--objective", "o", "--criterion", "c", "--node", "task")
        for _ in range(2):
            self._run("start", "task", "--executor-id", "e1")
            self._run("submit", "task", "--actor-id", "e1", "--evidence", "tried")
            self._run(
                "verify",
                "task",
                "--fail",
                "--reviewer-id",
                "r1",
                "--reason",
                "not yet",
                "--failed-criterion",
                "c",
                "--evidence",
                "reviewer reran",
            )
            if self._run("retry", "task") != 0:
                break

    def test_a_granted_attempt_is_recorded_as_a_transition(self) -> None:
        self._exhaust()

        outcome = self._run(
            "grant-attempt",
            "task",
            "--granted-by",
            "rick",
            "--reason",
            "design changed",
        )

        self.assertEqual(0, outcome)
        self.assertIn("grant-attempt", self._commands())
        self.assertEqual(0, self._run("retry", "task"))

    def test_a_grant_without_a_grantor_is_refused_by_the_parser(self) -> None:
        self._exhaust()

        with self.assertRaises(SystemExit):
            self._run("grant-attempt", "task", "--reason", "design changed")

    def test_superseding_is_recorded_as_a_transition(self) -> None:
        self._exhaust()

        outcome = self._run("supersede", "task", "--reason", "approach abandoned")

        self.assertEqual(0, outcome)
        self.assertIn("supersede", self._commands())
        self.assertEqual(0, self._run("completion-check"))


class BrokenInstallationTests(unittest.TestCase):
    """doctor is the one command that must not break when the install is.

    Found by the verification gate, not by this suite: with the install root a
    regular file rather than a directory, `graphctl doctor` exited 2 with a raw
    `[Errno 20] Not a directory`, while `conformance` and `effectiveness`
    both survived the same home. Two separate reads were guarded against
    `HarnessError` only, and resolving a path touches the filesystem, so the
    failure arrived as an errno instead.
    """

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.home = Path(self._directory.name)
        (self.home / ".local" / "share").mkdir(parents=True)
        # Where the install root belongs, put a file.
        (self.home / ".local" / "share" / "graph-engineering-agent-harness").write_text(
            "not a directory", encoding="utf-8"
        )
        self._environment = mock.patch.dict(
            os.environ, {"GRAPH_HARNESS_HOME": str(self.home)}
        )
        self._environment.start()
        self.addCleanup(self._environment.stop)

    def test_every_reporting_command_survives_an_unusable_install_root(self) -> None:
        workspace = Path(self._directory.name) / "work"
        workspace.mkdir()
        previous = os.getcwd()
        os.chdir(workspace)
        self.addCleanup(os.chdir, previous)

        for command in (["doctor"], ["conformance"], ["effectiveness"]):
            with self.subTest(command=command[0]):
                self.assertEqual(0, cli.main(command), command)

    def test_doctor_names_the_broken_journal_rather_than_raising(self) -> None:
        report = cli._journal_diagnostics()

        self.assertEqual(0, report["journal_bytes"])
        self.assertEqual([], report["journal_months"])
        self.assertTrue(report["warnings"])
