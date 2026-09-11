from __future__ import annotations

import itertools
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_harness import cli, codex_sessions, conformance, journal
from agent_harness.errors import HarnessError
from agent_harness.graph import utc_from_epoch
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

    @unittest.skipIf(os.name == "nt", "POSIX named pipe")
    def test_a_runtime_config_that_is_a_pipe_does_not_block_doctor(self) -> None:
        # The last reader in this harness that opened a file it did not write
        # without the bounded opener. A FIFO named `pyvenv.cfg` blocked this
        # for ever — and doctor is the one command the recovery procedure
        # depends on, so it is the worst place for that to be true.
        #
        # Driven in the subprocess doctor actually runs in, with a timeout,
        # so a regression fails rather than hangs the suite.
        config = (
            Path(self.home)
            / ".local/share/graph-engineering-agent-harness/venv/pyvenv.cfg"
        )
        config.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(config)
        self._run(
            "init", "--objective", "diagnose", "--criterion", "the report is honest"
        )

        environment = dict(os.environ)
        environment["GRAPH_HARNESS_HOME"] = self.home
        done = subprocess.run(
            [sys.executable, str(GRAPHCTL), "--graph", str(self.graph), "doctor"],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
            timeout=60,
        )

        self.assertEqual(0, done.returncode, done.stderr)
        report = json.loads(done.stdout)
        self.assertIsNone(report["runtime_version"])
        self.assertIsNone(report["runtime_supported"])
        self.assertTrue(report["healthy"], "a bad runtime file is not a bad graph")

    def test_a_runtime_config_over_the_bound_is_refused_rather_than_read(
        self,
    ) -> None:
        # The same guard as the pipe above, pinned on an outcome a mutation
        # can change. Two candidates were not. "Does not block" is not an
        # outcome: an opener without the type check reads the pipe as empty
        # and reports exactly what the refusal reports. Nor is a symlink:
        # `user_data_path` walks the components and refuses a link before
        # this function opens anything, so that never reaches the opener
        # either. The size bound does distinguish them — refused here, read
        # by a plain open, which would report the version inside a file of
        # any length.
        config = (
            Path(self.home)
            / ".local/share/graph-engineering-agent-harness/venv/pyvenv.cfg"
        )
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            "version = 3.99.0\n" + "padding = x\n" * 20_000, encoding="utf-8"
        )
        self.assertGreater(
            config.stat().st_size, cli.MAX_RUNTIME_CONFIG_BYTES, "not over the bound"
        )

        report = self._diagnose()

        self.assertIsNone(report["runtime_version"], "an unbounded file was read")
        self.assertIsNone(report["runtime_supported"])

    @unittest.skipIf(os.name == "nt", "POSIX symlink")
    def test_a_runtime_config_reached_through_a_symlink_is_not_read(self) -> None:
        # Held by the path walk in `user_data_path` rather than by the
        # opener, which is why it pins no guard here. Kept because the
        # property matters on its own: the runtime version must come from
        # the install tree and not from wherever a link points.
        real = Path(self.home) / "elsewhere.cfg"
        real.write_text("home = /somewhere\nversion = 3.99.0\n", encoding="utf-8")
        config = (
            Path(self.home)
            / ".local/share/graph-engineering-agent-harness/venv/pyvenv.cfg"
        )
        config.parent.mkdir(parents=True, exist_ok=True)
        config.symlink_to(real)

        report = self._diagnose()

        self.assertIsNone(report["runtime_version"], "a link was followed")
        self.assertIsNone(report["runtime_supported"])

    def _months(self) -> Path:
        months = (
            Path(self.home) / ".local/share/graph-engineering-agent-harness/journal"
        )
        months.mkdir(parents=True, exist_ok=True)
        return months

    def _month(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "event": "session_open",
                    "ts": "2026-08-01T00:00:00+00:00",
                    "client": "claude",
                    "session_id": "s1",
                    "repo": "/repo",
                }
            )
            + "\n",
            encoding="utf-8",
        )

    @unittest.skipIf(os.name == "nt", "POSIX named pipe")
    def test_a_month_refused_before_it_is_opened_is_reported(self) -> None:
        # Two silences, and this is the one no caller of `read` can see: a
        # file named like a month that is a pipe never reaches `month_files`
        # at all, because naming a month and opening one are deliberately
        # separate checks. doctor listed the readable months and left a
        # reader to assume that was all of them.
        months = self._months()
        self._month(months / "2026-07.jsonl")
        os.mkfifo(months / "2026-06.jsonl")

        report = self._diagnose()
        warnings = " ".join(report["warnings"])

        self.assertIn("2026-07.jsonl", report["journal_months"])
        self.assertNotIn("2026-06.jsonl", report["journal_months"])
        self.assertIn("2026-06.jsonl is named like a month and is not read", warnings)
        self.assertIn("not a plain file", warnings)
        self.assertNotIn("2026-07.jsonl is named", warnings)

    @unittest.skipIf(os.name == "nt", "POSIX file permissions")
    def test_a_month_that_passes_its_name_and_cannot_be_opened_is_reported(
        self,
    ) -> None:
        # The other silence: a plain file, correctly named, that `read` drops
        # with a `continue`. It does reach `month_files`, so doctor listed it
        # among the months it had read.
        months = self._months()
        unreadable = months / "2026-06.jsonl"
        self._month(unreadable)
        unreadable.chmod(0o000)
        self.addCleanup(unreadable.chmod, 0o600)

        report = self._diagnose()
        warnings = " ".join(report["warnings"])

        self.assertIn("2026-06.jsonl", report["journal_months"])
        self.assertIn("2026-06.jsonl is named like a month and is not read", warnings)
        self.assertIn("cannot be opened", warnings)

    def test_a_readable_month_is_not_reported_as_unreadable(self) -> None:
        # The other half: the warning must not fire for every month, which a
        # check written the wrong way round would do while looking right.
        months = self._months()
        self._month(months / "2026-06.jsonl")
        self._month(months / "2026-07.jsonl")

        report = self._diagnose()

        for name in ("2026-06.jsonl", "2026-07.jsonl"):
            self.assertIn(name, report["journal_months"])
        self.assertEqual([], [w for w in report["warnings"] if "is not read" in w])

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
                    # The hooks' own form, not the integer Codex stores.
                    # A Codex verdict is read in the same list and under the
                    # same keys as a hook-observed one, and every consumer
                    # that had to guard for two forms got one of them wrong.
                    "started_at": "2026-08-30T11:17:47+00:00",
                    "ended_at": "2026-08-30T11:17:54+00:00",
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

    def test_a_millisecond_column_names_the_same_instant_as_a_second_one(
        self,
    ) -> None:
        # Codex carries both units, so the reader has to tell them apart
        # before rendering. Asserted against the second-column store below
        # rather than against a literal, so the two units are held to naming
        # one instant rather than to two hand-written strings.
        self._store(
            "state_5.sqlite",
            "id TEXT, cwd TEXT, created_at INTEGER, updated_at INTEGER, source TEXT",
            [("a", "/r", 1788088667000, 1788088674000, "exec")],
        )
        milliseconds = codex_sessions.sessions(self.home)[0]

        self._store(
            "state_6.sqlite",
            "id TEXT, cwd TEXT, created_at INTEGER, updated_at INTEGER, source TEXT",
            [("a", "/r", 1788088667, 1788088674, "exec")],
        )
        seconds = codex_sessions.sessions(self.home)[0]

        self.assertEqual("2026-08-30T11:17:47+00:00", milliseconds["started_at"])
        self.assertEqual("2026-08-30T11:17:54+00:00", milliseconds["ended_at"])
        self.assertEqual(seconds["started_at"], milliseconds["started_at"])
        self.assertEqual(seconds["ended_at"], milliseconds["ended_at"])

    def test_a_codex_timestamp_sorts_against_a_hook_timestamp(self) -> None:
        # The reason the form matters. Compared as text, the integer Codex
        # stores sorts before every ISO timestamp whatever its date, so a
        # session from today was listed before one from last year.
        self._store(
            "state_5.sqlite",
            "id TEXT, cwd TEXT, created_at INTEGER, updated_at INTEGER, source TEXT",
            [("a", "/r", 1788088667, 1788088674, "exec")],
        )

        recovered = codex_sessions.sessions(self.home)[0]["started_at"]

        self.assertGreater(recovered, "2026-01-01T00:00:00+00:00")
        self.assertLess(recovered, "2026-12-31T00:00:00+00:00")

    def test_an_infinite_column_costs_one_session_and_not_every_session(
        self,
    ) -> None:
        # The column is REAL and SQLite round-trips 9e999 to `inf`, which
        # `int` refuses with OverflowError. That escaped `sessions()` and was
        # swallowed by the blanket handler in `_codex_verdicts`, which drops
        # every Codex session rather than the one with the bad column.
        self._store(
            "state_5.sqlite",
            "id TEXT, cwd TEXT, created_at REAL, updated_at REAL, source TEXT",
            [
                ("bad", "/r", 9e999, 9e999, "exec"),
                ("good", "/r", 1788088667, 1788088674, "exec"),
            ],
        )

        observed = {
            item["session_id"]: item for item in codex_sessions.sessions(self.home)
        }

        self.assertEqual({"bad", "good"}, set(observed))
        self.assertIsNone(observed["bad"]["started_at"])
        self.assertEqual("2026-08-30T11:17:47+00:00", observed["good"]["started_at"])

    def test_a_column_too_large_for_the_platform_is_refused_not_raised(self) -> None:
        # Each arm of the converter's handler is reachable with a different
        # magnitude, after the millisecond division: 1e18 becomes 1e15 and
        # raises ValueError, 1e21 becomes 1e18 and raises OSError, and 1e24
        # becomes 1e21 and raises OverflowError. Asserted together so
        # narrowing the tuple to any one of them is caught.
        #
        # Stored in REAL columns, because SQLite's INTEGER is 64-bit and
        # refuses anything above about 9.2e18 outright — so an integer column
        # can only ever reach the ValueError arm, and a test written against
        # one would have left the other two unexercised while looking
        # thorough.
        self._store(
            "state_5.sqlite",
            "id TEXT, cwd TEXT, created_at REAL, updated_at REAL, source TEXT",
            [
                ("value-error", "/r", 1e18, 1e18, "exec"),
                ("os-error", "/r", 1e21, 1e21, "exec"),
                ("overflow-error", "/r", 1e24, 1e24, "exec"),
            ],
        )

        observed = codex_sessions.sessions(self.home)

        self.assertEqual(3, len(observed))
        self.assertEqual([None, None, None], [item["started_at"] for item in observed])
        self.assertEqual(["/r", "/r", "/r"], [item["repo"] for item in observed])

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
                try:
                    codex.mkdir(parents=True)
                except OSError as exc:  # pragma: no cover - platform dependent
                    # Windows forbids ? in a filename, so the directory
                    # cannot exist there to be tested. The defect this
                    # guards is URI truncation inside SQLite, which is not
                    # Windows-specific; only the fixture is.
                    self.skipTest(f"{awkward!r} is not a legal name here: {exc}")
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


