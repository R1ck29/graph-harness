from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_harness import cli
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
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIsNone(json.loads(completed.stdout)["faulty_node"])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("verified", saved["nodes"][0]["status"])
            self.assertEqual("reviewer", saved["nodes"][0]["reviewer_id"])

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
