from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from scripts import install_pc as pc_installer

REPOSITORY = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY / "scripts" / "install_pc.py"
SKILL_NAMES = (
    "graph-planning",
    "graph-execution",
    "independent-verification",
    "selective-recovery",
)
MANAGED_BEGIN = "<!-- BEGIN graph-engineering-agent-harness -->"
MANAGED_END = "<!-- END graph-engineering-agent-harness -->"


class PcInstallContractTests(unittest.TestCase):
    """Black-box contract for a user-scoped Codex and Claude Code install."""

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.home = Path(self._temporary_directory.name) / "home"
        self.home.mkdir()
        self.codex = self.home / ".codex"
        self.codex_skills = self.home / ".agents"
        self.claude = self.home / ".claude"
        self.runtime_bin = self.home / "runtime-bin"
        self.runtime_bin.mkdir()
        suffix = ".exe" if os.name == "nt" else ""
        self.graphctl = self._fake_executable(f"graphctl{suffix}")
        self.stop_guard = self._fake_executable(f"graphctl-claude-stop{suffix}")
        self.session_start = self._fake_executable(f"graphctl-session-start{suffix}")
        self.session_end = self._fake_executable(f"graphctl-session-end{suffix}")
        self.edit_hook = self._fake_executable(f"graphctl-edit{suffix}")

    def _fake_executable(self, name: str) -> Path:
        executable = self.runtime_bin / name
        executable.write_text(
            "#!/usr/bin/env python3\n" "import sys\n" "raise SystemExit(0)\n",
            encoding="utf-8",
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        return executable

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(INSTALLER),
                "--home",
                str(self.home),
                "--runtime-bin-dir",
                str(self.runtime_bin),
                *arguments,
            ],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=False,
        )

    def _run_with_managed_runtime(
        self, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(INSTALLER),
                "--home",
                str(self.home),
                *arguments,
            ],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=False,
        )

    def _install(self) -> subprocess.CompletedProcess[str]:
        result = self._run()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result

    def _settings(self) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            json.loads((self.claude / "settings.json").read_text(encoding="utf-8")),
        )

    @staticmethod
    def _managed_stop_commands(settings: dict[str, Any]) -> list[str]:
        commands: list[str] = []
        for matcher in settings.get("hooks", {}).get("Stop", []):
            for hook in matcher.get("hooks", []):
                command = hook.get("command")
                if isinstance(command, str) and Path(command).name in {
                    "graphctl-claude-stop",
                    "graphctl-claude-stop.exe",
                }:
                    commands.append(command)
        return commands

    def _codex_hooks(self) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            json.loads((self.codex / "hooks.json").read_text(encoding="utf-8")),
        )

    @staticmethod
    def _managed_events(document: dict[str, Any]) -> dict[str, list[str]]:
        """Return the managed command installed under each hook event."""

        managed: dict[str, list[str]] = {}
        for event, matchers in document.get("hooks", {}).items():
            for matcher in matchers if isinstance(matchers, list) else []:
                for hook in (
                    matcher.get("hooks", []) if isinstance(matcher, dict) else []
                ):
                    command = hook.get("command")
                    if isinstance(command, str) and Path(command).stem in {
                        "graphctl-claude-stop",
                        "graphctl-session-start",
                        "graphctl-session-end",
                        "graphctl-edit",
                    }:
                        managed.setdefault(event, []).append(command)
        return managed

    def test_every_managed_event_is_installed_on_both_clients(self) -> None:
        self._install()

        for document in (self._settings(), self._codex_hooks()):
            managed = self._managed_events(document)
            self.assertEqual(
                {"SessionStart", "Stop", "SessionEnd", "PostToolUse"},
                set(managed),
                document,
            )
            self.assertEqual([str(self.session_start)], managed["SessionStart"])
            self.assertEqual([str(self.stop_guard)], managed["Stop"])
            self.assertEqual([str(self.session_end)], managed["SessionEnd"])
            self.assertEqual([str(self.edit_hook)], managed["PostToolUse"])

    def test_the_edit_hook_carries_the_matcher_that_selects_editing_tools(
        self,
    ) -> None:
        # Without a matcher the hook would run after every tool call, and the
        # producer would pay its cost on reads and searches too.
        self._install()

        for document in (self._settings(), self._codex_hooks()):
            entries = [
                matcher
                for matcher in document["hooks"]["PostToolUse"]
                if str(self.edit_hook) in json.dumps(matcher)
            ]
            self.assertEqual(1, len(entries), document)
            self.assertEqual(
                "Edit|Write|MultiEdit|NotebookEdit", entries[0].get("matcher")
            )

    def test_an_unrelated_claude_post_tool_use_entry_survives(self) -> None:
        # This event was unmanaged until now, and a real machine already has a
        # third-party entry on it.
        self.claude.mkdir(parents=True, exist_ok=True)
        original = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Edit|Write",
                        "hooks": [{"type": "command", "command": "someone-elses-tool"}],
                    }
                ]
            }
        }
        (self.claude / "settings.json").write_text(
            json.dumps(original, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        self._install()

        installed = self._settings()
        self.assertIn(
            original["hooks"]["PostToolUse"][0], installed["hooks"]["PostToolUse"]
        )

        self._run("--uninstall")

        self.assertEqual(original, self._settings())

    def _edit_managed_entry(self, **changes: Any) -> None:
        """Modify our own PostToolUse entry the way a curious user would."""

        path = self.claude / "settings.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        for matcher in document["hooks"]["PostToolUse"]:
            for hook in matcher.get("hooks", []):
                if Path(str(hook.get("command"))).stem == "graphctl-edit":
                    hook.update(changes)
        path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def _managed_edit_entries(self) -> list[Any]:
        return [
            hook
            for matcher in self._settings()["hooks"].get("PostToolUse", [])
            for hook in matcher.get("hooks", [])
            if Path(str(hook.get("command"))).stem == "graphctl-edit"
        ]

    def test_a_user_edited_managed_entry_is_replaced_not_duplicated(self) -> None:
        # Recognition keyed on the timeout and the args as well as the
        # command, so raising the timeout made our own entry unrecognisable
        # and the next install appended a second one. PostToolUse fires on
        # every edit, so the duplicate is paid on every edit.
        self._install()

        for change in (
            {"timeout": 30},
            {"args": ["--verbose"]},
            {"timeout": 5, "args": ["--x"]},
            {"note": "left by a person"},
        ):
            with self.subTest(change=change):
                self._edit_managed_entry(**change)
                self._install()

                self.assertEqual(1, len(self._managed_edit_entries()), change)

    def test_a_user_edited_managed_entry_is_reported_as_drift(self) -> None:
        self._install()
        self._edit_managed_entry(timeout=30)

        drifted = self._run("--check")

        self.assertEqual(2, drifted.returncode)
        self.assertIn("hook drift", drifted.stderr)

    def test_a_third_party_entry_sharing_our_matcher_survives(self) -> None:
        self.claude.mkdir(parents=True, exist_ok=True)
        foreign = {
            "matcher": "Edit|Write|MultiEdit|NotebookEdit",
            "hooks": [{"type": "command", "command": "someone-elses-tool"}],
        }
        (self.claude / "settings.json").write_text(
            json.dumps({"hooks": {"PostToolUse": [foreign]}}, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

        self._install()
        self._edit_managed_entry(timeout=30)
        self._install()

        entries = self._settings()["hooks"]["PostToolUse"]
        self.assertIn("someone-elses-tool", json.dumps(entries))
        self.assertEqual(1, len(self._managed_edit_entries()))

        self._run("--uninstall")

        self.assertEqual({"hooks": {"PostToolUse": [foreign]}}, self._settings())

    def test_every_managed_event_holds_one_entry_after_an_edit_and_reinstall(
        self,
    ) -> None:
        self._install()
        self._edit_managed_entry(timeout=30)
        self._install()

        for document in (self._settings(), self._codex_hooks()):
            for event, commands in self._managed_events(document).items():
                self.assertEqual(1, len(commands), f"{event}: {commands}")

    def test_check_reports_drift_for_a_removed_edit_hook(self) -> None:
        self._install()
        document = self._settings()
        document["hooks"].pop("PostToolUse")
        (self.claude / "settings.json").write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        drifted = self._run("--check")

        self.assertEqual(2, drifted.returncode)
        self.assertIn("hook drift", drifted.stderr)
        self.assertEqual(document, self._settings())

    def test_a_repeated_install_adds_no_second_entry_to_any_event(self) -> None:
        self._install()
        self._install()

        for document in (self._settings(), self._codex_hooks()):
            for event, commands in self._managed_events(document).items():
                self.assertEqual(1, len(commands), f"{event}: {commands}")

    def test_unrelated_codex_hooks_survive_install_and_uninstall(self) -> None:
        self.codex.mkdir(parents=True, exist_ok=True)
        original: dict[str, dict[str, Any]] = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Edit|Write",
                        "hooks": [{"type": "command", "command": "someone-elses-tool"}],
                    }
                ],
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "echo graphctl-claude-stop is user-owned",
                            }
                        ]
                    }
                ],
            }
        }
        (self.codex / "hooks.json").write_text(
            json.dumps(original, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        self._install()
        installed = self._codex_hooks()
        # PostToolUse is a managed event now, so the third-party entry is kept
        # beside ours rather than being the only one.
        self.assertIn(
            original["hooks"]["PostToolUse"][0], installed["hooks"]["PostToolUse"]
        )
        self.assertIn(original["hooks"]["Stop"][0], installed["hooks"]["Stop"])

        self._run("--uninstall")

        self.assertEqual(original, self._codex_hooks())

    def test_check_reports_drift_for_a_removed_event(self) -> None:
        self._install()
        document = self._codex_hooks()
        document["hooks"].pop("SessionStart")
        (self.codex / "hooks.json").write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        drifted = self._run("--check")

        self.assertEqual(2, drifted.returncode)
        self.assertIn("hook drift", drifted.stderr)
        self.assertIn("hooks.json", drifted.stderr)

    def _skill(self, client: Path, skill_name: str) -> Path:
        return client / "skills" / skill_name / "SKILL.md"

    def test_install_preserves_user_configuration_and_deploys_shared_skills(
        self,
    ) -> None:
        self.codex.mkdir()
        self.claude.mkdir()
        original_codex = "# My Codex notes\nKeep this line.\n"
        original_claude = "# My Claude notes\nKeep this line too.\n"
        original_settings = {
            "permissions": {"allow": ["Bash(git status)"]},
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "echo unrelated-stop-hook",
                            }
                        ]
                    },
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "echo graphctl-claude-stop is user-owned",
                            }
                        ]
                    },
                ]
            },
        }
        (self.codex / "AGENTS.md").write_text(original_codex, encoding="utf-8")
        (self.claude / "CLAUDE.md").write_text(original_claude, encoding="utf-8")
        (self.claude / "settings.json").write_text(
            json.dumps(original_settings), encoding="utf-8"
        )

        self._install()

        codex_instructions = (self.codex / "AGENTS.md").read_text(encoding="utf-8")
        claude_instructions = (self.claude / "CLAUDE.md").read_text(encoding="utf-8")
        for original, instructions in (
            (original_codex, codex_instructions),
            (original_claude, claude_instructions),
        ):
            self.assertIn(original, instructions)
            self.assertEqual(1, instructions.count(MANAGED_BEGIN))
            self.assertEqual(1, instructions.count(MANAGED_END))
            self.assertIn(str(self.graphctl), instructions)

        settings = self._settings()
        self.assertEqual(original_settings["permissions"], settings["permissions"])
        unrelated_commands = [
            hook["command"]
            for matcher in settings["hooks"]["Stop"]
            for hook in matcher["hooks"]
            if hook.get("command") == "echo unrelated-stop-hook"
        ]
        self.assertEqual(["echo unrelated-stop-hook"], unrelated_commands)
        self.assertEqual([str(self.stop_guard)], self._managed_stop_commands(settings))
        stop_entries = [
            hook for matcher in settings["hooks"]["Stop"] for hook in matcher["hooks"]
        ]
        self.assertIn(
            {
                "type": "command",
                "command": "echo graphctl-claude-stop is user-owned",
            },
            stop_entries,
        )
        managed = next(
            hook for hook in stop_entries if hook.get("command") == str(self.stop_guard)
        )
        self.assertEqual([], managed.get("args"))

        for skill_name in SKILL_NAMES:
            codex_skill = self._skill(self.codex_skills, skill_name)
            claude_skill = self._skill(self.claude, skill_name)
            self.assertTrue(codex_skill.is_file(), codex_skill)
            self.assertTrue(claude_skill.is_file(), claude_skill)
            if os.name != "nt":
                self.assertEqual(
                    codex_skill.resolve(strict=True), claude_skill.resolve(strict=True)
                )
                self.assertTrue(
                    codex_skill.is_symlink() or codex_skill.parent.is_symlink(),
                    "Codex skill must reference the shared payload on POSIX",
                )
                self.assertTrue(
                    claude_skill.is_symlink() or claude_skill.parent.is_symlink(),
                    "Claude skill must reference the shared payload on POSIX",
                )

        codex_reviewer = self.codex / "agents" / "graph_reviewer.toml"
        claude_reviewer = self.claude / "agents" / "graph-reviewer.md"
        self.assertTrue(codex_reviewer.is_file())
        self.assertTrue(claude_reviewer.is_file())
        self.assertFalse(
            codex_reviewer.is_symlink(),
            "Codex rejects a symlinked user agent profile",
        )
        self.assertFalse(
            claude_reviewer.is_symlink(),
            "reviewer adapters are client-specific regular files",
        )
        self.assertIn(
            'name = "graph_reviewer"', codex_reviewer.read_text(encoding="utf-8")
        )
        self.assertIn(
            "name: graph-reviewer", claude_reviewer.read_text(encoding="utf-8")
        )
        self.assertNotEqual(
            codex_reviewer.read_bytes(),
            claude_reviewer.read_bytes(),
            "the clients require distinct reviewer-agent adapters",
        )

    def test_second_install_is_idempotent_without_duplicate_blocks_or_hooks(
        self,
    ) -> None:
        self._install()
        tracked_files = [
            self.codex / "AGENTS.md",
            self.claude / "CLAUDE.md",
            self.claude / "settings.json",
            self.codex / "agents" / "graph_reviewer.toml",
            self.claude / "agents" / "graph-reviewer.md",
        ]
        tracked_files.extend(
            self._skill(client, skill_name)
            for client in (self.codex_skills, self.claude)
            for skill_name in SKILL_NAMES
        )
        before = {path: path.read_bytes() for path in tracked_files}

        self._install()

        self.assertEqual(before, {path: path.read_bytes() for path in tracked_files})
        for path in (self.codex / "AGENTS.md", self.claude / "CLAUDE.md"):
            instructions = path.read_text(encoding="utf-8")
            self.assertEqual(1, instructions.count(MANAGED_BEGIN))
            self.assertEqual(1, instructions.count(MANAGED_END))
        self.assertEqual(1, len(self._managed_stop_commands(self._settings())))

    @unittest.skipIf(os.name == "nt", "POSIX symlink migration")
    def test_reinstall_migrates_legacy_linked_agents_to_regular_files(self) -> None:
        self._install()
        agents = (
            self.codex / "agents" / "graph_reviewer.toml",
            self.claude / "agents" / "graph-reviewer.md",
        )
        payload = self.home / ".local/share/graph-engineering-agent-harness/payload"
        sources = (
            payload / "agents/codex/graph_reviewer.toml",
            payload / "agents/claude/graph-reviewer.md",
        )
        for agent, source in zip(agents, sources):
            agent.unlink()
            agent.symlink_to(source)

        self._install()

        for agent in agents:
            self.assertTrue(agent.is_file())
            self.assertFalse(agent.is_symlink())

    def test_check_reports_drift_without_rewriting_the_managed_payload(self) -> None:
        self._install()
        damaged = self._skill(self.codex_skills, "graph-planning")
        damaged.write_text("drifted shared payload\n", encoding="utf-8")

        checked = self._run("--check")

        self.assertNotEqual(0, checked.returncode)
        self.assertIn("drift", (checked.stdout + checked.stderr).lower())
        self.assertEqual(
            "drifted shared payload\n", damaged.read_text(encoding="utf-8")
        )
        if os.name != "nt":
            self.assertEqual(
                "drifted shared payload\n",
                self._skill(self.claude, "graph-planning").read_text(encoding="utf-8"),
            )

    def test_check_reports_regular_target_drift_instead_of_install_conflict(
        self,
    ) -> None:
        self._install()
        reviewer = self.codex / "agents" / "graph_reviewer.toml"
        reviewer.write_text("drifted reviewer\n", encoding="utf-8")

        checked = self._run("--check")

        self.assertNotEqual(0, checked.returncode)
        output = (checked.stdout + checked.stderr).lower()
        self.assertIn("drift", output)
        self.assertNotIn("unmanaged target conflict", output)
        self.assertEqual("drifted reviewer\n", reviewer.read_text(encoding="utf-8"))

    def test_check_without_an_install_is_read_only(self) -> None:
        install_root = self.home / ".local/share/graph-engineering-agent-harness"

        checked = self._run_with_managed_runtime("--check")

        self.assertNotEqual(0, checked.returncode)
        self.assertIn("manifest is missing", (checked.stdout + checked.stderr).lower())
        self.assertFalse(install_root.exists())

    def test_missing_home_returns_a_controlled_error(self) -> None:
        missing_home = self.home.parent / "missing-home"

        rejected = subprocess.run(
            [sys.executable, str(INSTALLER), "--home", str(missing_home), "--check"],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(2, rejected.returncode)
        self.assertIn("installation failed", rejected.stderr.lower())
        self.assertNotIn("traceback", rejected.stderr.lower())

    def test_runtime_command_conflict_is_refused_before_creating_the_runtime(
        self,
    ) -> None:
        conflict = self.home / ".local/bin/graphctl"
        conflict.parent.mkdir(parents=True)
        conflict.write_text("user-owned executable\n", encoding="utf-8")
        install_root = self.home / ".local/share/graph-engineering-agent-harness"

        rejected = self._run_with_managed_runtime()

        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("conflict", (rejected.stdout + rejected.stderr).lower())
        self.assertEqual(
            "user-owned executable\n", conflict.read_text(encoding="utf-8")
        )
        self.assertFalse(install_root.exists())

    def test_runtime_build_uses_a_staged_source_outside_the_checkout(self) -> None:
        observed_sources: list[Path] = []

        def inspect_run(arguments: list[str], *, check: bool) -> None:
            self.assertTrue(check)
            if "install" not in arguments:
                return
            source = Path(arguments[-1])
            observed_sources.append(source)
            self.assertNotEqual(REPOSITORY, source)
            self.assertTrue((source / "pyproject.toml").is_file())
            self.assertTrue((source / "agent_harness/cli.py").is_file())
            self.assertFalse((source / ".git").exists())

        with patch("subprocess.run", side_effect=inspect_run):
            pc_installer._create_runtime(
                self.home / ".local/share/graph-engineering-agent-harness",
                Path(sys.executable),
            )

        self.assertEqual(1, len(observed_sources))

    def test_windows_copied_runtime_executable_matches_by_content(self) -> None:
        source = self.home / "source/graphctl.exe"
        target = self.home / ".local/bin/graphctl.exe"
        source.parent.mkdir()
        target.parent.mkdir(parents=True)
        source.write_bytes(b"managed executable\n")
        target.write_bytes(source.read_bytes())

        self.assertTrue(pc_installer._target_matches(target, source, platform="nt"))
        self.assertFalse(pc_installer._target_matches(target, source, platform="posix"))

    @unittest.skipIf(os.name == "nt", "POSIX shell command execution")
    def test_stop_hook_exec_form_handles_runtime_paths_that_contain_spaces(
        self,
    ) -> None:
        spaced_home = self.home.parent / "home with spaces"
        spaced_home.mkdir()
        runtime_bin = spaced_home / "runtime with spaces"
        runtime_bin.mkdir()
        for name in (
            "graphctl",
            "graphctl-claude-stop",
            "graphctl-session-start",
            "graphctl-session-end",
            "graphctl-edit",
        ):
            executable = runtime_bin / name
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

        installed = subprocess.run(
            [
                sys.executable,
                str(INSTALLER),
                "--home",
                str(spaced_home),
                "--runtime-bin-dir",
                str(runtime_bin),
            ],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, installed.returncode, installed.stdout + installed.stderr)
        settings = json.loads(
            (spaced_home / ".claude/settings.json").read_text(encoding="utf-8")
        )
        command = self._managed_stop_commands(settings)[0]

        managed = next(
            hook
            for matcher in settings["hooks"]["Stop"]
            for hook in matcher["hooks"]
            if hook.get("command") == command
        )
        self.assertEqual([], managed.get("args"))

        executed = subprocess.run([command, *managed["args"]], check=False)

        self.assertEqual(0, executed.returncode, command)

    def test_dry_run_writes_nothing_and_uninstall_removes_only_managed_entries(
        self,
    ) -> None:
        self.codex.mkdir()
        self.claude.mkdir()
        original_codex = "# personal Codex instructions\n"
        original_claude = "# personal Claude instructions\n"
        original_settings = {
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "echo keep-me"}]}]
            },
            "env": {"KEEP": "1"},
        }
        (self.codex / "AGENTS.md").write_text(original_codex, encoding="utf-8")
        (self.claude / "CLAUDE.md").write_text(original_claude, encoding="utf-8")
        (self.claude / "settings.json").write_text(
            json.dumps(original_settings), encoding="utf-8"
        )
        codex_custom_agent = self.codex / "agents" / "custom.toml"
        claude_custom_agent = self.claude / "agents" / "custom.md"
        codex_custom_agent.parent.mkdir()
        claude_custom_agent.parent.mkdir()
        codex_custom_agent.write_text("custom codex agent\n", encoding="utf-8")
        claude_custom_agent.write_text("custom claude agent\n", encoding="utf-8")

        dry_run = self._run("--dry-run")

        self.assertEqual(0, dry_run.returncode, dry_run.stdout + dry_run.stderr)
        self.assertEqual(
            original_codex, (self.codex / "AGENTS.md").read_text(encoding="utf-8")
        )
        self.assertEqual(
            original_claude, (self.claude / "CLAUDE.md").read_text(encoding="utf-8")
        )
        self.assertEqual(original_settings, self._settings())
        self.assertFalse((self.codex_skills / "skills").exists())
        self.assertFalse((self.claude / "skills").exists())

        self._install()
        removed = self._run("--uninstall")

        self.assertEqual(0, removed.returncode, removed.stdout + removed.stderr)
        self.assertEqual(
            original_codex, (self.codex / "AGENTS.md").read_text(encoding="utf-8")
        )
        self.assertEqual(
            original_claude, (self.claude / "CLAUDE.md").read_text(encoding="utf-8")
        )
        self.assertEqual(original_settings, self._settings())
        self.assertEqual(
            "custom codex agent\n", codex_custom_agent.read_text(encoding="utf-8")
        )
        self.assertEqual(
            "custom claude agent\n", claude_custom_agent.read_text(encoding="utf-8")
        )
        self.assertFalse((self.codex / "agents" / "graph_reviewer.toml").exists())
        self.assertFalse((self.claude / "agents" / "graph-reviewer.md").exists())
        for client in (self.codex_skills, self.claude):
            for skill_name in SKILL_NAMES:
                self.assertFalse(self._skill(client, skill_name).exists())

    def test_unmanaged_same_name_skill_is_refused_before_any_partial_install(
        self,
    ) -> None:
        self.codex.mkdir()
        original_codex = "# existing user configuration\n"
        (self.codex / "AGENTS.md").write_text(original_codex, encoding="utf-8")
        conflict = self._skill(self.codex_skills, "graph-planning")
        conflict.parent.mkdir(parents=True)
        conflict.write_text("# User-owned skill\nDo not replace.\n", encoding="utf-8")

        rejected = self._run()

        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("conflict", (rejected.stdout + rejected.stderr).lower())
        self.assertEqual(
            "# User-owned skill\nDo not replace.\n",
            conflict.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            original_codex, (self.codex / "AGENTS.md").read_text(encoding="utf-8")
        )
        self.assertFalse((self.claude / "CLAUDE.md").exists())
        self.assertFalse((self.claude / "settings.json").exists())
        self.assertFalse((self.codex / "agents" / "graph_reviewer.toml").exists())

    def test_user_modified_managed_agent_is_neither_overwritten_nor_uninstalled(
        self,
    ) -> None:
        self._install()
        reviewer = self.codex / "agents" / "graph_reviewer.toml"
        replacement = "# user replacement after installation\n"
        reviewer.write_text(replacement, encoding="utf-8")

        reinstall = self._run()

        self.assertNotEqual(0, reinstall.returncode)
        self.assertIn("conflict", (reinstall.stdout + reinstall.stderr).lower())
        self.assertEqual(replacement, reviewer.read_text(encoding="utf-8"))

        removed = self._run("--uninstall")

        self.assertEqual(0, removed.returncode, removed.stdout + removed.stderr)
        self.assertEqual(replacement, reviewer.read_text(encoding="utf-8"))

    @unittest.skipIf(os.name == "nt", "POSIX symlink safety")
    def test_symlinked_user_settings_are_refused_without_touching_target(self) -> None:
        outside = self.home.parent / "outside-settings.json"
        original = '{"env": {"KEEP": "1"}}\n'
        outside.write_text(original, encoding="utf-8")
        self.claude.mkdir()
        (self.claude / "settings.json").symlink_to(outside)

        rejected = self._run()

        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("link-like", (rejected.stdout + rejected.stderr).lower())
        self.assertEqual(original, outside.read_text(encoding="utf-8"))
        self.assertFalse((self.codex / "AGENTS.md").exists())


if __name__ == "__main__":
    unittest.main()