class UnobservedWindowTests(unittest.TestCase):
    """A window nobody observed must not decide that two sessions overlapped.

    Rendering Codex's timestamps into the hooks' form made them comparable,
    and that turned out to be the wrong thing to act on. `updated_at` is when
    the thread row was last written, not when the session ended. Counting
    only the rows that become verdicts — subagent threads never reach the
    report — a reviewer measured nine of twenty-nine windows spanning over a
    day on a real store, the widest twenty-four days. Acting on those marked
    five of the twenty-nine as contested by each other, and marked a fully
    observed session as contested by a row that merely spanned it.
    """

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.home = Path(self._directory.name)

    def _codex_store(self, rows: list[tuple[object, ...]]) -> None:
        codex = self.home / ".codex"
        codex.mkdir(exist_ok=True)
        connection = sqlite3.connect(codex / "state_5.sqlite")
        with connection:
            connection.execute(
                "CREATE TABLE threads (id TEXT, cwd TEXT, created_at INTEGER, "
                "updated_at INTEGER, source TEXT)"
            )
            for row in rows:
                connection.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?)", row)
        connection.close()

    def _observed_session(
        self, session: str, repo: str, opened: int, closed: int
    ) -> None:
        for event, stamp in (("session_open", opened), ("session_close", closed)):
            entry = journal.record(
                event,
                client="claude",
                session_id=session,
                repo=repo,
                snapshot={
                    "git": True,
                    "head": "0" * 40,
                    "digest": "a" * 64,
                    "files": 1,
                    "lines": 3,
                    "degraded": False,
                },
            )
            entry["ts"] = utc_from_epoch(stamp)
            self.assertTrue(journal.append(entry, self.home))
        edit = journal.record(
            "edit",
            client="claude",
            session_id=session,
            repo=repo,
            tool="Edit",
            path_id="abcdef123456",
        )
        edit["ts"] = utc_from_epoch(opened + 1)
        self.assertTrue(journal.append(edit, self.home))
        # A graphctl run inside the window, so the session is `conformant`
        # rather than `bypass`. The sharpest form of the defect is a session
        # that followed the protocol being told its work may belong to
        # something else.
        moved = journal.record(
            "graph_transition",
            client="claude",
            session_id=session,
            repo=repo,
            command="submit",
        )
        moved["ts"] = utc_from_epoch(opened + 2)
        self.assertTrue(journal.append(moved, self.home))

    def test_a_codex_row_lifetime_does_not_contest_an_observed_session(self) -> None:
        # The reviewer's counter-example, kept. The Codex window is 23.6 days
        # wide, the observed session is 30 seconds inside it, and that
        # session's one edit is attributable to it alone.
        wide_start, wide_end = 1786000000, 1786000000 + 2_041_985
        self._codex_store([("codex-wide", "/repo", wide_start, wide_end, "exec")])
        self._observed_session("hook", "/repo", wide_start + 100, wide_start + 130)

        report = conformance.report(self.home)
        by_id = {item["session_id"]: item for item in report["sessions"]}

        self.assertEqual("conformant", by_id["hook"]["verdict"])
        self.assertFalse(
            by_id["hook"]["contested"], "an unobserved window contested it"
        )
        self.assertFalse(by_id["codex-wide"]["contested"])

    def test_two_codex_rows_do_not_contest_each_other(self) -> None:
        self._codex_store(
            [
                ("a", "/repo", 1786000000, 1786000100, "exec"),
                ("b", "/repo", 1786000050, 1786000150, "exec"),
            ]
        )

        report = conformance.report(self.home)

        self.assertEqual(
            [False, False], [item["contested"] for item in report["sessions"]]
        )

    def test_two_observed_sessions_still_contest_each_other(self) -> None:
        # The other half of the rule: excluding unobserved windows must not
        # exclude the case the flag exists for.
        self._observed_session("first", "/repo", 1786000000, 1786000100)
        self._observed_session("second", "/repo", 1786000050, 1786000150)

        report = conformance.report(self.home, include_codex=False)

        self.assertEqual(
            [True, True], [item["contested"] for item in report["sessions"]]
        )

    def test_a_verdict_that_says_nothing_about_its_window_is_refused(
        self,
    ) -> None:
        # The failure mode the field exists to prevent, applied to the field
        # itself: a verdict quietly exempt from the comparison reads exactly
        # like one that was compared and found alone, so a producer that
        # forgot to say which kind of window it carries must not be answered
        # silently.
        with self.assertRaisesRegex(HarnessError, "does not say what its"):
            conformance._contested(
                [{"session_id": "nameless", "repo": "/repo", "contested": False}]
            )

    def test_every_verdict_says_what_its_timestamps_describe(self) -> None:
        # A reader comparing two sessions has to be able to tell which pair
        # was observed, so the distinction is in the report rather than only
        # in this module.
        self._codex_store([("codex", "/repo", 1786000000, 1786000100, "exec")])
        self._observed_session("hook", "/other", 1786000000, 1786000100)

        report = conformance.report(self.home)
        bounds = {item["session_id"]: item["bounds"] for item in report["sessions"]}

        self.assertEqual("thread_lifetime", bounds["codex"])
        self.assertEqual("observed", bounds["hook"])
        self.assertEqual(3, report["schema_version"])


