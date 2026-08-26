from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_harness import cli
from agent_harness.errors import HarnessError
from agent_harness.graph import Graph
from agent_harness.storage import GraphStore

from tests.helpers import graph, node

REPOSITORY = Path(__file__).resolve().parents[1]
GRAPHCTL = REPOSITORY / "scripts" / "graphctl.py"


class GraphStoreTests(unittest.TestCase):
    def test_round_trip_persists_graph_and_writes_without_temp_artifact(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as directory:
            path = Path(directory) / "task-graph.json"
            store = GraphStore(path)
            original = Graph.from_dict(graph(node("plan")))

            store.save(original)
            restored = store.load()

            self.assertEqual(original.to_dict(), restored.to_dict())
            self.assertTrue(path.exists())
            self.assertEqual([], list(Path(directory).glob("*.tmp")))


class GraphCtlAtomicityTests(unittest.TestCase):
    def setUp(self) -> None:
        # The CLI confines every path to the current directory, and the
        # in-process cases below build fixtures under the repository. Pin the
        # working directory so the suite does not depend on where it is run
        # from; CI happens to run from the root, which hid this.
        self._previous_directory = Path.cwd()
        os.chdir(REPOSITORY)
        self.addCleanup(os.chdir, self._previous_directory)

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(GRAPHCTL), *arguments],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=False,
        )

    def _write_graph(self, path: Path, payload: dict[str, Any]) -> None:
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_init_creates_ready_graph_and_refuses_to_overwrite_it(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as directory:
            path = Path(directory) / "new-graph.json"
            arguments = (
                "--graph",
                str(path),
                "init",
                "--objective",
                "Prepare the client brief",
                "--criterion",
                "facts are sourced",
                "--criterion",
                "recommendations are actionable",
            )

            created = self._run(*arguments)

            self.assertEqual(0, created.returncode, created.stderr)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("ready", saved["nodes"][0]["status"])
            self.assertEqual(
                ["facts are sourced", "recommendations are actionable"],
                saved["nodes"][0]["acceptance_criteria"],
            )
            before = path.read_text(encoding="utf-8")
            rejected = self._run(*arguments)
            self.assertEqual(2, rejected.returncode)
            self.assertIn("already exists", rejected.stderr)
            self.assertEqual(before, path.read_text(encoding="utf-8"))

    def test_init_does_not_overwrite_file_created_during_publish(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as directory:
            path = Path(directory) / "raced-graph.json"
            arguments = cli.build_parser().parse_args(
                [
                    "--graph",
                    str(path),
                    "init",
                    "--objective",
                    "Prepare a brief",
                    "--criterion",
                    "brief is complete",
                ]
            )

            def competing_create(source: str, destination: str) -> None:
                del source
                Path(destination).write_text("competitor data\n", encoding="utf-8")
                raise FileExistsError(destination)

            with mock.patch("agent_harness.cli.os.link", side_effect=competing_create):
                with self.assertRaisesRegex(HarnessError, "already exists"):
                    cli.run(arguments)

            self.assertEqual("competitor data\n", path.read_text(encoding="utf-8"))

    def test_doctor_reports_graph_and_lock_without_changing_either(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as directory:
            path = Path(directory) / "task-graph.json"
            lock_path = path.with_suffix(path.suffix + ".lock")
            self._write_graph(path, graph(node("plan")))
            lock_path.write_text("43210\n", encoding="utf-8")
            graph_before = path.read_text(encoding="utf-8")
            lock_before = lock_path.read_text(encoding="utf-8")

            diagnosed = self._run("--graph", str(path), "doctor")

            self.assertEqual(0, diagnosed.returncode, diagnosed.stderr)
            report = json.loads(diagnosed.stdout)
            self.assertTrue(report["graph_valid"])
            self.assertTrue(report["lock_present"])
            self.assertFalse(report["healthy"])
            self.assertEqual(str(lock_path), report["lock_path"])
            self.assertEqual(43210, report["lock_pid"])
            self.assertEqual(graph_before, path.read_text(encoding="utf-8"))
            self.assertEqual(lock_before, lock_path.read_text(encoding="utf-8"))

    def test_evidence_help_warns_about_persistent_and_shell_history(self) -> None:
        for command in ("submit", "verify"):
            help_result = self._run(command, "--help")

            self.assertEqual(0, help_result.returncode, help_result.stderr)
            self.assertIn("content persists", help_result.stdout)
            self.assertIn("in the graph", help_result.stdout)
            self.assertIn("shell history", help_result.stdout)

    def test_evidence_file_must_be_a_regular_file(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as directory:
            relative = Path(directory).relative_to(REPOSITORY)

            with self.assertRaisesRegex(HarnessError, "regular file"):
                cli._evidence(f"@{relative}")

    def test_happy_path_persists_each_cli_state_transition(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as directory:
            path = Path(directory) / "task-graph.json"
            self._write_graph(path, graph(node("plan")))

            self.assertEqual(
                0,
                self._run(
                    "--graph", str(path), "start", "plan", "--executor-id", "impl"
                ).returncode,
            )
            self.assertEqual(
                0,
                self._run(
                    "--graph",
                    str(path),
                    "submit",
                    "plan",
                    "--evidence",
                    "tests pass",
                    "--actor-id",
                    "impl",
                ).returncode,
            )
            completed = self._run(
                "--graph",
                str(path),
                "verify",
                "plan",
                "--pass",
                "--reviewer-id",
                "reviewer",
                "--criterion",
                "relevant tests pass",
                "--evidence",
                "reviewer reran tests successfully",
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIsNone(json.loads(completed.stdout)["faulty_node"])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("verified", saved["nodes"][0]["status"])
            self.assertEqual("reviewer", saved["nodes"][0]["reviewer_id"])

    def test_uncertain_cli_result_explains_and_supports_safe_resubmission(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as directory:
            path = Path(directory) / "task-graph.json"
            self._write_graph(path, graph(node("plan")))
            self.assertEqual(
                0,
                self._run(
                    "--graph", str(path), "start", "plan", "--executor-id", "impl"
                ).returncode,
            )
            self.assertEqual(
                0,
                self._run(
                    "--graph",
                    str(path),
                    "submit",
                    "plan",
                    "--evidence",
                    "initial evidence",
                    "--actor-id",
                    "impl",
                ).returncode,
            )
            uncertain = self._run(
                "--graph",
                str(path),
                "verify",
                "plan",
                "--uncertain",
                "--reviewer-id",
                "reviewer",
                "--reason",
                "need complete output",
            )

            self.assertEqual(0, uncertain.returncode, uncertain.stderr)
            self.assertIn("withdraw", json.loads(uncertain.stdout)["next_action"])
            withdrawn = self._run(
                "--graph",
                str(path),
                "withdraw",
                "plan",
                "--actor-id",
                "impl",
            )
            self.assertEqual(0, withdrawn.returncode, withdrawn.stderr)
            self.assertEqual("running", json.loads(withdrawn.stdout)["status"])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(1, saved["nodes"][0]["attempts"])
            self.assertIsNone(saved["nodes"][0].get("verification"))

    def test_failed_cli_command_does_not_partially_mutate_graph_file(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as directory:
            path = Path(directory) / "task-graph.json"
            original = graph(node("plan"))
            self._write_graph(path, original)

            failed = self._run(
                "--graph",
                str(path),
                "verify",
                "plan",
                "--pass",
                "--reviewer-id",
                "reviewer",
            )

            self.assertNotEqual(0, failed.returncode)
            self.assertEqual(original, json.loads(path.read_text(encoding="utf-8")))

    def test_graph_save_error_does_not_create_false_failure_record(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as directory:
            path = Path(directory) / "task-graph.json"
            task_graph = Graph.from_dict(graph(node("plan")))
            task_graph.start("plan", executor_id="impl")
            task_graph.submit(
                "plan",
                [{"kind": "test", "value": "fails"}],
                actor_id="impl",
            )
            GraphStore(path).save(task_graph)
            before = json.loads(path.read_text(encoding="utf-8"))
            arguments = cli.build_parser().parse_args(
                [
                    "--graph",
                    str(path),
                    "verify",
                    "plan",
                    "--fail",
                    "--reviewer-id",
                    "reviewer",
                    "--reason",
                    "bug",
                    "--failed-criterion",
                    "relevant tests pass",
                    "--evidence",
                    "reviewer reproduced the bug",
                ]
            )

            with mock.patch.object(
                GraphStore, "save", side_effect=OSError("disk full")
            ):
                with self.assertRaises(OSError):
                    cli.run(arguments)

            self.assertEqual(before, json.loads(path.read_text(encoding="utf-8")))

    def test_failure_query_reads_bounded_graph_resident_history(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as directory:
            path = Path(directory) / "task-graph.json"
            task_graph = Graph.from_dict(graph(node("plan")))
            task_graph.start("plan", executor_id="impl")
            task_graph.submit(
                "plan", [{"kind": "test", "value": "fails"}], actor_id="impl"
            )
            task_graph.verify(
                "plan",
                "fail",
                reviewer_id="reviewer",
                reason="bug",
                failed_criteria=["relevant tests pass"],
                review_evidence=[
                    {"kind": "test", "summary": "reviewer reproduced the bug"}
                ],
            )
            GraphStore(path).save(task_graph)

            result = self._run(
                "--graph", str(path), "failures", "--node", "plan", "--limit", "1"
            )

            self.assertEqual(0, result.returncode, result.stderr)
            failures = json.loads(result.stdout)["failures"]
            self.assertEqual(1, len(failures))
            self.assertEqual("bug", failures[0]["reason"])

    def test_cli_rejects_graph_path_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task-graph.json"
            self._write_graph(path, graph(node("plan")))

            rejected = self._run("--graph", str(path), "validate")

            self.assertEqual(2, rejected.returncode)
            self.assertIn("outside the workspace", rejected.stderr)
            self.assertIn("run the command from the directory", rejected.stderr)

    def test_parent_directory_graph_is_refused_with_working_directory_guidance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as directory:
            root = Path(directory)
            self._write_graph(root / "task-graph.json", graph(node("plan")))
            nested = root / "package"
            nested.mkdir()

            rejected = subprocess.run(
                [
                    sys.executable,
                    str(GRAPHCTL),
                    "--graph",
                    "../task-graph.json",
                    "ready",
                ],
                cwd=nested,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(2, rejected.returncode)
            self.assertIn("outside the workspace", rejected.stderr)
            self.assertIn("run the command from the directory", rejected.stderr)

    def test_missing_graph_names_the_recovery_commands(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as directory:
            missing = subprocess.run(
                [sys.executable, str(GRAPHCTL), "ready"],
                cwd=Path(directory),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(2, missing.returncode)
            self.assertIn("graph file not found", missing.stderr)
            self.assertIn("graphctl init", missing.stderr)


class GraphCtlAuthoringTests(unittest.TestCase):
    """The planner builds a multi-node graph through the CLI, as documented."""

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(GRAPHCTL), *arguments],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_add_node_builds_a_dag_and_rejects_a_bad_addition_atomically(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as directory:
            path = Path(directory) / "task-graph.json"
            created = self._run(
                "--graph",
                str(path),
                "init",
                "--objective",
                "Ship the parser",
                "--node",
                "plan",
                "--criterion",
                "the design is written down",
                "--description",
                "Agree the parser design",
                "--max-attempts",
                "4",
            )
            self.assertEqual(0, created.returncode, created.stderr)
            root = json.loads(path.read_text(encoding="utf-8"))["nodes"][0]
            self.assertEqual("Agree the parser design", root["description"])
            self.assertEqual(4, root["max_attempts"])

            added = self._run(
                "--graph",
                str(path),
                "add-node",
                "implement",
                "--description",
                "Write the parser",
                "--criterion",
                "the parser round-trips",
                "--depends-on",
                "plan",
                "--role",
                "specialist",
                "--max-attempts",
                "3",
            )

            self.assertEqual(0, added.returncode, added.stderr)
            self.assertEqual("blocked", json.loads(added.stdout)["status"])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(["plan", "implement"], [n["id"] for n in saved["nodes"]])
            self.assertEqual("specialist", saved["nodes"][1]["assigned_role"])
            self.assertEqual(3, saved["nodes"][1]["max_attempts"])

            before = path.read_text(encoding="utf-8")
            rejected = self._run(
                "--graph",
                str(path),
                "add-node",
                "implement",
                "--description",
                "Duplicate",
                "--criterion",
                "c",
            )

            self.assertEqual(2, rejected.returncode)
            self.assertIn("duplicate node id", rejected.stderr)
            self.assertEqual(before, path.read_text(encoding="utf-8"))
