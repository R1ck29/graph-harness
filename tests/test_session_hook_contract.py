from __future__ import annotations

import io
import json
import os
import shutil
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
        self._record_edits()
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
        self._record_edits()
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
        self._record_edits()
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
                    },
                ),
                self.home,
            )
            for step in range(5):
                journal.append(
                    journal.record(
                        "edit",
                        client="claude",
                        session_id=session,
                        repo=str(self.repo),
                        tool="Edit",
                        path_id=f"{index}{step:011x}",
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
        # Both thresholds are counted from edit records now: distinct files,
        # or the number of editing tool calls. A session that rewrites one
        # file many times is as much work as one that touches several.
        def bypass(files: int, calls: int, session: str) -> None:
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
                    },
                ),
                self.home,
            )
            for index in range(max(files, calls)):
                journal.append(
                    journal.record(
                        "edit",
                        client="claude",
                        session_id=session,
                        repo=str(self.repo),
                        tool="Edit",
                        path_id=f"{index % max(files, 1):012x}",
                    ),
                    self.home,
                )
            journal.append(
                journal.record(
                    "session_close",
                    client="claude",
                    session_id=session,
                    repo=str(self.repo),
                    snapshot={"git": True, "head": "h", "digest": "after"},
                ),
                self.home,
            )

        bypass(session_hooks.WARN_MIN_FILES, 0, "many-files")
        self.assertIsNotNone(session_hooks.previous_bypass(str(self.repo)))

        bypass(1, session_hooks.WARN_MIN_EDITS, "many-calls")
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
        self._record_edits()
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
        self._record_edits()
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

    def test_shell_work_committed_inside_one_turn_is_now_silent(self) -> None:
        # This expectation is inverted deliberately. It used to assert a
        # report, sized from `git diff` between the two recorded commits. That
        # sizing could not tell authored work from a pull, so anyone running
        # `git pull` was accused; the head term went with it.
        #
        # What remains is the documented blind spot: a shell edit committed
        # with no turn boundary in between leaves every snapshot clean and
        # produces no edit event. It fails towards silence, which is the
        # direction this module errs in everywhere else.
        self._start()
        (self.repo / "tracked.txt").write_text("base\nmore\n" * 20, encoding="utf-8")
        (self.repo / "second.txt").write_text("new\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "work")
        self._end()

        with mock.patch("sys.stderr", io.StringIO()) as reported:
            outcome = self._start(self._payload(session_id="s2"))

        self.assertEqual(0, outcome)
        self.assertEqual("", reported.getvalue())

    def test_a_session_is_not_judged_on_the_records_it_just_wrote(self) -> None:
        # The opening record of the session asking the question is excluded by
        # position, not by identity or timestamp, so a resumed session that
        # reuses its identifier still hears about its own earlier run.
        self._start()
        self._record_edits()
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

    def _record_edits(self, session: str = "s1", count: int = 2) -> None:
        """Record *count* editing tool calls for *session*."""

        for index in range(count):
            payload = json.loads(self._payload(session_id=session))
            payload["tool_name"] = "Edit"
            payload["tool_input"] = {"file_path": str(self.repo / f"f{index}.py")}
            with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
                session_hooks.record_edit()

    def _bypass(self, session: str = "s1") -> None:
        """Leave one finished session that wrote code without the graph.

        Written as edit records rather than as a dirty tree, because that is
        what the signal reads now. The behaviour these scenarios exercise —
        reporting once, choosing between candidates, attribution, concurrency
        — is unchanged; only the way a session is shown to have authored
        something has moved from inference to a record of the act.
        """

        self._start(self._payload(session_id=session))
        (self.repo / "f0.py").write_text("base\nmore\n" * 20, encoding="utf-8")
        (self.repo / "f1.py").write_text("new\n", encoding="utf-8")
        self._record_edits(session)
        self._end(self._payload(session_id=session))

    def test_a_graphctl_run_without_a_client_session_id_still_counts(self) -> None:
        # Only one client exports a session id. Attributing a run by identity
        # left every other way of running graphctl counting for no session,
        # so the session that did use the graph was accused.
        self._start()
        self._record_edits()
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
        """Record one finished session that wrote *files* files, without a hook.

        The snapshots are kept so the record still looks like a real one, but
        the size the scan reads comes from the edit records below them.
        """

        journal.append(
            journal.record(
                "session_open",
                client="claude",
                session_id=session_id,
                repo=repository_key(self.repo),
                snapshot={"git": True, "head": "a" * 40, "digest": f"{digest}-before"},
            ),
            self.home,
        )
        for index in range(files):
            journal.append(
                journal.record(
                    "edit",
                    client="claude",
                    session_id=session_id,
                    repo=repository_key(self.repo),
                    tool="Edit",
                    path_id=f"{index:012x}",
                ),
                self.home,
            )
        for event, snapshot in (
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

    # The forged-head test that stood here is gone with the code it guarded.
    # A recorded head is no longer passed to git at all, which is a stronger
    # guarantee than validating it was; SignalTests
    # test_no_recorded_head_can_reach_a_git_argument_list asserts the absence
    # end to end rather than the validation.

    def test_a_corrupt_count_neither_raises_nor_silences_the_repository(self) -> None:
        records = [
            {
                "event": "session_open",
                "snapshot": {"git": True, "files": "lots", "lines": None},
            },
            {
                "event": "session_close",
                "snapshot": {"git": True, "files": "many", "lines": "several"},
            },
        ]

        self.assertEqual((0, 0), session_hooks.session_size(records))

    def test_nothing_the_tree_reports_can_change_the_size(self) -> None:
        # This replaces a test that checked pre-existing dirt was not charged
        # to a session. The size no longer comes from the tree at all, which
        # is a stronger statement than the one it made: a tree that claims
        # thousands of changed files and lines moves nothing.
        edits = [
            {"event": "edit", "path_id": "aa", "session_id": "s"},
            {"event": "edit", "path_id": "bb", "session_id": "s"},
        ]
        quiet = [{"event": "session_open", "snapshot": {"git": True, "files": 0}}]
        loud = [
            {
                "event": "session_open",
                "snapshot": {"git": True, "digest": "a", "files": 9999, "lines": 9999},
            }
        ]

        self.assertEqual(
            session_hooks.session_size(quiet + edits),
            session_hooks.session_size(loud + edits),
        )
        self.assertEqual((2, 2), session_hooks.session_size(loud + edits))

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
        first, first_outside = session_hooks.path_id(self.repo, str(self.repo / "a.py"))
        again, _ = session_hooks.path_id(self.repo, str(self.repo / "a.py"))
        other, _ = session_hooks.path_id(self.repo, str(self.repo / "b.py"))
        away, away_outside = session_hooks.path_id(self.repo, "/etc/hosts")

        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertRegex(first, r"^[0-9a-f]{12}$")
        self.assertFalse(first_outside)
        self.assertTrue(away_outside)
        self.assertRegex(away, r"^[0-9a-f]{12}$")

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

    def test_a_notebook_edit_names_its_target_differently_and_still_counts(
        self,
    ) -> None:
        # NotebookEdit was in the tool list and in the installer matcher while
        # its edits were dropped, because it carries notebook_path rather than
        # file_path. A notebook-only session reported exactly like one that
        # never edited at all.
        payload = json.loads(self._payload(session_id="N1"))
        payload["tool_name"] = "NotebookEdit"
        payload["tool_input"] = {"notebook_path": str(self.repo / "study.ipynb")}

        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            self.assertEqual(0, session_hooks.record_edit())

        recorded = self._edits()
        self.assertEqual(1, len(recorded))
        self.assertEqual("NotebookEdit", recorded[0]["tool"])
        self.assertRegex(recorded[0]["path_id"], r"^[0-9a-f]{12}$")
        self.assertNotIn("study", self._journal_text())

    def test_an_edit_outside_the_repository_is_flagged_as_outside(self) -> None:
        # A session that writes to a scratchpad has not touched the project.
        # Counting those edits toward the project's thresholds made heavy
        # scratchpad use look like unrecorded work on the repository.
        outside = Path(self._directory.name) / "scratch.md"
        payload = self._edit_payload("O1", "Edit", str(outside))

        with mock.patch("sys.stdin", io.StringIO(payload)):
            self.assertEqual(0, session_hooks.record_edit())

        recorded = self._edits()
        self.assertEqual(1, len(recorded))
        self.assertTrue(recorded[0]["outside"])
        self.assertRegex(recorded[0]["path_id"], r"^[0-9a-f]{12}$")
        self.assertNotIn("scratch", self._journal_text())

    def test_an_edit_inside_the_repository_carries_no_outside_flag(self) -> None:
        payload = self._edit_payload("I1", "Edit", str(self.repo / "a.py"))

        with mock.patch("sys.stdin", io.StringIO(payload)):
            session_hooks.record_edit()

        recorded = self._edits()
        self.assertEqual(1, len(recorded))
        self.assertNotIn("outside", recorded[0])

    def test_outside_edits_do_not_count_toward_the_thresholds(self) -> None:
        # Annotated because the literal mixes an edit record with a boundary
        # record carrying a nested snapshot, and mypy otherwise joins the
        # element type to object. CI type-checks tests/ as well as the
        # package, so an unannotated literal here fails the static job.
        records: list[dict[str, Any]] = [
            {"event": "edit", "session_id": "s", "path_id": "aa", "outside": True},
            {"event": "edit", "session_id": "s", "path_id": "bb", "outside": True},
            {"event": "edit", "session_id": "s", "path_id": "cc", "outside": True},
            {"event": "edit", "session_id": "s", "path_id": "dd", "outside": True},
            {"event": "edit", "session_id": "s", "path_id": "ee", "outside": True},
            {"event": "edit", "session_id": "s", "path_id": "ff", "outside": True},
            {"event": "session_open", "snapshot": {"git": True}},
        ]

        self.assertEqual((0, 0), session_hooks.session_size(records))
        self.assertFalse(session_hooks.edited(records))
        self.assertEqual((False, 0, 0), session_hooks.worth_reporting(records))

    def test_a_session_that_only_edited_outside_is_not_reported(self) -> None:
        self._start()
        outside = Path(self._directory.name) / "elsewhere"
        outside.mkdir()
        for index in range(6):
            # _edit_payload already returns JSON; encoding it again made
            # read_payload see a string rather than an object, so nothing was
            # recorded and the test passed by doing nothing.
            payload = self._edit_payload("s1", "Edit", str(outside / f"note{index}.md"))
            with mock.patch("sys.stdin", io.StringIO(payload)):
                session_hooks.record_edit()
        self._end()

        with mock.patch("sys.stderr", io.StringIO()) as reported:
            outcome = self._start(self._payload(session_id="s2"))

        self.assertEqual(0, outcome)
        self.assertEqual("", reported.getvalue())

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


class SignalTests(unittest.TestCase):
    """A head move says something changed, never who changed it or why.

    Every case here is driven through the real hooks against a real clone of
    a real upstream, because the defect this replaces was invisible to a
    suite that constructed its snapshots by hand.
    """

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.home = Path(self._directory.name) / "home"
        self.home.mkdir()
        self.upstream = Path(self._directory.name) / "upstream"
        self.upstream.mkdir()
        _git(self.upstream, "init", "-q")
        _git(self.upstream, "config", "user.email", "up@example.invalid")
        _git(self.upstream, "config", "user.name", "up")
        (self.upstream / "seed.txt").write_text("seed\n", encoding="utf-8")
        _git(self.upstream, "add", "-A")
        _git(self.upstream, "commit", "-qm", "seed")
        self.clone = Path(self._directory.name) / "clone"
        subprocess.run(
            ("git", "clone", "-q", str(self.upstream), str(self.clone)),
            capture_output=True,
            check=True,
        )
        _git(self.clone, "config", "user.email", "me@example.invalid")
        _git(self.clone, "config", "user.name", "me")
        self._environment = mock.patch.dict(
            os.environ,
            {
                "GRAPH_HARNESS_HOME": str(self.home),
                "CLAUDE_PROJECT_DIR": str(self.clone),
                "CLAUDE_CODE_SESSION_ID": "s1",
            },
        )
        self._environment.start()
        self.addCleanup(self._environment.stop)

    def _payload(self, session: str) -> str:
        return json.dumps({"session_id": session, "cwd": str(self.clone)})

    def _open(self, session: str) -> None:
        with mock.patch("sys.stdin", io.StringIO(self._payload(session))):
            with mock.patch("sys.stderr", io.StringIO()):
                session_hooks.session_start()

    def _turn(self, session: str) -> None:
        with mock.patch("sys.stdin", io.StringIO(self._payload(session))):
            session_hooks.observe("turn_end", json.loads(self._payload(session)))

    def _close(self, session: str) -> None:
        with mock.patch("sys.stdin", io.StringIO(self._payload(session))):
            session_hooks.session_end()

    def _edit(self, session: str, name: str) -> None:
        payload = json.loads(self._payload(session))
        payload["tool_name"] = "Edit"
        payload["tool_input"] = {"file_path": str(self.clone / name)}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            session_hooks.record_edit()

    def _write(self, repo: Path, *names: str) -> None:
        for name in names:
            (repo / name).write_text(
                "".join(f"line {index}\n" for index in range(40)), encoding="utf-8"
            )

    def _found(self) -> dict[str, Any] | None:
        return session_hooks.previous_bypass(repository_key(self.clone))

    def test_a_pull_is_never_reported(self) -> None:
        self._open("puller")
        self._write(self.upstream, "a.txt", "b.txt")
        _git(self.upstream, "add", "-A")
        _git(self.upstream, "commit", "-qm", "upstream work")
        _git(self.clone, "pull", "-q", "--ff-only")
        self._close("puller")

        self.assertIsNone(self._found())

    def test_a_checkout_is_never_reported(self) -> None:
        _git(self.clone, "checkout", "-q", "-b", "feature")
        self._write(self.clone, "a.txt", "b.txt")
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-qm", "feature work")
        _git(self.clone, "checkout", "-q", "master")
        self._open("checker")
        _git(self.clone, "checkout", "-q", "feature")
        self._close("checker")

        self.assertIsNone(self._found())

    def test_a_hard_reset_is_never_reported(self) -> None:
        self._write(self.clone, "a.txt", "b.txt")
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-qm", "local work")
        self._open("resetter")
        _git(self.clone, "reset", "-q", "--hard", "HEAD~1")
        self._close("resetter")

        self.assertIsNone(self._found())

    def test_a_commit_by_someone_else_mid_session_is_never_reported(self) -> None:
        self._open("bystander")
        _git(self.clone, "config", "user.email", "other@example.invalid")
        self._write(self.clone, "a.txt", "b.txt")
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-qm", "another terminal")
        self._close("bystander")

        self.assertIsNone(self._found())

    def test_an_editing_tool_call_is_reported(self) -> None:
        self._open("editor")
        self._write(self.clone, "a.py", "b.py")
        self._edit("editor", "a.py")
        self._edit("editor", "b.py")
        self._close("editor")

        found = self._found()

        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual("editor", found["session_id"])
        self.assertEqual(2, found["changed_files"])

    def test_an_edit_is_reported_even_though_the_work_was_committed(self) -> None:
        # The tree is clean at both ends. Only the edit record and the
        # intermediate turn boundary say anything happened.
        self._open("committer")
        self._write(self.clone, "a.py", "b.py")
        self._edit("committer", "a.py")
        self._edit("committer", "b.py")
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-qm", "my own work")
        self._close("committer")

        found = self._found()

        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual("committer", found["session_id"])
        # Counted from the edit records, so committing afterwards cannot
        # change the number.
        self.assertEqual(2, found["changed_files"])
        self.assertEqual(2, found["edits"])

    # Eight tests stood here and are gone with the term they pinned. Each
    # asserted something the working-tree comparison was supposed to conclude:
    # that a shell edit across two turns is reported, that a stash pop and a
    # git apply are false positives, that deleting junk while authoring is
    # still authoring, that a created file is sized in lines, that a leftover
    # REBASE_HEAD must not silence a shell session. Six review rounds were
    # spent making those true and none of them ever was for long.
    #
    # The tree no longer concludes anything, so every one of those cases has
    # the same answer now and it is stated once, in
    # test_no_git_operation_can_produce_a_warning and
    # test_a_shell_only_session_is_unattributed_not_read_only. What the
    # deletions cost is real and is pinned by the second of those: shell
    # authoring is no longer reported.

    def test_a_shell_edit_committed_inside_one_turn_is_the_known_blind_spot(
        self,
    ) -> None:
        # Documented in docs/conformance.md. Pinned here so it cannot change
        # unnoticed. It errs towards silence, which is the direction this
        # module errs in everywhere else.
        self._open("hidden")
        self._write(self.clone, "a.txt", "b.txt")
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-qm", "shell work")
        self._close("hidden")

        self.assertIsNone(self._found())

    def test_a_read_is_not_an_edit(self) -> None:
        # The producer accepted any tool carrying a file path, so a Read was
        # recorded as an edit. Recording a read as authored work is the same
        # failure class as mistaking a pull for it.
        payload = json.loads(self._payload("reader"))
        payload["tool_name"] = "Read"
        payload["tool_input"] = {"file_path": str(self.clone / "seed.txt")}

        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            self.assertEqual(0, session_hooks.record_edit())

        self.assertEqual(
            [], [e for e in journal.read(self.home) if e.get("event") == "edit"]
        )

    def _verdict(self, session: str) -> str:
        from agent_harness import conformance

        found = [
            item
            for item in conformance.report(self.home, include_codex=False)["sessions"]
            if item["session_id"] == session
        ]
        self.assertEqual(1, len(found), found)
        return str(found[0]["verdict"])

    def test_a_conflicted_pull_is_never_reported(self) -> None:
        # The head term was removed and the dirty-tree term inherited the same
        # defect: a merge git could not complete leaves conflict markers in the
        # tree that nobody authored.
        (self.clone / "seed.txt").write_text("local\n" * 40, encoding="utf-8")
        _git(self.clone, "commit", "-qam", "local work")
        (self.upstream / "seed.txt").write_text("theirs\n" * 40, encoding="utf-8")
        _git(self.upstream, "commit", "-qam", "their work")
        _git(self.clone, "fetch", "-q")
        self._open("conflicted")
        subprocess.run(
            ("git", "-C", str(self.clone), "pull", "--no-rebase", "-q"),
            capture_output=True,
        )
        self._turn("conflicted")
        self._close("conflicted")

        self.assertIsNone(self._found())
        self.assertEqual("unattributed", self._verdict("conflicted"))

    def test_a_conflicted_pull_that_is_aborted_is_never_reported(self) -> None:
        # The session ends with a byte-identical tree and an unmoved HEAD. It
        # was still accused, because the size is a maximum across boundaries
        # and one boundary fell while the merge was open.
        (self.clone / "seed.txt").write_text("local\n" * 40, encoding="utf-8")
        _git(self.clone, "commit", "-qam", "local work")
        (self.upstream / "seed.txt").write_text("theirs\n" * 40, encoding="utf-8")
        _git(self.upstream, "commit", "-qam", "their work")
        _git(self.clone, "fetch", "-q")
        self._open("aborter")
        subprocess.run(
            ("git", "-C", str(self.clone), "pull", "--no-rebase", "-q"),
            capture_output=True,
        )
        self._turn("aborter")
        _git(self.clone, "merge", "--abort")
        self._close("aborter")

        self.assertEqual(
            "",
            subprocess.run(
                ("git", "-C", str(self.clone), "status", "--porcelain"),
                capture_output=True,
                text=True,
            ).stdout,
        )
        self.assertIsNone(self._found())
        self.assertEqual("unattributed", self._verdict("aborter"))

    def test_a_submodule_update_is_never_reported(self) -> None:
        far = Path(self._directory.name) / "far"
        far.mkdir()
        _git(far, "init", "-q")
        _git(far, "config", "user.email", "far@example.invalid")
        _git(far, "config", "user.name", "far")
        (far / "one.txt").write_text("one\n", encoding="utf-8")
        _git(far, "add", "-A")
        _git(far, "commit", "-qm", "one")
        subprocess.run(
            (
                "git",
                "-C",
                str(self.clone),
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                "-q",
                str(far),
                "vendor",
            ),
            capture_output=True,
            check=True,
        )
        _git(self.clone, "commit", "-qam", "add submodule")
        (far / "one.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
        _git(far, "commit", "-qam", "far moves on")

        self._open("submoduler")
        subprocess.run(
            (
                "git",
                "-C",
                str(self.clone),
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "update",
                "--remote",
                "-q",
            ),
            capture_output=True,
        )
        self._turn("submoduler")
        self._close("submoduler")

        self.assertIsNone(self._found())
        self.assertEqual("unattributed", self._verdict("submoduler"))

    def test_discarding_dirt_that_was_there_first_is_not_work(self) -> None:
        # git checkout -- ., git clean -fd and git reset --hard all move the
        # tree without adding anything. The hook stayed silent only because
        # the size was below the threshold, while the report called it bypass.
        self._write(self.clone, "a.txt", "b.txt")
        self._open("cleaner")
        _git(self.clone, "checkout", "--", ".")
        subprocess.run(
            ("git", "-C", str(self.clone), "clean", "-fdq"), capture_output=True
        )
        self._turn("cleaner")
        self._close("cleaner")

        self.assertIsNone(self._found())
        self.assertEqual("unattributed", self._verdict("cleaner"))

    def test_a_hard_reset_over_pre_session_dirt_is_not_work(self) -> None:
        self._write(self.clone, "a.txt", "b.txt")
        _git(self.clone, "add", "-A")
        self._open("resetter2")
        _git(self.clone, "reset", "-q", "--hard")
        self._turn("resetter2")
        self._close("resetter2")

        self.assertIsNone(self._found())
        self.assertEqual("unattributed", self._verdict("resetter2"))

    def test_work_committed_before_every_turn_boundary_is_invisible(self) -> None:
        # The blind spot is wider than one turn: committing before each
        # boundary hides a whole session's shell-written work, because every
        # snapshot is then clean. Pinned so the documented width stays true.
        self._open("hider")
        for round_number in range(3):
            self._write(self.clone, f"r{round_number}_a.txt", f"r{round_number}_b.txt")
            _git(self.clone, "add", "-A")
            _git(self.clone, "commit", "-qm", f"round {round_number}")
            self._turn("hider")
        self._close("hider")

        self.assertIsNone(self._found())

    def test_edits_under_an_ignored_path_are_invisible(self) -> None:
        # Outside the documented blind spot entirely: git never reports these
        # paths, so no snapshot can see them.
        (self.clone / ".gitignore").write_text("build/\n", encoding="utf-8")
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-qm", "ignore build")
        (self.clone / "build").mkdir()
        self._open("ignorer")
        self._write(self.clone / "build", "a.txt", "b.txt")
        self._turn("ignorer")
        self._close("ignorer")

        self.assertIsNone(self._found())

    def _git_dir(self) -> Path:
        return self.clone / ".git"

    def test_a_leftover_rebase_head_does_not_silence_an_editing_tool_session(
        self,
    ) -> None:
        (self._git_dir() / "REBASE_HEAD").write_text("a" * 40, encoding="utf-8")
        self._open("rebased2")
        self._write(self.clone, "a.py", "b.py")
        self._edit("rebased2", "a.py")
        self._edit("rebased2", "b.py")
        self._close("rebased2")

        self.assertIsNotNone(self._found())

    def test_a_rebase_actually_in_progress_is_not_judged(self) -> None:
        # The directories are what git creates while a rebase is open and
        # removes when it finishes or is aborted.
        for marker in ("rebase-merge", "rebase-apply"):
            with self.subTest(marker=marker):
                directory = self._git_dir() / marker
                directory.mkdir()
                session = f"rebasing-{marker}"
                self._open(session)
                self._write(self.clone, "a.txt", "b.txt")
                self._turn(session)
                self._close(session)

                self.assertEqual("unattributed", self._verdict(session))
                shutil.rmtree(directory)

    def _other_git(self) -> str | None:
        """Find a git other than the one first on PATH, if the system has one.

        The REBASE_HEAD defect survived a green suite because git 2.21 is
        first on PATH here and removes the file, while git 2.50 does not. A
        suite that exercises one git cannot see a difference between gits.
        """

        found = subprocess.run(
            ("which", "-a", "git"), capture_output=True, text=True, check=False
        ).stdout.split()
        primary = found[0] if found else None
        for candidate in found[1:]:
            if candidate != primary and Path(candidate).exists():
                return candidate
        return None

    def test_a_completed_rebase_does_not_silence_the_next_session(self) -> None:
        # End to end under a second git, because the marker git leaves behind
        # differs by version and that is what hid the defect. Skipped rather
        # than faked when the machine has only one git.
        other = self._other_git()
        if other is None:
            self.skipTest("only one git on this system")

        def run(*arguments: str) -> None:
            subprocess.run(
                (other, "-C", str(self.clone), *arguments),
                capture_output=True,
                check=False,
            )

        run("checkout", "-q", "-b", "side")
        (self.clone / "seed.txt").write_text("side" + chr(10), encoding="utf-8")
        run("commit", "-qam", "side")
        run("checkout", "-q", "master")
        (self.clone / "seed.txt").write_text("main" + chr(10), encoding="utf-8")
        run("commit", "-qam", "main")
        run("checkout", "-q", "side")
        run("rebase", "master")
        (self.clone / "seed.txt").write_text("resolved" + chr(10), encoding="utf-8")
        run("add", "seed.txt")
        subprocess.run(
            (other, "-C", str(self.clone), "rebase", "--continue"),
            capture_output=True,
            check=False,
            env={**os.environ, "GIT_EDITOR": "true"},
        )
        self.assertFalse((self.clone / ".git/rebase-merge").exists())
        self.assertFalse((self.clone / ".git/rebase-apply").exists())

        self._open("after-rebase")
        self._write(self.clone, "a.py", "b.py")
        self._edit("after-rebase", "a.py")
        self._edit("after-rebase", "b.py")
        self._turn("after-rebase")
        self._close("after-rebase")

        found = self._found()

        self.assertIsNotNone(found, f"silenced under {other}")
        assert found is not None
        self.assertEqual("after-rebase", found["session_id"])

    def test_no_git_operation_can_produce_a_warning(self) -> None:
        # The whole family, in one table, driven through the real hooks. Six
        # review rounds were spent finding these one at a time; none of them
        # produces an edit record, so none of them can be reported.
        scenarios = {
            "stash-pop": self._stash_pop,
            "apply": self._apply_patch,
            "cherry-pick-n": self._cherry_pick_no_commit,
            "worktree-add": self._worktree_add,
            "checkout-path": self._checkout_path,
            "leftover-rebase-head": self._leftover_rebase_head,
            "build-output": self._build_output,
        }
        for name, action in scenarios.items():
            with self.subTest(scenario=name):
                session = f"op-{name}"
                self._open(session)
                action()
                self._turn(session)
                self._close(session)

                self.assertIsNone(self._found(), name)
                self.assertNotEqual("bypass", self._verdict(session), name)

    def _stash_pop(self) -> None:
        self._write(self.clone, "s1.txt", "s2.txt")
        _git(self.clone, "add", "-A")
        _git(self.clone, "stash", "-q")
        _git(self.clone, "stash", "pop", "-q")

    def _apply_patch(self) -> None:
        patch = Path(self._directory.name) / "p.patch"
        patch.write_text(
            "diff --git a/p.txt b/p.txt"
            + chr(10)
            + "new file mode 100644"
            + chr(10)
            + "--- /dev/null"
            + chr(10)
            + "+++ b/p.txt"
            + chr(10)
            + "@@ -0,0 +1,2 @@"
            + chr(10)
            + "+one"
            + chr(10)
            + "+two"
            + chr(10),
            encoding="utf-8",
        )
        subprocess.run(
            ("git", "-C", str(self.clone), "apply", str(patch)),
            capture_output=True,
            check=False,
        )

    def _cherry_pick_no_commit(self) -> None:
        _git(self.clone, "checkout", "-q", "-b", "pick-source")
        self._write(self.clone, "picked.txt")
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-qm", "picked")
        _git(self.clone, "checkout", "-q", "master")
        subprocess.run(
            ("git", "-C", str(self.clone), "cherry-pick", "-n", "pick-source"),
            capture_output=True,
            check=False,
        )

    def _worktree_add(self) -> None:
        subprocess.run(
            (
                "git",
                "-C",
                str(self.clone),
                "worktree",
                "add",
                "-q",
                "inner",
                "-b",
                "wt",
            ),
            capture_output=True,
            check=False,
        )

    def _checkout_path(self) -> None:
        _git(self.clone, "checkout", "-q", "-b", "other")
        self._write(self.clone, "fromother.txt")
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-qm", "other")
        _git(self.clone, "checkout", "-q", "master")
        _git(self.clone, "checkout", "other", "--", "fromother.txt")

    def _leftover_rebase_head(self) -> None:
        (self.clone / ".git" / "REBASE_HEAD").write_text("a" * 40, encoding="utf-8")
        self._write(self.clone, "after.txt")

    def _build_output(self) -> None:
        build = self.clone / "build"
        build.mkdir()
        self._write(build, "out1.js", "out2.js")

    def test_a_shell_only_session_is_unattributed_not_read_only(self) -> None:
        # Not reported, because nothing says this session's agent wrote it.
        # Not read_only either, because something plainly changed. The verdict
        # says what is true: the change could not be attributed.
        self._open("sheller-only")
        self._write(self.clone, "a.txt", "b.txt")
        self._turn("sheller-only")
        self._close("sheller-only")

        self.assertIsNone(self._found())
        self.assertEqual("unattributed", self._verdict("sheller-only"))

    def test_a_session_that_edited_is_reported(self) -> None:
        self._open("author")
        self._write(self.clone, "a.py", "b.py")
        self._edit("author", "a.py")
        self._edit("author", "b.py")
        self._close("author")

        found = self._found()

        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(2, found["changed_files"])
        self.assertEqual("bypass", self._verdict("author"))

    def test_the_working_tree_decides_nothing(self) -> None:
        # Shown rather than asserted: every session's verdict is recomputed
        # with the snapshots stripped out of the journal, and nothing moves.
        # This covers the verdict half only — the warning half is covered by
        # test_no_git_operation_can_produce_a_warning and
        # test_a_shell_only_session_is_unattributed_not_read_only, which a
        # review confirmed catch a tree-driven warning in thirteen places
        # while this test alone does not.
        from agent_harness import conformance

        self._open("author2")
        self._write(self.clone, "a.py")
        self._edit("author2", "a.py")
        self._close("author2")
        self._open("sheller2")
        self._write(self.clone, "c.txt", "d.txt")
        self._close("sheller2")

        with_tree = {
            item["session_id"]: item["verdict"]
            for item in conformance.report(self.home, include_codex=False)["sessions"]
        }
        stripped = [
            {key: value for key, value in entry.items() if key != "snapshot"}
            for entry in journal.read(self.home)
        ]
        with mock.patch.object(journal, "read", return_value=iter(stripped)):
            without_tree = {
                item["session_id"]: item["verdict"]
                for item in conformance.report(self.home, include_codex=False)[
                    "sessions"
                ]
            }

        self.assertEqual(with_tree, without_tree)
        self.assertEqual("bypass", with_tree["author2"])

    def test_commit_sizing_is_gone(self) -> None:
        self.assertFalse(hasattr(worktree, "committed_size"))
        self.assertFalse(hasattr(worktree, "COMMIT_NAME"))

    def test_no_recorded_head_can_reach_a_git_argument_list(self) -> None:
        self._open("forger")
        journal.append(
            journal.record(
                "turn_end",
                client="claude",
                session_id="forger",
                repo=repository_key(self.clone),
                snapshot={
                    "git": True,
                    "head": "--output=/tmp/forged",
                    "digest": "different",
                    "files": 9,
                    "lines": 90,
                    "degraded": False,
                },
            ),
            self.home,
        )
        self._close("forger")
        runs: list[tuple[str, ...]] = []
        real_run = subprocess.run

        def counted(arguments: Any, **options: Any) -> Any:
            runs.append(tuple(arguments))
            return real_run(arguments, **options)

        with mock.patch("subprocess.run", counted):
            self._found()

        self.assertEqual([], [run for run in runs if "--output=/tmp/forged" in run])
        self.assertFalse(Path("/tmp/forged").exists())


if __name__ == "__main__":
    unittest.main()
