"""Regressions for GH-1: invalid input and retired-node stability."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_harness import Graph, GraphStore, HarnessError
from tests import workspace_root
from tests.helpers import graph, node

REPOSITORY = Path(__file__).resolve().parents[1]
GRAPHCTL = REPOSITORY / "scripts" / "graphctl.py"
NON_STRING_VALUES: tuple[Any, ...] = (None, False, 12, [], {})


class MalformedGraphTests(unittest.TestCase):
    def test_non_string_status_is_a_validation_error(self) -> None:
        for status in NON_STRING_VALUES:
            with self.subTest(status=status):
                document = graph(node("work"))
                document["nodes"][0]["status"] = status
                with self.assertRaisesRegex(HarnessError, "work.*invalid status"):
                    Graph.from_dict(document)

    def test_failure_history_entries_must_be_objects(self) -> None:
        for entry in (*NON_STRING_VALUES, "bad"):
            with self.subTest(entry=entry):
                document = graph(node("work"))
                document["nodes"][0]["failure_history"] = [entry]
                with self.assertRaisesRegex(HarnessError, "work.*failure_history"):
                    Graph.from_dict(document)

    def test_readers_report_malformed_graph_without_tracebacks_or_writes(self) -> None:
        with tempfile.TemporaryDirectory(dir=workspace_root()) as directory:
            root = Path(directory)
            path = root / "task-graph.json"
            for field, value in (("status", []), ("failure_history", [None])):
                document = graph(node("work"))
                document["nodes"][0][field] = value
                original = json.dumps(document)
                path.write_text(original, encoding="utf-8")
                with self.subTest(field=field, reader="store"):
                    with self.assertRaises(HarnessError):
                        GraphStore(path).load()
                for command in (
                    "validate",
                    "status",
                    "failures",
                    "completion-check",
                    "doctor",
                ):
                    with self.subTest(field=field, reader=command):
                        result = subprocess.run(
                            [sys.executable, str(GRAPHCTL), command],
                            cwd=root,
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        self.assertNotIn("Traceback", result.stderr)
                        if command == "doctor":
                            self.assertEqual(0, result.returncode, result.stderr)
                            report = json.loads(result.stdout)
                            self.assertFalse(report["graph_valid"])
                            self.assertIn(
                                field.replace("status", "invalid status"),
                                report["graph_error"],
                            )
                        else:
                            self.assertEqual(2, result.returncode, result.stderr)
                            self.assertIn(
                                field.replace("status", "invalid status"), result.stderr
                            )
                        self.assertEqual(original, path.read_text(encoding="utf-8"))
                with self.subTest(field=field, reader="stop-hook"):
                    hook = subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            "from agent_harness.claude_hook import main; raise SystemExit(main())",
                        ],
                        cwd=REPOSITORY,
                        env={**os.environ, "CLAUDE_PROJECT_DIR": str(root)},
                        input="{}",
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(2, hook.returncode, hook.stderr)
                    self.assertNotIn("Traceback", hook.stderr)
                    self.assertIn("Graph completion is not verified", hook.stderr)
                    self.assertEqual(original, path.read_text(encoding="utf-8"))


class EvidenceInputTests(unittest.TestCase):
    def _run(self, root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(GRAPHCTL), *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_submit_and_verify_reject_non_string_evidence_atomically(self) -> None:
        with tempfile.TemporaryDirectory(dir=workspace_root()) as directory:
            root = Path(directory)
            path = root / "task-graph.json"
            task_graph = Graph.from_dict(graph(node("work")))
            # A null criterion used to become the string 'None' and match.
            task_graph.node("work")["acceptance_criteria"] = ["None"]
            task_graph.start("work", "executor")
            running = task_graph.to_dict()
            task_graph.submit(
                "work", [{"kind": "test", "summary": "pass"}], actor_id="executor"
            )
            awaiting = task_graph.to_dict()
            for command, document, arguments in (
                ("submit", running, ("--actor-id", "executor")),
                (
                    "verify",
                    awaiting,
                    ("--pass", "--reviewer-id", "reviewer", "--criterion", "None"),
                ),
            ):
                for field in ("criterion", "kind", "summary", "value"):
                    for value in NON_STRING_VALUES:
                        for source in ("inline", "file"):
                            with self.subTest(
                                command=command, field=field, value=value, source=source
                            ):
                                payload: dict[str, Any] = {
                                    "criterion": "None",
                                    "kind": "test",
                                    "summary": "pass",
                                }
                                if field == "value":
                                    del payload["summary"]
                                payload[field] = value
                                rendered = json.dumps(payload)
                                evidence = rendered
                                if source == "file":
                                    (root / "evidence.json").write_text(
                                        rendered, encoding="utf-8"
                                    )
                                    evidence = "@evidence.json"
                                original = json.dumps(document)
                                path.write_text(original, encoding="utf-8")
                                result = self._run(
                                    root,
                                    command,
                                    "work",
                                    *arguments,
                                    "--evidence",
                                    evidence,
                                )
                                self.assertEqual(2, result.returncode, result.stderr)
                                self.assertIn("requires non-empty", result.stderr)
                                self.assertNotIn("Traceback", result.stderr)
                                self.assertEqual(
                                    original, path.read_text(encoding="utf-8")
                                )

    def test_json_metadata_and_legacy_string_value_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory(dir=workspace_root()) as directory:
            root = Path(directory)
            path = root / "task-graph.json"
            task_graph = Graph.from_dict(graph(node("work")))
            task_graph.start("work", "executor")
            GraphStore(path).save(task_graph)
            payload = {
                "kind": "test",
                "value": "legacy pass",
                "metadata": {"count": 2, "detail": None},
            }
            result = self._run(
                root,
                "submit",
                "work",
                "--actor-id",
                "executor",
                "--evidence",
                json.dumps(payload),
            )
            self.assertEqual(0, result.returncode, result.stderr)
            saved = GraphStore(path).load().node("work")["evidence"][0]
            self.assertEqual("legacy pass", saved["summary"])
            self.assertEqual("relevant tests pass", saved["criterion"])
            self.assertEqual(payload["metadata"], saved["metadata"])


class SupersededInvalidationTests(unittest.TestCase):
    def test_upstream_failure_preserves_retirement_but_reaches_its_descendants(
        self,
    ) -> None:
        task_graph = Graph.from_dict(
            graph(
                node("up", status="verified"),
                node("retired", depends_on=["up"]),
                node("child", depends_on=["retired"]),
                node("probe", depends_on=["up"]),
                node("unrelated", status="verified"),
            )
        )
        task_graph.start("retired", "executor")
        task_graph.submit(
            "retired", [{"kind": "test", "summary": "failed"}], actor_id="executor"
        )
        task_graph.verify(
            "retired",
            "fail",
            "reviewer",
            reason="abandon approach",
            failed_criteria=["relevant tests pass"],
            review_evidence=[{"kind": "test", "summary": "reproduced"}],
        )
        task_graph.supersede("retired", reason="human chose another approach")
        retired = task_graph.to_dict()["nodes"][1]
        unrelated = task_graph.to_dict()["nodes"][-1]
        task_graph.retry("child")
        task_graph.start("child", "child-executor")
        task_graph.start("probe", "probe-executor")
        task_graph.submit(
            "probe",
            [{"kind": "test", "summary": "upstream fault"}],
            actor_id="probe-executor",
        )
        invalidated = task_graph.verify(
            "probe",
            "fail",
            "probe-reviewer",
            reason="upstream regression",
            faulty_node="up",
            failed_criteria=["relevant tests pass"],
            faulty_criteria=["relevant tests pass"],
            review_evidence=[{"kind": "test", "summary": "traced upstream"}],
        )

        self.assertEqual(["probe", "child"], invalidated)
        self.assertEqual(retired, task_graph.node("retired"))
        self.assertEqual(unrelated, task_graph.node("unrelated"))
        self.assertEqual("invalidated", task_graph.node("child")["status"])
        self.assertEqual(0, task_graph.node("child")["attempts"])
        self.assertEqual(
            invalidated,
            task_graph.node("up")["failure_history"][-1]["affected_downstream_nodes"],
        )
        with self.assertRaisesRegex(
            HarnessError, "cannot retry retired from superseded"
        ):
            task_graph.retry("retired")
        self.assertEqual(retired, Graph.from_dict(task_graph.to_dict()).node("retired"))
