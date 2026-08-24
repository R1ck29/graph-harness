"""Executable contracts for the documented one-node quick start."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
GRAPHCTL = REPOSITORY / "scripts" / "graphctl.py"
QUICKSTART = REPOSITORY / "examples" / "quickstart"
CRITERION = (
    "the deliverable contains a title, intended audience, and next step "
    "without customer data"
)


class QuickStartContractTests(unittest.TestCase):
    def _run(
        self, workspace: Path, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(GRAPHCTL), *arguments],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_packaged_entry_point_and_repository_script_share_the_same_cli(
        self,
    ) -> None:
        project = (REPOSITORY / "pyproject.toml").read_text(encoding="utf-8")
        script = GRAPHCTL.read_text(encoding="utf-8")

        self.assertIn('graphctl = "agent_harness.cli:main"', project)
        self.assertIn("from agent_harness.cli import main", script)

    def test_source_cli_completes_the_documented_quickstart(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as directory:
            workspace = Path(directory)
            copied_examples = workspace / "examples" / "quickstart"
            copied_examples.parent.mkdir()
            shutil.copytree(QUICKSTART, copied_examples)
            shutil.copy2(
                copied_examples / "task-graph.json", workspace / "task-graph.json"
            )

            validate = self._run(workspace, "validate")
            ready = self._run(workspace, "ready")
            start = self._run(
                workspace, "start", "deliverable", "--executor-id", "executor-1"
            )
            submit = self._run(
                workspace,
                "submit",
                "deliverable",
                "--actor-id",
                "executor-1",
                "--evidence",
                "@examples/quickstart/submission-evidence.json",
                "--relevant-file",
                "examples/quickstart/deliverable.txt",
            )
            packet = self._run(workspace, "review-packet", "deliverable")
            verify = self._run(
                workspace,
                "verify",
                "deliverable",
                "--pass",
                "--reviewer-id",
                "reviewer-1",
                "--criterion",
                CRITERION,
                "--evidence",
                "@examples/quickstart/review-evidence.json",
            )
            complete = self._run(workspace, "completion-check")

            for result in (validate, ready, start, submit, packet, verify, complete):
                self.assertEqual(0, result.returncode, result.stderr)

            self.assertEqual({"valid": True, "nodes": 1}, json.loads(validate.stdout))
            self.assertEqual({"ready": ["deliverable"]}, json.loads(ready.stdout))
            self.assertEqual(
                "awaiting_verification", json.loads(submit.stdout)["status"]
            )
            self.assertEqual(
                CRITERION, json.loads(packet.stdout)["acceptance_criteria"][0]
            )
            self.assertEqual("PASS", json.loads(verify.stdout)["result"])
            self.assertEqual({"complete": True}, json.loads(complete.stdout))

            saved = json.loads(
                (workspace / "task-graph.json").read_text(encoding="utf-8")
            )
            node = saved["nodes"][0]
            self.assertEqual("verified", node["status"])
            self.assertEqual("executor-1", node["executor_id"])
            self.assertEqual("reviewer-1", node["verification"]["reviewer_id"])
            self.assertEqual(
                node["acceptance_criteria"], node["verification"]["checked_criteria"]
            )
            self.assertNotEqual(node["evidence"], node["verification"]["evidence"])
