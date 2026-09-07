from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast
from unittest import mock

from agent_harness.evals import load_cases, run_suite
from agent_harness.errors import HarnessError
from agent_harness.paths import is_link_like
from tests import workspace_root

REPOSITORY = Path(__file__).resolve().parents[1]
SYNC = REPOSITORY / "scripts" / "sync_adapters.py"
EVAL_RUNNER = REPOSITORY / "evals" / "runner" / "run.py"
CLAUDE_SETTINGS = REPOSITORY / "adapters" / "claude" / "settings.example.json"
CLAUDE_GUARD = REPOSITORY / "adapters" / "claude" / "hooks" / "graph_guard.py"
CODEX_REVIEWER = REPOSITORY / ".codex" / "agents" / "reviewer.toml"


def _load_graph_guard() -> ModuleType:
    """Import the Stop hook as a module so its checks can be called directly."""

    specification = importlib.util.spec_from_file_location(
        "graph_guard_under_test", CLAUDE_GUARD
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _create_windows_junction(link: Path, target: Path) -> None:
    """Create a real NTFS junction or skip when the host cannot provide one."""

    if os.name != "nt":
        raise unittest.SkipTest("NTFS junction regression test requires Windows")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip()
        raise unittest.SkipTest(f"junction creation unavailable: {detail}")


class EvaluationFixtureTests(unittest.TestCase):
    def test_mount_point_reparse_tag_is_detected_without_path_is_junction(self) -> None:
        reparse_status = SimpleNamespace(
            st_reparse_tag=0xA0000003,
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
        )
        with mock.patch.object(Path, "is_symlink", return_value=False):
            with mock.patch.object(Path, "lstat", return_value=reparse_status):
                self.assertTrue(is_link_like(Path("junction")))

    def test_cloud_reparse_tag_is_not_treated_as_a_junction(self) -> None:
        reparse_status = SimpleNamespace(
            st_reparse_tag=0x9000001A,
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
        )
        with mock.patch.object(Path, "is_symlink", return_value=False):
            with mock.patch.object(Path, "lstat", return_value=reparse_status):
                self.assertFalse(is_link_like(Path("cloud-placeholder")))

    def test_reparse_attribute_is_fail_closed_when_tag_is_unavailable(self) -> None:
        reparse_status = SimpleNamespace(
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT
        )
        with mock.patch.object(Path, "is_symlink", return_value=False):
            with mock.patch.object(Path, "lstat", return_value=reparse_status):
                self.assertTrue(is_link_like(Path("untagged-reparse-point")))

    def test_five_named_evaluation_cases_load_with_required_objectives(self) -> None:
        cases = load_cases(REPOSITORY / "evals" / "cases")

        self.assertGreaterEqual(len(cases), 5)
        expected = {
            "simple-bug-fix",
            "multi-file-change",
            "test-failure-repair",
            "regression-risk",
            "upstream-invalidates-descendants",
        }
        self.assertTrue(expected.issubset({case["id"] for case in cases}))
        for case in cases:
            self.assertTrue(case["objective"])
            self.assertTrue(case["task_graph"])

    def test_offline_suite_executes_invalidation_and_does_not_fabricate_model_metrics(
        self,
    ) -> None:
        result = run_suite(REPOSITORY / "evals" / "cases")

        self.assertEqual(5, result["case_count"])
        for case in result["results"]:
            self.assertIsNone(case["task_success"])
            self.assertIsNone(case["reviewer_detection_rate"])
            self.assertTrue(all(case["protocol_checks"].values()))

    def test_eval_loader_rejects_unknown_failure_node(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = json.loads(
                (REPOSITORY / "evals" / "cases" / "simple-bug-fix.json").read_text(
                    encoding="utf-8"
                )
            )
            payload["failure_node"] = "missing"
            Path(directory, "invalid.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )

            with self.assertRaises(HarnessError):
                load_cases(directory)

    def test_eval_runner_rejects_output_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"

            result = subprocess.run(
                [sys.executable, str(EVAL_RUNNER), "--output", str(output)],
                cwd=REPOSITORY,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(2, result.returncode)
            self.assertFalse(output.exists())

    def test_eval_runner_rejects_symlink_output(self) -> None:
        with tempfile.TemporaryDirectory(dir=workspace_root()) as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_text("preserve me", encoding="utf-8")
            output = root / "result.json"
            try:
                output.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")

            result = subprocess.run(
                [sys.executable, str(EVAL_RUNNER), "--output", str(output)],
                cwd=REPOSITORY,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(2, result.returncode)
            self.assertEqual("preserve me", target.read_text(encoding="utf-8"))

    def test_eval_runner_rejects_output_through_windows_junction(self) -> None:
        with tempfile.TemporaryDirectory(dir=workspace_root()) as workspace_directory:
            with tempfile.TemporaryDirectory() as outside_directory:
                junction = Path(workspace_directory) / "junction"
                outside = Path(outside_directory)
                _create_windows_junction(junction, outside)
                try:
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(EVAL_RUNNER),
                            "--output",
                            str(junction / "result.json"),
                        ],
                        cwd=REPOSITORY,
                        capture_output=True,
                        text=True,
                        check=False,
                    )

                    self.assertEqual(2, result.returncode)
                    self.assertFalse((outside / "result.json").exists())
                finally:
                    os.rmdir(junction)


class AdapterSyncTests(unittest.TestCase):
    def _run(self, root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SYNC), "--root", str(root), *arguments],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_check_detects_generated_adapter_drift_without_rewriting_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skills = root / "skills"
            adapter = root / "adapters" / "codex" / "skills"
            skills.mkdir(parents=True)
            adapter.mkdir(parents=True)
            source = skills / "review.md"
            generated = adapter / "review.md"
            source.write_text("# Review\nCanonical instructions\n", encoding="utf-8")
            generated.write_text("# Review\nOutdated instructions\n", encoding="utf-8")
            before = generated.read_text(encoding="utf-8")

            result = self._run(root, "--check")

            self.assertNotEqual(0, result.returncode)
            self.assertIn("drift", (result.stdout + result.stderr).lower())
            self.assertEqual(before, generated.read_text(encoding="utf-8"))

    def test_check_detects_stale_generated_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "skills" / "review.md"
            source.parent.mkdir(parents=True)
            source.write_text("canonical\n", encoding="utf-8")
            for relative in (
                ".agents/skills",
                ".claude/skills",
                "adapters/codex/skills",
                "adapters/claude/skills",
            ):
                target = root / relative
                target.mkdir(parents=True)
                (target / "review.md").write_text("canonical\n", encoding="utf-8")
            stale = root / "adapters" / "codex" / "skills" / "stale.md"
            stale.write_text("obsolete\n", encoding="utf-8")

            result = self._run(root, "--check")

            self.assertNotEqual(0, result.returncode)
            self.assertIn("stale.md", result.stderr)
            self.assertTrue(stale.exists())

    def test_editor_bookkeeping_files_are_neither_generated_nor_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "skills" / "review.md"
            source.parent.mkdir(parents=True)
            source.write_text("canonical\n", encoding="utf-8")
            (root / "skills" / ".DS_Store").write_bytes(b"BUD1 editor state")

            generate = self._run(root)
            self.assertEqual(0, generate.returncode, generate.stderr)
            self.assertNotIn(".DS_Store", generate.stdout)

            for relative in (
                ".agents/skills",
                ".claude/skills",
                "adapters/codex/skills",
                "adapters/claude/skills",
            ):
                self.assertTrue((root / relative / "review.md").exists())
                self.assertFalse((root / relative / ".DS_Store").exists())

            confirm = self._run(root, "--check")
            self.assertEqual(0, confirm.returncode, confirm.stderr)

    def test_hidden_generated_file_is_not_reported_as_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "skills" / "review.md"
            source.parent.mkdir(parents=True)
            source.write_text("canonical\n", encoding="utf-8")
            for relative in (
                ".agents/skills",
                ".claude/skills",
                "adapters/codex/skills",
                "adapters/claude/skills",
            ):
                target = root / relative
                target.mkdir(parents=True)
                (target / "review.md").write_text("canonical\n", encoding="utf-8")
            leftover = root / ".claude" / "skills" / ".DS_Store"
            leftover.write_bytes(b"BUD1 editor state")

            result = self._run(root, "--check")

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(leftover.exists())

    def test_sync_removes_stale_targets_when_canonical_tree_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "skills").mkdir()
            stale = root / ".agents" / "skills" / "stale.md"
            stale.parent.mkdir(parents=True)
            stale.write_text("obsolete\n", encoding="utf-8")

            result = self._run(root)

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse(stale.exists())

    def test_sync_rejects_symlinked_canonical_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "skills"
            source_root.mkdir()
            outside = root / "outside.md"
            outside.write_text("local secret\n", encoding="utf-8")
            try:
                (source_root / "linked.md").symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")

            result = self._run(root, "--check")

            self.assertEqual(2, result.returncode)
            self.assertIn("symlink", result.stderr.lower())

    def test_sync_rejects_symlinked_generated_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "skills" / "review.md"
            source.parent.mkdir(parents=True)
            source.write_text("canonical\n", encoding="utf-8")
            outside = root / "outside"
            outside.mkdir()
            target_parent = root / "adapters" / "codex"
            target_parent.mkdir(parents=True)
            try:
                (target_parent / "skills").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")

            result = self._run(root)

            self.assertEqual(2, result.returncode)
            self.assertIn("symlink", result.stderr.lower())

    def test_sync_rejects_junctioned_canonical_tree_on_windows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            (outside / "linked.md").write_text("local secret\n", encoding="utf-8")
            junction = root / "skills"
            _create_windows_junction(junction, outside)
            try:
                result = self._run(root, "--check")

                self.assertEqual(2, result.returncode)
                self.assertIn("link-like", result.stderr.lower())
            finally:
                os.rmdir(junction)


class CodexReviewerAdapterTests(unittest.TestCase):
    """The Codex reviewer config had no coverage; a rename or typo passed CI."""

    def _load(self) -> dict[str, object]:
        try:
            # 3.10 has no tomllib at all; 3.11+ ships it without a py.typed
            # marker, so the two interpreters raise different codes here.
            import tomllib  # type: ignore[import-not-found,import-untyped,unused-ignore]
        except ImportError as exc:  # Python 3.10 has no tomllib.
            raise unittest.SkipTest(f"tomllib requires Python 3.11: {exc}")
        parsed = tomllib.loads(CODEX_REVIEWER.read_text(encoding="utf-8"))
        return cast("dict[str, object]", parsed)

    def test_reviewer_declares_a_read_only_sandbox(self) -> None:
        config = self._load()

        self.assertEqual("graph_reviewer", config["name"])
        self.assertEqual("read-only", config["sandbox_mode"])

    def test_reviewer_is_told_not_to_record_its_own_verdict(self) -> None:
        instructions = str(self._load()["developer_instructions"])

        self.assertIn("PASS, FAIL, or UNCERTAIN", instructions)
        self.assertIn("do not run graphctl verify yourself", instructions)
        self.assertIn("task-graph.json", instructions)


class ClaudeHookPortabilityTests(unittest.TestCase):
    def test_example_uses_the_project_selected_python_command(self) -> None:
        settings = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
        command = settings["hooks"]["Stop"][0]["hooks"][0]["command"]

        self.assertTrue(command.startswith("python "))
        self.assertNotIn("python3", command)

    def test_example_declares_one_stop_hook_command_with_a_timeout(self) -> None:
        settings = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))

        self.assertEqual(["Stop"], list(settings["hooks"]))
        matchers = settings["hooks"]["Stop"]
        self.assertIsInstance(matchers, list)
        self.assertEqual(1, len(matchers))
        entries = matchers[0]["hooks"]
        self.assertEqual(1, len(entries))
        self.assertEqual("command", entries[0]["type"])
        self.assertIn("${CLAUDE_PROJECT_DIR}", entries[0]["command"])
        self.assertIn("graph_guard.py", entries[0]["command"])
        self.assertIsInstance(entries[0]["timeout"], int)
        self.assertGreater(entries[0]["timeout"], 0)

    def test_guard_refuses_an_interpreter_below_the_supported_release(self) -> None:
        guard = _load_graph_guard()

        with mock.patch.object(sys, "stderr", new=io.StringIO()) as captured:
            with self.assertRaises(SystemExit) as raised:
                guard.ensure_supported_python((3, 8, 5))

        self.assertEqual(2, raised.exception.code)
        message = captured.getvalue()
        self.assertIn("3.8.5", message)
        self.assertIn("3.10", message)
        self.assertIn(".venv/bin/python", message)

    def test_guard_accepts_the_minimum_and_later_supported_releases(self) -> None:
        guard = _load_graph_guard()

        self.assertEqual((3, 10), guard.MINIMUM_PYTHON)
        # (3, 10) exercises the equality boundary itself; a 3-tuple such as
        # (3, 10, 0) already sorts above the 2-tuple minimum and would keep
        # passing if the comparison were tightened to a strict `>`.
        for version in ((3, 10), (3, 10, 0), (3, 13, 2), (4, 0, 0)):
            with self.subTest(version=version):
                self.assertIsNone(guard.ensure_supported_python(version))

    def _run_guard_as_main(
        self, faked_version: str, cwd: Path
    ) -> "subprocess.CompletedProcess[str]":
        """Execute the hook the way Claude Code does, as `__main__`.

        Calling `ensure_supported_python` directly cannot show that the check is
        still wired into the script, so deleting the call site would otherwise
        leave the suite green.
        """

        program = (
            "import sys\n"
            f"sys.version_info = {faked_version}\n"
            f"exec(open({str(CLAUDE_GUARD)!r}).read())\n"
        )
        # Point the guard at an empty project and put the harness on the import
        # path explicitly, so the result does not depend on whether this
        # interpreter happens to have the package installed.
        environment = dict(os.environ)
        environment["CLAUDE_PROJECT_DIR"] = str(cwd)
        environment["PYTHONPATH"] = str(REPOSITORY)
        return subprocess.run(
            [sys.executable, "-c", program],
            cwd=cwd,
            env=environment,
            input="{}",
            capture_output=True,
            text=True,
            check=False,
        )

    def test_running_the_hook_on_an_old_interpreter_refuses_before_any_check(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._run_guard_as_main("(3, 8, 5)", Path(directory))

        self.assertEqual(2, result.returncode)
        self.assertIn("3.8.5", result.stderr)
        self.assertIn(".venv/bin/python", result.stderr)

    def test_running_the_hook_on_a_supported_interpreter_reaches_the_graph_check(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            # No graph in this directory, so a guard that gets past the version
            # check must exit 0 rather than refusing.
            result = self._run_guard_as_main("(3, 13, 2)", Path(directory))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr.strip())

    def test_guard_refuses_the_release_just_below_the_minimum(self) -> None:
        guard = _load_graph_guard()

        with mock.patch.object(sys, "stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                guard.ensure_supported_python((3, 9, 18))

        self.assertEqual(2, raised.exception.code)
