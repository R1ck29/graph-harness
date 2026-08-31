from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_harness import claude_hook, journal, session_hooks, worktree
from agent_harness.paths import repository_key, repository_key_fast

REPOSITORY = Path(__file__).resolve().parents[1]


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(repo), *arguments),
        capture_output=True,
        text=True,
        check=True,
    )


class SessionHookTests(unittest.TestCase):
    """Only a hook can record a session boundary, so the record must survive."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.home = Path(self._directory.name) / "home"
        self.home.mkdir()
        self.repo = Path(self._directory.name) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "harness@example.invalid")
        _git(self.repo, "config", "user.name", "harness")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "init")
        self._environment = mock.patch.dict(
            os.environ,
            {
                "GRAPH_HARNESS_HOME": str(self.home),
                "CLAUDE_PROJECT_DIR": str(self.repo),
                "CLAUDE_CODE_SESSION_ID": "s1",
            },
        )
        self._environment.start()
        self.addCleanup(self._environment.stop)

    def _payload(self, **fields: Any) -> str:
        base = {"session_id": "s1", "cwd": str(self.repo)}
        base.update(fields)
        return json.dumps(base)

    def _events(self) -> list[str]:
        return [str(entry["event"]) for entry in journal.read(self.home)]

    def _start(self, payload: str | None = None) -> int:
        with mock.patch("sys.stdin", io.StringIO(payload or self._payload())):
            return session_hooks.session_start()

    def _end(self, payload: str | None = None) -> int:
        with mock.patch("sys.stdin", io.StringIO(payload or self._payload())):
            return session_hooks.session_end()

    def _stop(self, payload: str | None = None) -> int:
        with mock.patch("sys.stdin", io.StringIO(payload or self._payload())):
            return claude_hook.main()

    def test_session_start_and_end_record_their_boundaries(self) -> None:
        self.assertEqual(0, self._start())
        self.assertEqual(0, self._end())

        self.assertEqual(["session_open", "session_close"], self._events())
        opened = list(journal.read(self.home))[0]
        self.assertEqual("claude", opened["client"])
        self.assertEqual(repository_key(self.repo), opened["repo"])
        self.assertTrue(opened["snapshot"]["git"])

    def test_the_stop_hook_records_a_turn_and_keeps_its_exit_codes(self) -> None:
        self.assertEqual(0, self._stop())

        self.assertEqual(["turn_end"], self._events())

    def test_the_stop_hook_still_refuses_an_incomplete_graph(self) -> None:
        (self.repo / "task-graph.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "objective": "unfinished",
                    "nodes": [
                        {
                            "id": "task",
                            "description": "unfinished",
                            "depends_on": [],
                            "status": "ready",
                            "assigned_role": "implementer",
                            "acceptance_criteria": ["done"],
                            "evidence": [],
                            "attempts": 0,
                            "max_attempts": 2,
                            "failure_reason": None,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        previous = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, previous)

        self.assertEqual(2, self._stop())
        self.assertEqual(["turn_end"], self._events())

    def test_the_stop_hook_returns_early_when_already_active(self) -> None:
        self.assertEqual(0, self._stop(self._payload(stop_hook_active=True)))

        self.assertEqual([], self._events())

    def test_a_recording_failure_cannot_change_the_stop_verdict(self) -> None:
        with mock.patch(
            "agent_harness.journal.append", side_effect=OSError("journal is gone")
        ):
            self.assertEqual(0, self._stop())

    def test_an_unusable_payload_records_nothing_and_does_not_fail(self) -> None:
        self.assertEqual(0, self._start("not json"))
        self.assertEqual(0, self._end("not json"))

        self.assertEqual([], self._events())

    def test_an_oversized_payload_is_refused(self) -> None:
        oversized = json.dumps({"session_id": "s1", "pad": "x" * 1_100_000})

        self.assertEqual(0, self._start(oversized))
        self.assertEqual([], self._events())

    def test_a_prior_bypass_above_the_threshold_is_reported_at_the_next_start(
        self,
    ) -> None:
        self._start()
        (self.repo / "tracked.txt").write_text("base\nmore\n" * 20, encoding="utf-8")
        (self.repo / "second.txt").write_text("new\n", encoding="utf-8")
        self._end()

        with mock.patch("sys.stderr", io.StringIO()) as reported:
            outcome = self._start(self._payload(session_id="s2"))

        self.assertEqual(2, outcome)
        self.assertIn("without recording any task-graph state", reported.getvalue())

    def test_a_trivial_change_is_recorded_but_not_reported(self) -> None:
        self._start()
        (self.repo / "tracked.txt").write_text("base\ntypo\n", encoding="utf-8")
        self._end()

        with mock.patch("sys.stderr", io.StringIO()) as reported:
            outcome = self._start(self._payload(session_id="s2"))

        self.assertEqual(0, outcome)
        self.assertEqual("", reported.getvalue())
        self.assertIn("session_close", self._events())

    def test_a_session_that_used_the_graph_is_not_reported(self) -> None:
        self._start()
        (self.repo / "tracked.txt").write_text("base\nmore\n" * 20, encoding="utf-8")
        journal.append(
            journal.record(
                "graph_transition",
                client="claude",
                session_id="s1",
                repo=repository_key(self.repo),
                command="start",
            ),
            self.home,
        )
        self._end()

        with mock.patch("sys.stderr", io.StringIO()) as reported:
            outcome = self._start(self._payload(session_id="s2"))

        self.assertEqual(0, outcome)
        self.assertEqual("", reported.getvalue())

    def test_a_session_in_another_repository_is_not_reported(self) -> None:
        self._start()
        (self.repo / "tracked.txt").write_text("base\nmore\n" * 20, encoding="utf-8")
        self._end()
        elsewhere = Path(self._directory.name) / "other"
        elsewhere.mkdir()

        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(elsewhere)}):
            with mock.patch("sys.stderr", io.StringIO()) as reported:
                outcome = self._start(
                    json.dumps({"session_id": "s2", "cwd": str(elsewhere)})
                )

        self.assertEqual(0, outcome)
        self.assertEqual("", reported.getvalue())

    def test_the_most_recent_bypass_is_the_one_reported(self) -> None:
        for index, session in enumerate(("older", "newer"), start=1):
            journal.append(
                journal.record(
                    "session_open",
                    client="claude",
                    session_id=session,
                    repo=str(self.repo),
                    snapshot={
                        "git": True,
                        "head": "h",
                        "digest": f"before-{index}",
                        "files": 0,
                        "lines": 0,
                    },
                ),
                self.home,
            )
            journal.append(
                journal.record(
                    "session_close",
                    client="claude",
                    session_id=session,
                    repo=str(self.repo),
                    snapshot={
                        "git": True,
                        "head": "h",
                        "digest": f"after-{index}",
                        "files": index * 5,
                        "lines": index * 50,
                    },
                ),
                self.home,
            )

        reported = session_hooks.previous_bypass(str(self.repo))

        self.assertIsNotNone(reported)
        assert reported is not None
        self.assertEqual("newer", reported["session_id"])

    def test_a_session_that_never_finished_is_not_reported(self) -> None:
        journal.append(
            journal.record(
                "session_open",
                client="claude",
                session_id="still-running",
                repo=str(self.repo),
                snapshot={
                    "git": True,
                    "head": "h",
                    "digest": "before",
                    "files": 0,
                    "lines": 0,
                },
            ),
            self.home,
        )

        self.assertIsNone(session_hooks.previous_bypass(str(self.repo)))

    def test_a_malformed_snapshot_is_not_treated_as_a_bypass(self) -> None:
        for event in ("session_open", "session_close"):
            journal.append(
                journal.record(
                    event,
                    client="claude",
                    session_id="broken",
                    repo=str(self.repo),
                    snapshot="not a snapshot",
                ),
                self.home,
            )

        self.assertIsNone(session_hooks.previous_bypass(str(self.repo)))

    def test_either_threshold_alone_triggers_the_report(self) -> None:
        def bypass(files: int, lines: int, session: str) -> None:
            journal.append(
                journal.record(
                    "session_open",
                    client="claude",
                    session_id=session,
                    repo=str(self.repo),
                    snapshot={
                        "git": True,
                        "head": "h",
                        "digest": "before",
                        "files": 0,
                        "lines": 0,
                    },
                ),
                self.home,
            )
            journal.append(
                journal.record(
                    "session_close",
                    client="claude",
                    session_id=session,
                    repo=str(self.repo),
                    snapshot={
                        "git": True,
                        "head": "h",
                        "digest": "after",
                        "files": files,
                        "lines": lines,
                    },
                ),
                self.home,
            )

        bypass(2, 0, "many-files")
        self.assertIsNotNone(session_hooks.previous_bypass(str(self.repo)))

        bypass(1, 20, "many-lines")
        self.assertIsNotNone(session_hooks.previous_bypass(str(self.repo)))

    def test_all_three_producers_agree_on_the_repository_key(self) -> None:
        # A session hook, the turn hook and the CLI each derive this key in a
        # different place, and records are matched by exact string equality.
        # If they disagree, a session that used the graph reads as one that
        # never did.
        self._start()
        self._stop()
        previous = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, previous)
        journal.append(
            journal.record(
                "graph_transition",
                client="claude",
                session_id="s1",
                repo=repository_key(),
                command="start",
            ),
            self.home,
        )

        keys = {str(entry["repo"]) for entry in journal.read(self.home)}

        self.assertEqual(1, len(keys), keys)

    def test_a_session_reached_through_a_symlink_is_not_falsely_accused(self) -> None:
        # macOS reaches /tmp and /var through symlinks, so an unresolved
        # project path is the normal case rather than an exotic one.
        linked = Path(self._directory.name) / "linked-repo"
        try:
            linked.symlink_to(self.repo, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(linked)}):
            self._start()
            (self.repo / "tracked.txt").write_text(
                "base\nmore\n" * 20, encoding="utf-8"
            )
            (self.repo / "second.txt").write_text("new\n", encoding="utf-8")
            previous = os.getcwd()
            os.chdir(self.repo)
            self.addCleanup(os.chdir, previous)
            journal.append(
                journal.record(
                    "graph_transition",
                    client="claude",
                    session_id="s1",
                    repo=repository_key(),
                    command="start",
                ),
                self.home,
            )
            self._end()

            with mock.patch("sys.stderr", io.StringIO()) as reported:
                outcome = self._start(self._payload(session_id="s2"))

        self.assertEqual(0, outcome, reported.getvalue())
        self.assertEqual("", reported.getvalue())

    def test_a_bypass_is_reported_once_and_not_after_a_later_session(self) -> None:
        self._start()
        (self.repo / "tracked.txt").write_text("base\nmore\n" * 20, encoding="utf-8")
        (self.repo / "second.txt").write_text("new\n", encoding="utf-8")
        self._end()

        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(2, self._start(self._payload(session_id="s2")))
        journal.append(
            journal.record(
                "graph_transition",
                client="claude",
                session_id="s2",
                repo=repository_key(self.repo),
                command="start",
            ),
            self.home,
        )
        self._end(self._payload(session_id="s2"))

        with mock.patch("sys.stderr", io.StringIO()) as reported:
            outcome = self._start(self._payload(session_id="s3"))

        self.assertEqual(0, outcome, reported.getvalue())
        self.assertEqual("", reported.getvalue())

    def test_a_question_session_afterwards_does_not_bury_a_bypass(self) -> None:
        # A session that changed nothing must not become "the previous
        # session" and hide the bypass before it.
        self._start()
        (self.repo / "tracked.txt").write_text("base\nmore\n" * 20, encoding="utf-8")
        (self.repo / "second.txt").write_text("new\n", encoding="utf-8")
        self._end()

        with mock.patch("sys.stderr", io.StringIO()) as reported:
            outcome = self._start(self._payload(session_id="asked-a-question"))

        self.assertEqual(2, outcome)
        self.assertIn("without recording any task-graph state", reported.getvalue())
        # And the report is recorded, so the next session hears it once only.
        self._end(self._payload(session_id="asked-a-question"))
        with mock.patch("sys.stderr", io.StringIO()) as again:
            self.assertEqual(0, self._start(self._payload(session_id="s3")))
        self.assertEqual("", again.getvalue())

    def test_committed_work_is_reported_though_it_leaves_no_dirty_files(self) -> None:
        # Committing returns the tree to a clean state, so the snapshots alone
        # report no changed files at all. The size has to come from the commit.
        self._start()
        (self.repo / "tracked.txt").write_text("base\nmore\n" * 20, encoding="utf-8")
        (self.repo / "second.txt").write_text("new\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "work")
        self._end()

        with mock.patch("sys.stderr", io.StringIO()) as reported:
            outcome = self._start(self._payload(session_id="s2"))

        self.assertEqual(2, outcome)
        self.assertIn("without recording any task-graph state", reported.getvalue())

    def test_a_session_is_not_judged_on_the_records_it_just_wrote(self) -> None:
        # The opening record of the session asking the question is excluded by
        # position, not by identity or timestamp, so a resumed session that
        # reuses its identifier still hears about its own earlier run.
        self._start()
        (self.repo / "tracked.txt").write_text("base\nmore\n" * 20, encoding="utf-8")
        (self.repo / "second.txt").write_text("new\n", encoding="utf-8")
        self._stop()

        with mock.patch("sys.stderr", io.StringIO()) as reported:
            resumed = self._start(self._payload(source="resume"))

        self.assertEqual(2, resumed, reported.getvalue())
        self.assertIn("without recording any task-graph state", reported.getvalue())

    def test_an_end_recorded_before_its_open_is_left_alone(self) -> None:
        journal.append(
            journal.record(
                "session_close",
                client="claude",
                session_id="out-of-order",
                repo=repository_key(self.repo),
                snapshot={
                    "git": True,
                    "head": "a",
                    "digest": "after",
                    "files": 9,
                    "lines": 99,
                },
            ),
            self.home,
        )
        stored = journal.month_files(self.home)[0]
        stored.write_text(
            stored.read_text(encoding="utf-8").replace('"ts":"20', '"ts":"19', 1),
            encoding="utf-8",
        )
        journal.append(
            journal.record(
                "session_open",
                client="claude",
                session_id="out-of-order",
                repo=repository_key(self.repo),
                snapshot={
                    "git": True,
                    "head": "a",
                    "digest": "before",
                    "files": 0,
                    "lines": 0,
                },
            ),
            self.home,
        )

        self.assertIsNone(session_hooks.previous_bypass(repository_key(self.repo)))

    def _bypass(self) -> None:
        """Leave one finished session that changed code without the graph."""

        self._start()
        (self.repo / "tracked.txt").write_text("base\nmore\n" * 20, encoding="utf-8")
        (self.repo / "second.txt").write_text("new\n", encoding="utf-8")
        self._end()

    def test_a_graphctl_run_without_a_client_session_id_still_counts(self) -> None:
        # Only one client exports a session id. Attributing a run by identity
        # left every other way of running graphctl counting for no session,
        # so the session that did use the graph was accused.
        self._start()
        (self.repo / "tracked.txt").write_text("base\nmore\n" * 20, encoding="utf-8")
        (self.repo / "second.txt").write_text("new\n", encoding="utf-8")
        journal.append(
            journal.record(
                "graph_transition",
                client=None,
                repo=repository_key(self.repo),
                command="start",
            ),
            self.home,
        )
        self._end()

        with mock.patch("sys.stderr", io.StringIO()) as reported:
            outcome = self._start(self._payload(session_id="s2"))

        self.assertEqual(0, outcome, reported.getvalue())

    def test_a_graphctl_run_from_a_subdirectory_still_counts(self) -> None:
        nested = self.repo / "package" / "inner"
        nested.mkdir(parents=True)
        self._start()
        (self.repo / "tracked.txt").write_text("base\nmore\n" * 20, encoding="utf-8")
        (self.repo / "second.txt").write_text("new\n", encoding="utf-8")
        previous = os.getcwd()
        os.chdir(nested)
        self.addCleanup(os.chdir, previous)
        journal.append(
            journal.record(
                "graph_transition",
                client="claude",
                session_id="s1",
                repo=repository_key(),
                command="start",
            ),
            self.home,
        )
        os.chdir(previous)
        self._end()

        with mock.patch("sys.stderr", io.StringIO()) as reported:
            outcome = self._start(self._payload(session_id="s2"))

        self.assertEqual(0, outcome, reported.getvalue())

    def test_a_timestampless_transition_cannot_silence_every_report(self) -> None:
        self._bypass()
        directory = journal.journal_directory(self.home)
        month = sorted(directory.glob("*.jsonl"))[0]
        with month.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "schema_version": 1,
                        "event": "graph_transition",
                        "repo": repository_key(self.repo),
                        "command": "start",
                    }
                )
                + "\n"
            )

        with mock.patch("sys.stderr", io.StringIO()) as reported:
            outcome = self._start(self._payload(session_id="s2"))

        self.assertEqual(2, outcome)
        self.assertIn("without recording any task-graph state", reported.getvalue())

    def test_a_transition_outside_the_session_window_does_not_silence_it(self) -> None:
        journal.append(
            journal.record(
                "graph_transition",
                client="claude",
                session_id="someone-else",
                repo=repository_key(self.repo),
                command="start",
            ),
            self.home,
        )
        self._bypass()

        with mock.patch("sys.stderr", io.StringIO()) as reported:
            outcome = self._start(self._payload(session_id="s2"))

        self.assertEqual(2, outcome)
        self.assertIn("without recording any task-graph state", reported.getvalue())

    def test_a_degraded_observation_is_not_treated_as_evidence(self) -> None:
        for event, digest in (("session_open", "before"), ("session_close", "after")):
            journal.append(
                journal.record(
                    event,
                    client="claude",
                    session_id="degraded",
                    repo=repository_key(self.repo),
                    snapshot={
                        "git": True,
                        "head": "h",
                        "digest": digest,
                        "files": 9,
                        "lines": 99,
                        "degraded": True,
                    },
                ),
                self.home,
            )

        self.assertIsNone(session_hooks.previous_bypass(repository_key(self.repo)))

    def test_a_budget_exhausted_snapshot_is_marked_degraded(self) -> None:
        with mock.patch.object(worktree, "SNAPSHOT_BUDGET_SECONDS", -1.0):
            observed = worktree.snapshot(self.repo)

        self.assertFalse(observed["git"])
        self.assertTrue(observed["degraded"])

    def test_a_pull_sized_head_move_is_not_reported(self) -> None:
        # Any HEAD move used to be reportable regardless of size, which
        # accused a session for a pull, a checkout, or a commit made in
        # another terminal.
        self._start()
        (self.repo / "tracked.txt").write_text("base\nsmall\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "one small line")
        self._end()

        with mock.patch("sys.stderr", io.StringIO()) as reported:
            outcome = self._start(self._payload(session_id="s2"))

        self.assertEqual(0, outcome, reported.getvalue())

    def test_neither_hook_can_be_made_to_raise(self) -> None:
        # A hook that raises is reported by the client as broken, and the
        # record it was going to write is lost either way.
        with mock.patch(
            "agent_harness.session_hooks.observe", side_effect=OSError("no journal")
        ):
            self.assertEqual(0, self._start())
            self.assertEqual(0, self._end())

        with mock.patch(
            "agent_harness.session_hooks.worktree.snapshot",
            side_effect=RuntimeError("git exploded"),
        ):
            self.assertEqual(0, self._start())
            self.assertEqual(0, self._end())

    def _session(self, session_id: str, files: int, lines: int, digest: str) -> None:
        """Record one finished session of a stated size, without a hook."""

        for event, snapshot in (
            ("session_open", {"files": 0, "lines": 0, "digest": f"{digest}-before"}),
            ("session_close", {"files": files, "lines": lines, "digest": digest}),
        ):
            journal.append(
                journal.record(
                    event,
                    client="claude",
                    session_id=session_id,
                    repo=repository_key(self.repo),
                    snapshot={
                        "git": True,
                        "head": "a" * 40,
                        "degraded": False,
                        **snapshot,
                    },
                ),
                self.home,
            )

    def test_a_trivial_session_afterwards_does_not_bury_a_bypass(self) -> None:
        # Judging only the newest finished session let one small change after
        # a real bypass hide it for good, because a session that is never
        # reported is never recorded as reported and so stays the newest.
        self._session("bypassed", files=9, lines=90, digest="big")
        self._session("tiny", files=1, lines=1, digest="small")

        found = session_hooks.previous_bypass(repository_key(self.repo))

        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual("bypassed", found["session_id"])

    def test_a_bypass_already_reported_is_skipped_and_an_older_one_is_found(
        self,
    ) -> None:
        self._session("older", files=9, lines=90, digest="older")
        self._session("newer", files=9, lines=90, digest="newer")
        journal.append(
            journal.record(
                "bypass_reported",
                client="claude",
                session_id="newer",
                repo=repository_key(self.repo),
            ),
            self.home,
        )

        found = session_hooks.previous_bypass(repository_key(self.repo))

        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual("older", found["session_id"])

    def test_the_scan_stops_at_a_session_that_used_the_graph(self) -> None:
        # Everything older than a session that used the graph has already
        # been answered by it, so reaching back past it would nag.
        self._bypass()
        self._start(self._payload(session_id="compliant"))
        (self.repo / "third.txt").write_text("more\n" * 30, encoding="utf-8")
        journal.append(
            journal.record(
                "graph_transition",
                client="claude",
                session_id="compliant",
                repo=repository_key(self.repo),
                command="start",
            ),
            self.home,
        )
        self._end(self._payload(session_id="compliant"))

        with mock.patch("sys.stderr", io.StringIO()) as reported:
            outcome = self._start(self._payload(session_id="s3"))

        self.assertEqual(0, outcome, reported.getvalue())

    def test_a_report_that_cannot_be_recorded_is_not_printed(self) -> None:
        # The written record is the only thing that stops the same bypass
        # being reported again, so saying it without recording it repeats
        # forever.
        self._bypass()
        real = journal.append

        def refuse(entry: dict[str, Any], home: object = None) -> bool:
            if entry.get("event") == "bypass_reported":
                return False
            return bool(real(entry, home))  # type: ignore[arg-type]

        with mock.patch("agent_harness.journal.append", side_effect=refuse):
            with mock.patch("sys.stderr", io.StringIO()) as reported:
                outcome = self._start(self._payload(session_id="s2"))

        self.assertEqual(0, outcome)
        self.assertEqual("", reported.getvalue())

    def test_a_timestampless_session_cannot_hijack_the_selection(self) -> None:
        self._bypass()
        directory = journal.journal_directory(self.home)
        month = sorted(directory.glob("*.jsonl"))[0]
        with month.open("a", encoding="utf-8") as handle:
            for event in ("session_open", "session_close"):
                handle.write(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "event": event,
                            "session_id": "ghost",
                            "repo": repository_key(self.repo),
                            "snapshot": {
                                "git": True,
                                "head": "a" * 40,
                                "digest": event,
                                "files": 0,
                                "lines": 0,
                            },
                        }
                    )
                    + "\n"
                )

        with mock.patch("sys.stderr", io.StringIO()) as reported:
            outcome = self._start(self._payload(session_id="s2"))

        self.assertEqual(2, outcome, reported.getvalue())

    def test_a_forged_head_is_never_passed_to_git(self) -> None:
        # The journal is forgeable by anyone who can write the home, so an
        # option-shaped head must not reach git as an argument.
        target = Path(self._directory.name) / "pwned.txt"

        files, lines = worktree.committed_size(
            self.repo,
            {"head": f"--output={target}"},
            {"head": "b" * 40},
        )

        self.assertEqual((0, 0), (files, lines))
        self.assertFalse(target.exists())

    def test_a_corrupt_count_neither_raises_nor_silences_the_repository(self) -> None:
        self.assertEqual(
            (0, 0),
            worktree.change_size(
                {"git": True, "files": "lots", "lines": None},
                {"git": True, "files": "many", "lines": "several"},
            ),
        )

    def test_dirt_present_before_the_session_is_not_charged_to_it(self) -> None:
        # Falling back to the absolute file count named files nobody in the
        # session had touched.
        before = {"git": True, "head": "h", "digest": "a", "files": 5, "lines": 40}
        after = {"git": True, "head": "h", "digest": "b", "files": 5, "lines": 41}

        self.assertEqual((0, 1), worktree.change_size(before, after))

    def test_the_scan_does_not_reach_back_indefinitely(self) -> None:
        # Each candidate weighed costs a git call, and announcing something
        # from weeks ago as the previous session would be false besides.
        for index in range(session_hooks.MAX_CANDIDATES + 3):
            self._session(f"tiny-{index}", files=1, lines=1, digest=f"t{index}")
        self._session("ancient", files=9, lines=90, digest="ancient")
        directory = journal.journal_directory(self.home)
        month = sorted(directory.glob("*.jsonl"))[0]
        lines = month.read_text(encoding="utf-8").splitlines(keepends=True)
        # Put the real bypass first, behind more trivial sessions than the
        # scan is willing to weigh.
        month.write_text("".join(lines[-2:] + lines[:-2]), encoding="utf-8")

        self.assertIsNone(session_hooks.previous_bypass(repository_key(self.repo)))

    def test_a_stale_report_lock_does_not_stall_every_session_start(self) -> None:
        self._bypass()
        lock = journal.journal_directory(self.home) / "report.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("999999\n", encoding="utf-8")

        started = time.monotonic()
        with mock.patch("sys.stderr", io.StringIO()):
            outcome = self._start(self._payload(session_id="s2"))
        elapsed = time.monotonic() - started

        self.assertEqual(0, outcome)
        self.assertLess(elapsed, 3.0)
        self.assertNotIn(
            "report.lock",
            [path.name for path in journal.month_files(self.home)],
        )

    def test_a_graphctl_run_still_counts_when_no_session_has_an_identifier(
        self,
    ) -> None:
        # Falling back to filtering by an absent identifier discarded every
        # record that also lacked one, including the graphctl runs that prove
        # the session followed the protocol.
        history = [
            {"event": "graph_transition", "repo": "/r", "ts": "t1"},
            {"event": "session_open", "session_id": "a", "repo": "/r", "ts": "t2"},
        ]

        cut = session_hooks._cut_position(
            history, {"event": "session_open", "session_id": None, "ts": "t9"}
        )

        self.assertEqual(len(history), cut)

    def test_two_starts_at_once_report_one_bypass_between_them(self) -> None:
        # Both sessions write their opening record before either decides, so
        # a decision cut at its own record cannot see the other's claim and
        # both would report. The lock alone does not fix that.
        self._bypass()
        first = session_hooks.observe(
            "session_open", json.loads(self._payload(session_id="A"))
        )
        second = session_hooks.observe(
            "session_open", json.loads(self._payload(session_id="B"))
        )

        claims = [
            session_hooks._claim_bypass(str(first["repo"]), first),
            session_hooks._claim_bypass(str(second["repo"]), second),
        ]

        self.assertEqual(1, len([claim for claim in claims if claim is not None]))
        recorded = [
            entry
            for entry in journal.read(self.home)
            if entry.get("event") == "bypass_reported"
        ]
        self.assertEqual(1, len(recorded), recorded)

    def test_a_graphctl_run_recorded_late_still_counts_for_its_session(
        self,
    ) -> None:
        # Records from separate processes are not ordered by causality, so a
        # run flushed after its own session closed lands outside every window.
        # Placing it by position alone accused the session that used the graph.
        self._bypass()
        asking = journal.record(
            "session_open",
            client="claude",
            session_id="asker",
            repo=repository_key(self.repo),
            snapshot={
                "git": True,
                "head": "a" * 40,
                "degraded": False,
                "digest": "x",
                "files": 2,
                "lines": 41,
            },
        )
        journal.append(asking, self.home)
        journal.append(
            journal.record(
                "graph_transition",
                client="claude",
                session_id="s1",
                repo=repository_key(self.repo),
                command="start",
            ),
            self.home,
        )

        self.assertIsNone(
            session_hooks.previous_bypass(repository_key(self.repo), since=asking)
        )

    def _journal_text(self) -> str:
        return "".join(
            path.read_text(encoding="utf-8") for path in journal.month_files(self.home)
        )

    def _edits(self) -> list[dict[str, Any]]:
        return [
            entry for entry in journal.read(self.home) if entry.get("event") == "edit"
        ]

    def _edit_payload(self, session_id: str, tool: str, path: str) -> str:
        payload = json.loads(self._payload(session_id=session_id))
        payload["tool_name"] = tool
        payload["tool_input"] = {"file_path": path}
        return json.dumps(payload)

    def test_an_edit_records_a_hashed_path_and_never_the_path(self) -> None:
        # A path is content enough: a filename can name a customer, and
        # docs/security.md forbids recording user content.
        target = str(self.repo / "clients" / "acme-contract.md")

        with mock.patch(
            "sys.stdin", io.StringIO(self._edit_payload("E1", "Edit", target))
        ):
            self.assertEqual(0, session_hooks.record_edit())

        recorded = self._edits()
        self.assertEqual(1, len(recorded))
        self.assertEqual("Edit", recorded[0]["tool"])
        self.assertEqual("E1", recorded[0]["session_id"])
        self.assertEqual(repository_key(self.repo), recorded[0]["repo"])
        self.assertRegex(recorded[0]["path_id"], r"^[0-9a-f]{12}$")
        text = self._journal_text()
        self.assertNotIn("acme-contract", text)
        self.assertNotIn("clients", text)

    def test_the_same_path_hashes_the_same_and_a_different_one_does_not(self) -> None:
        first = session_hooks.path_id(self.repo, str(self.repo / "a.py"))
        again = session_hooks.path_id(self.repo, str(self.repo / "a.py"))
        other = session_hooks.path_id(self.repo, str(self.repo / "b.py"))

        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertRegex(first, r"^[0-9a-f]{12}$")

    def test_a_path_outside_the_repository_is_recorded_without_the_path(self) -> None:
        with mock.patch(
            "sys.stdin", io.StringIO(self._edit_payload("E2", "Write", "/etc/hosts"))
        ):
            self.assertEqual(0, session_hooks.record_edit())

        recorded = self._edits()
        self.assertEqual(1, len(recorded))
        self.assertRegex(recorded[0]["path_id"], r"^[0-9a-f]{12}$")
        self.assertNotIn("/etc/hosts", self._journal_text())

    def test_every_managed_editing_tool_is_recorded(self) -> None:
        for index, tool in enumerate(("Edit", "Write", "MultiEdit", "NotebookEdit")):
            payload = self._edit_payload(f"E{index}", tool, str(self.repo / "a.py"))
            with mock.patch("sys.stdin", io.StringIO(payload)):
                self.assertEqual(0, session_hooks.record_edit())

        self.assertEqual(
            ["Edit", "Write", "MultiEdit", "NotebookEdit"],
            [entry["tool"] for entry in self._edits()],
        )

    def test_an_edit_that_cannot_be_recorded_still_exits_zero(self) -> None:
        payload = self._edit_payload("E3", "Edit", str(self.repo / "a.py"))

        with mock.patch("sys.stdin", io.StringIO(payload)):
            with mock.patch.object(journal, "append", return_value=False):
                self.assertEqual(0, session_hooks.record_edit())

        with mock.patch("sys.stdin", io.StringIO(payload)):
            with mock.patch.object(journal, "append", side_effect=OSError("full")):
                self.assertEqual(0, session_hooks.record_edit())

    def test_an_unusable_edit_payload_records_nothing_and_exits_zero(self) -> None:
        # A tool call is not a boundary; nothing is worth recording when the
        # payload does not name a tool and a file.
        unusable = [
            "",
            "not json",
            "[]",
            "{}",
            json.dumps({"session_id": "x", "tool_name": "Edit"}),
            json.dumps(
                {"session_id": "x", "tool_name": "", "tool_input": {"file_path": "a"}}
            ),
            json.dumps({"session_id": "x", "tool_name": "Edit", "tool_input": "a"}),
            json.dumps(
                {
                    "session_id": "x",
                    "tool_name": "Edit",
                    "tool_input": {"file_path": ""},
                }
            ),
        ]

        for raw in unusable:
            with mock.patch("sys.stdin", io.StringIO(raw)):
                self.assertEqual(0, session_hooks.record_edit(), raw)

        self.assertEqual([], self._edits())

    def test_an_oversized_edit_payload_is_refused(self) -> None:
        payload = self._edit_payload(
            "E4", "Edit", str(self.repo / ("a" * session_hooks.MAX_HOOK_INPUT_BYTES))
        )

        with mock.patch("sys.stdin", io.StringIO(payload)):
            self.assertEqual(0, session_hooks.record_edit())

        self.assertEqual([], self._edits())

    def test_the_edit_hook_takes_no_snapshot_and_spawns_no_process(self) -> None:
        # It runs after every editing tool call rather than once per turn, so
        # anything paid here is paid hundreds of times in a session. A
        # snapshot costs about 130 ms and several git calls; a bare
        # `git rev-parse` for the repository key still costs a process.
        payload = self._edit_payload("E5", "Edit", str(self.repo / "a.py"))
        runs: list[tuple[str, ...]] = []
        real_run = subprocess.run

        def counted(arguments: Any, **options: Any) -> Any:
            runs.append(tuple(arguments))
            return real_run(arguments, **options)

        with mock.patch("sys.stdin", io.StringIO(payload)):
            with mock.patch.object(
                worktree, "snapshot", side_effect=AssertionError("snapshotted")
            ):
                with mock.patch("subprocess.run", counted):
                    self.assertEqual(0, session_hooks.record_edit())

        self.assertEqual(1, len(self._edits()))
        self.assertEqual([], runs)

    def test_the_fast_key_agrees_with_the_one_every_other_producer_uses(self) -> None:
        # A cheaper derivation is only safe while it names the same
        # repository; a producer that drifted would look like a session of
        # its own.
        inner = self.repo / "pkg" / "deep"
        inner.mkdir(parents=True)

        # Directories only: every producer names a directory. Handed a file,
        # `git -C` fails and repository_key degrades to the path it was given,
        # while the walk-up still finds the top level. Nothing passes a file,
        # so the two are not contracted to agree there.
        for start in (self.repo, inner):
            self.assertEqual(
                repository_key(start), repository_key_fast(start), str(start)
            )

    def test_the_fast_key_falls_back_when_there_is_no_git_directory(self) -> None:
        outside = Path(self._directory.name) / "plain"
        outside.mkdir()

        self.assertEqual(repository_key(outside), repository_key_fast(outside))

    def test_the_edit_entry_point_fails_closed_on_an_unsupported_interpreter(
        self,
    ) -> None:
        payload = self._edit_payload("E6", "Edit", str(self.repo / "a.py"))

        with mock.patch("sys.stdin", io.StringIO(payload)):
            with mock.patch("sys.version_info", (3, 8, 5)):
                with self.assertRaises(SystemExit) as refused:
                    session_hooks.edit_entrypoint()

        self.assertEqual(2, refused.exception.code)

    def test_an_unsupported_interpreter_still_fails_closed(self) -> None:
        with self.assertRaises(SystemExit) as refused:
            claude_hook.ensure_supported_python((3, 8, 5))

        self.assertEqual(2, refused.exception.code)

        with mock.patch("sys.stdin", io.StringIO(self._payload())):
            with mock.patch("sys.version_info", (3, 8, 5)):
                with self.assertRaises(SystemExit):
                    session_hooks.start_entrypoint()


if __name__ == "__main__":
    unittest.main()