class ContestedEquivalenceTests(unittest.TestCase):
    """The narrowed search must answer exactly what comparing every pair did.

    The pairwise version is the specification, so it is written out here and
    the two are held equal over generated groups rather than argued about.
    The cases that matter are the ones a reader would not think to try: a
    session missing one bound, a session whose end precedes its start, two
    sessions with identical windows, and a session that touches another only
    at an endpoint.
    """

    WINDOWS = (
        ("2026-09-01T00:00:00+00:00", "2026-09-01T00:00:10+00:00"),
        ("2026-09-01T00:00:05+00:00", "2026-09-01T00:00:15+00:00"),
        ("2026-09-01T00:00:10+00:00", "2026-09-01T00:00:20+00:00"),
        ("2026-09-01T00:00:20+00:00", "2026-09-01T00:00:20+00:00"),
        ("2026-09-01T00:00:30+00:00", "2026-09-01T00:00:25+00:00"),
        ("2026-09-01T00:00:00+00:00", "2026-09-01T00:00:10+00:00"),
        (None, "2026-09-01T00:00:10+00:00"),
        ("2026-09-01T00:00:00+00:00", None),
        (None, None),
    )

    @staticmethod
    def _pairwise(group: list[dict[str, Any]]) -> list[bool]:
        """The implementation this replaces, kept as the specification."""

        answers = []
        for first in group:
            overlapped = False
            for second in group:
                if first is second:
                    continue
                if conformance._overlaps(first, second):
                    overlapped = True
                    break
            answers.append(overlapped)
        return answers

    def _group(self, rng: random.Random) -> list[dict[str, Any]]:
        group: list[dict[str, Any]] = []
        for index in range(rng.randint(0, 9)):
            started, ended = rng.choice(self.WINDOWS)
            group.append(
                {
                    "session_id": f"s{index}",
                    "repo": "/repo",
                    "started_at": started,
                    "ended_at": ended,
                    "contested": False,
                    "bounds": conformance.BOUNDS_OBSERVED,
                }
            )
        return group

    def test_the_narrowed_search_agrees_with_comparing_every_pair(self) -> None:
        rng = random.Random(20260911)
        for trial in range(500):
            with self.subTest(trial=trial):
                group = self._group(rng)
                expected = self._pairwise(group)

                for verdict in group:
                    verdict["contested"] = False
                conformance._mark_overlaps(group)

                self.assertEqual(
                    expected, [bool(verdict["contested"]) for verdict in group]
                )

    def test_it_agrees_whatever_order_the_sessions_arrive_in(self) -> None:
        # The pairwise version could not depend on order; a sorted sweep can,
        # so every permutation of a small group is checked rather than one
        # arrangement of a large one.
        group = [
            {
                "session_id": f"s{index}",
                "repo": "/repo",
                "started_at": started,
                "ended_at": ended,
                "contested": False,
                "bounds": conformance.BOUNDS_OBSERVED,
            }
            for index, (started, ended) in enumerate(self.WINDOWS[:5])
        ]
        for order in itertools.permutations(range(len(group))):
            arranged = [group[index] for index in order]
            expected = self._pairwise(arranged)

            for verdict in arranged:
                verdict["contested"] = False
            conformance._mark_overlaps(arranged)

            self.assertEqual(
                expected,
                [bool(verdict["contested"]) for verdict in arranged],
                f"order {order}",
            )

    def test_a_thousand_sequential_sessions_are_not_compared_pairwise(self) -> None:
        # The cost, not the answer. Sequential non-overlapping sessions were
        # the worst case for the pairwise version because nothing let it stop
        # early, so that is what is measured; the bound is loose enough to
        # survive a slow machine and far below the quadratic cost.
        base = 1788000000
        group = [
            {
                "session_id": f"s{index}",
                "repo": "/repo",
                "started_at": utc_from_epoch(base + index * 10),
                "ended_at": utc_from_epoch(base + index * 10 + 5),
                "contested": False,
                "bounds": conformance.BOUNDS_OBSERVED,
            }
            for index in range(1000)
        ]

        started = time.monotonic()
        conformance._mark_overlaps(group)
        swept = time.monotonic() - started

        started = time.monotonic()
        self._pairwise(group)
        pairwise = time.monotonic() - started

        self.assertEqual([], [v for v in group if v["contested"]])
        self.assertLess(
            swept, pairwise / 5, f"swept {swept:.3f}s pairwise {pairwise:.3f}s"
        )


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

    def test_the_broken_install_is_named_rather_than_left_to_an_errno(self) -> None:
        # The warning must not depend on the filesystem raising. A regular
        # file where the install root belongs raises NotADirectoryError on
        # POSIX and raises nothing on Windows, where the glob simply yields
        # nothing — so the same broken install warned on one platform and
        # looked healthy on the other, which is exactly what doctor exists
        # to prevent. The check now names the offending path itself.
        report = cli._journal_diagnostics()

        self.assertEqual(1, len(report["warnings"]))
        self.assertIn("is not a directory", report["warnings"][0])
        self.assertIn("graph-engineering-agent-harness", report["warnings"][0])

    def test_doctor_names_the_broken_journal_rather_than_raising(self) -> None:
        report = cli._journal_diagnostics()

        self.assertEqual(0, report["journal_bytes"])
        self.assertEqual([], report["journal_months"])
        self.assertTrue(report["warnings"])
