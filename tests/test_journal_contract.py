from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import cast
from unittest import mock

from agent_harness import journal, worktree
from agent_harness.errors import HarnessError
from agent_harness.paths import repository_key, user_data_path

REPOSITORY = Path(__file__).resolve().parents[1]


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(repo), *arguments),
        capture_output=True,
        text=True,
        check=True,
    )


class UserDataPathTests(unittest.TestCase):
    """Nothing else in the library writes outside the workspace."""

    def test_path_stays_under_the_install_directory_of_the_selected_home(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            resolved = user_data_path("journal/2026-08.jsonl", home)

            self.assertEqual(
                Path(home).resolve()
                / ".local/share/graph-engineering-agent-harness"
                / "journal/2026-08.jsonl",
                resolved,
            )

    def test_path_escaping_the_home_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaisesRegex(HarnessError, "escapes the selected home"):
                user_data_path("../../elsewhere", home)

    def test_link_like_component_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            root = Path(home) / ".local/share/graph-engineering-agent-harness"
            root.mkdir(parents=True)
            outside = Path(home) / "outside"
            outside.mkdir()
            try:
                (root / "journal").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")

            with self.assertRaisesRegex(HarnessError, "link-like"):
                user_data_path("journal/2026-08.jsonl", home)


class RepositoryKeyTests(unittest.TestCase):
    """Three producers derive this key separately and match on it exactly."""

    def test_a_path_that_cannot_be_derived_degrades_rather_than_raises(self) -> None:
        # Every caller records rather than acts, so a key that cannot be
        # derived must degrade here rather than rely on each of them wrapping
        # the call. An embedded null byte is rejected before git sees it.
        #
        # Compared against the platform's own rendering of the path, not
        # against the POSIX spelling. The contract is that the call degrades
        # instead of raising; the separator it comes back with is incidental,
        # and asserting the input verbatim failed on Windows, where the same
        # path renders with backslashes.
        value = "/tmp/a\x00b"

        derived = repository_key(value)

        # The property, not a rendering. Asserting the input verbatim failed
        # on Windows, which uses backslashes; asserting `str(Path(value))`
        # failed there too, because the key is absolute and Windows prefixes
        # the drive. What the callers need is that this returns a usable
        # string instead of raising, and that it still identifies that path.
        self.assertIsInstance(derived, str)
        self.assertTrue(derived)
        self.assertIn("\x00", derived)
        self.assertTrue(derived.endswith(Path(value).name))

    def test_a_subdirectory_and_the_root_share_one_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            nested = repo / "package" / "inner"
            nested.mkdir(parents=True)
            _git(repo, "init", "-q")

            self.assertEqual(repository_key(repo), repository_key(nested))

    def test_a_directory_outside_git_still_yields_a_stable_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plain = Path(directory) / "plain"
            plain.mkdir()

            self.assertEqual(str(plain.resolve()), repository_key(plain))


class JournalWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.home = self._directory.name

    def test_record_is_written_to_the_month_named_by_its_timestamp(self) -> None:
        entry = journal.record("session_open", client="claude", session_id="s1")

        self.assertTrue(journal.append(entry, self.home))

        month = journal.month_path(entry["ts"], self.home)
        self.assertEqual(f"{entry['ts'][:7]}.jsonl", month.name)
        stored = json.loads(month.read_text(encoding="utf-8").strip())
        self.assertEqual("session_open", stored["event"])
        self.assertEqual(journal.SCHEMA_VERSION, stored["schema_version"])

    def test_unknown_event_is_refused_at_construction(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown journal event"):
            journal.record("something_else")

    def test_over_long_record_sheds_fields_instead_of_being_dropped(self) -> None:
        entry = journal.record(
            "session_open",
            client="claude",
            session_id="s1",
            snapshot={"digest": "d" * (journal.MAX_JOURNAL_LINE_BYTES * 2)},
        )

        line = journal.render(entry)

        self.assertIsNotNone(line)
        assert line is not None
        self.assertLessEqual(len(line.encode("utf-8")), journal.MAX_JOURNAL_LINE_BYTES)
        stored = json.loads(line)
        self.assertNotIn("snapshot", stored)
        self.assertEqual(["snapshot"], stored["shed"])
        self.assertEqual("s1", stored["session_id"])

    def test_write_failure_returns_false_instead_of_raising(self) -> None:
        entry = journal.record("turn_end", client="claude", session_id="s1")

        with mock.patch("agent_harness.journal.os.open", side_effect=OSError("full")):
            self.assertFalse(journal.append(entry, self.home))

    def test_unreadable_journal_directory_yields_no_records(self) -> None:
        with mock.patch(
            "agent_harness.journal.journal_directory", side_effect=OSError("gone")
        ):
            self.assertEqual([], list(journal.read(self.home)))

    def test_truncated_line_does_not_hide_the_records_around_it(self) -> None:
        first = journal.record("session_open", client="claude", session_id="s1")
        journal.append(first, self.home)
        month = journal.month_path(first["ts"], self.home)
        with month.open("a", encoding="utf-8") as handle:
            handle.write('{"event": "session_close", "cli')
        journal.append(
            journal.record("turn_end", client="claude", session_id="s1"), self.home
        )

        events = [entry["event"] for entry in journal.read(self.home)]

        self.assertEqual(["session_open", "turn_end"], events)

    def test_prune_keeps_only_the_most_recent_months(self) -> None:
        directory = journal.journal_directory(self.home)
        directory.mkdir(parents=True)
        for month in ("2026-01", "2026-02", "2026-03", "2026-04"):
            (directory / f"{month}.jsonl").write_text("", encoding="utf-8")

        removed = journal.prune(2, self.home)

        self.assertEqual(["2026-01.jsonl", "2026-02.jsonl"], removed)
        self.assertEqual(
            ["2026-03.jsonl", "2026-04.jsonl"],
            sorted(path.name for path in directory.glob("*.jsonl")),
        )

    def test_prune_refuses_a_retention_bound_below_one_month(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 1"):
            journal.prune(0, self.home)

    def test_a_foreign_jsonl_file_neither_counts_nor_is_deleted(self) -> None:
        directory = journal.journal_directory(self.home)
        directory.mkdir(parents=True)
        for name in ("2025-12.jsonl", "2026-01.jsonl", "2026-02.jsonl", "notes.jsonl"):
            (directory / name).write_text("", encoding="utf-8")

        removed = journal.prune(3, self.home)

        self.assertEqual([], removed)
        self.assertTrue((directory / "2025-12.jsonl").exists())
        self.assertTrue((directory / "notes.jsonl").exists())

    def test_a_foreign_jsonl_file_is_not_read_as_history(self) -> None:
        directory = journal.journal_directory(self.home)
        directory.mkdir(parents=True)
        (directory / "notes.jsonl").write_text(
            '{"event": "session_open", "session_id": "forged"}\n', encoding="utf-8"
        )

        self.assertEqual([], list(journal.read(self.home)))

    def test_a_record_naming_an_unknown_event_is_still_readable(self) -> None:
        directory = journal.journal_directory(self.home)
        directory.mkdir(parents=True)
        (directory / "2026-08.jsonl").write_text(
            '{"schema_version": 2, "event": "tool_use", "session_id": "s1"}\n',
            encoding="utf-8",
        )

        events = [entry["event"] for entry in journal.read(self.home)]

        self.assertEqual(["tool_use"], events)

    def test_prune_reports_nothing_when_the_directory_is_unusable(self) -> None:
        with mock.patch(
            "agent_harness.journal.journal_directory",
            side_effect=HarnessError("link-like"),
        ):
            self.assertEqual([], journal.prune(2, self.home))
            self.assertEqual(0, journal.total_bytes(self.home))

    def test_a_record_that_is_not_a_mapping_is_refused_without_raising(self) -> None:
        self.assertFalse(journal.append([("event", "turn_end")], self.home))  # type: ignore[arg-type]

    def test_an_unresolvable_home_is_refused_without_raising(self) -> None:
        # The package points GRAPH_HARNESS_HOME at a throwaway directory, so
        # this case has to clear it to reach the fallback it is about.
        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch(
                "agent_harness.paths.Path.home", side_effect=RuntimeError("no home")
            ),
        ):
            os.environ.pop("GRAPH_HARNESS_HOME", None)
            self.assertFalse(
                journal.append(journal.record("turn_end", session_id="s1"))
            )
            self.assertEqual([], journal.prune(2))

    def test_concurrent_appends_lose_no_line_and_interleave_none(self) -> None:
        writers = 8
        per_writer = 60
        with ProcessPoolExecutor(max_workers=writers) as pool:
            failures = list(
                pool.map(
                    _append_many,
                    [(self.home, index, per_writer) for index in range(writers)],
                )
            )

        self.assertEqual([0] * writers, failures)
        entries = list(journal.read(self.home))
        self.assertEqual(writers * per_writer, len(entries))
        self.assertEqual(
            writers * per_writer,
            len({(entry["session_id"], entry["command"]) for entry in entries}),
        )


def _append_many(arguments: tuple[str, int, int]) -> int:
    home, writer, count = arguments
    failures = 0
    for index in range(count):
        written = journal.append(
            journal.record(
                "graph_transition",
                client="test",
                session_id=f"writer-{writer}",
                command=f"command-{index}",
                repo="x" * (writer * 41 % 300),
            ),
            home,
        )
        if not written:
            failures += 1
    return failures


class WorktreeSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.repo = Path(self._directory.name) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "harness@example.invalid")
        _git(self.repo, "config", "user.name", "harness")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "init")

    def test_further_edit_to_an_already_modified_file_is_visible(self) -> None:
        tracked = self.repo / "tracked.txt"
        tracked.write_text("base\nmore\n", encoding="utf-8")
        before = worktree.snapshot(self.repo)
        tracked.write_text("base\nmore\nagain\n", encoding="utf-8")

        after = worktree.snapshot(self.repo)

        self.assertTrue(worktree.dirty_changed(before, after))

    def test_edit_to_an_existing_untracked_file_is_visible(self) -> None:
        untracked = self.repo / "scratch.txt"
        untracked.write_text("one\n", encoding="utf-8")
        before = worktree.snapshot(self.repo)
        untracked.write_text("one\ntwo\n", encoding="utf-8")

        after = worktree.snapshot(self.repo)

        self.assertTrue(worktree.dirty_changed(before, after))

    def test_committing_the_work_is_visible_though_the_tree_returns_clean(self) -> None:
        (self.repo / "tracked.txt").write_text("base\nwork\n", encoding="utf-8")
        before = worktree.snapshot(self.repo)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "work")

        after = worktree.snapshot(self.repo)

        self.assertNotEqual(before["head"], after["head"])
        self.assertTrue(worktree.dirty_changed(before, after))

    def test_a_clean_repository_reports_no_change(self) -> None:
        before = worktree.snapshot(self.repo)

        after = worktree.snapshot(self.repo)

        self.assertFalse(worktree.dirty_changed(before, after))

    def test_directory_outside_git_is_reported_as_such_not_as_clean(self) -> None:
        outside = Path(self._directory.name) / "plain"
        outside.mkdir()

        observed = worktree.snapshot(outside)

        self.assertFalse(observed["git"])
        self.assertIsNone(observed["digest"])
        self.assertFalse(worktree.dirty_changed(observed, worktree.snapshot(outside)))

    def test_unavailable_git_degrades_instead_of_raising(self) -> None:
        with mock.patch(
            "agent_harness.worktree.subprocess.run", side_effect=OSError("no git")
        ):
            observed = worktree.snapshot(self.repo)

        self.assertFalse(observed["git"])

    def test_the_snapshot_is_the_same_whatever_the_locale_says(self) -> None:
        # `_git` names its encoding instead of inheriting the locale's,
        # because git emits paths as UTF-8 bytes on every platform and the
        # Windows locale is not UTF-8. I declared this unreachable from a
        # POSIX test on the grounds that a POSIX locale is already UTF-8 —
        # true only of the default, and a test controls the environment of
        # any subprocess it spawns.
        #
        # Driven through the real snapshot in two locales rather than by
        # mocking the decode: the digest must not depend on which one.
        # Without the named encoding the ISO-8859-1 run decodes the path as
        # mojibake, so the untracked file is never found, its contents are
        # never hashed, and the changed-line count collapses to zero.
        (self.repo / "café.txt").write_text("one\n", encoding="utf-8")
        program = (
            "import json, sys;"
            f"sys.path.insert(0, {str(REPOSITORY)!r});"
            "from agent_harness import worktree;"
            f"print(json.dumps(worktree.snapshot({str(self.repo)!r})))"
        )

        digests = {}
        for label, locale_name in (
            ("utf8", "en_US.UTF-8"),
            ("latin1", "en_US.ISO8859-1"),
        ):
            done = subprocess.run(
                [sys.executable, "-X", "utf8=0", "-c", program],
                capture_output=True,
                text=True,
                timeout=60,
                env={
                    **os.environ,
                    "LC_ALL": locale_name,
                    "PYTHONUTF8": "0",
                    "PYTHONCOERCECLOCALE": "0",
                },
            )
            self.assertEqual(0, done.returncode, done.stderr)
            digests[label] = json.loads(done.stdout)

        self.assertEqual(digests["utf8"]["digest"], digests["latin1"]["digest"])
        self.assertEqual(digests["utf8"]["lines"], digests["latin1"]["lines"])
        self.assertGreater(digests["utf8"]["lines"], 0, "the file was not seen at all")

    def test_edit_to_an_untracked_file_with_a_quoted_name_is_visible(self) -> None:
        # Git C-quotes non-ASCII paths under its default core.quotepath, so a
        # newline-form parser resolves an escape string and misses the edit.
        untracked = self.repo / "メモ.txt"
        untracked.write_text("one\n", encoding="utf-8")
        before = worktree.snapshot(self.repo)
        untracked.write_text("one\ntwo\nthree\n", encoding="utf-8")

        after = worktree.snapshot(self.repo)

        self.assertTrue(worktree.dirty_changed(before, after))

    def test_edit_to_a_staged_file_before_the_first_commit_is_visible(self) -> None:
        unborn = Path(self._directory.name) / "unborn"
        unborn.mkdir()
        _git(unborn, "init", "-q")
        _git(unborn, "config", "user.email", "harness@example.invalid")
        _git(unborn, "config", "user.name", "harness")
        staged = unborn / "a.txt"
        staged.write_text("one\n", encoding="utf-8")
        _git(unborn, "add", "a.txt")
        before = worktree.snapshot(unborn)
        staged.write_text("one\ntwo\n", encoding="utf-8")

        after = worktree.snapshot(unborn)

        self.assertTrue(before["git"])
        self.assertIsNone(before["head"])
        self.assertFalse(before["degraded"])
        self.assertTrue(worktree.dirty_changed(before, after))

    def test_a_diff_git_cannot_produce_is_reported_rather_than_hidden(self) -> None:
        real = worktree._git

        def failing(repo: Path, *arguments: str, **options: object) -> str | None:
            if arguments[:1] == ("diff",) and "--shortstat" not in arguments:
                return None
            return real(repo, *arguments, **options)  # type: ignore[arg-type]

        with mock.patch("agent_harness.worktree._git", side_effect=failing):
            observed = worktree.snapshot(self.repo)

        self.assertTrue(observed["git"])
        self.assertTrue(observed["degraded"])

    def test_a_worktree_side_rename_does_not_inflate_the_file_count(self) -> None:
        # Porcelain v1 can put the rename letter in either column, and both
        # forms carry an origin path. Consuming only the index-column form
        # left the origin to be parsed as a third, garbage entry.
        entries = worktree._status_entries("A  a.txt\0 R b.txt\0a.txt\0")

        self.assertEqual([(" R", "b.txt\0a.txt"), ("A ", "a.txt")], entries)

    def test_a_month_file_cannot_be_named_with_non_ascii_digits(self) -> None:
        self.assertIsNone(journal.MONTH_NAME.fullmatch("٢٠٢٦-٠١.jsonl"))
        self.assertIsNotNone(journal.MONTH_NAME.fullmatch("2026-01.jsonl"))

    def test_a_rename_is_visible(self) -> None:
        before = worktree.snapshot(self.repo)
        _git(self.repo, "mv", "tracked.txt", "renamed.txt")

        self.assertTrue(worktree.dirty_changed(before, worktree.snapshot(self.repo)))

    def test_a_created_file_contributes_its_lines(self) -> None:
        # Untracked content never reaches git diff, so a session that created
        # files was sized at zero lines and announced as changing none.
        before = worktree.snapshot(self.repo)
        (self.repo / "second.txt").write_text("a\nb\nc\n", encoding="utf-8")

        after = worktree.snapshot(self.repo)

        self.assertEqual(before["lines"] + 3, after["lines"])

    def test_a_snapshot_carries_context_and_no_decision_fields(self) -> None:
        # Everything the snapshot once carried for the sake of a decision is
        # gone with the decision: the open-operation marker and the hashed
        # dirty-path set both existed only to stop a tree comparison drawing
        # the wrong conclusion, and nothing draws conclusions from a tree now.
        observed = worktree.snapshot(self.repo)

        self.assertTrue(observed["git"])
        self.assertEqual(
            {"git", "head", "digest", "files", "lines", "degraded"}, set(observed)
        )


if __name__ == "__main__":
    unittest.main()


class JournalReadGuardTests(unittest.TestCase):
    """Nothing a person can leave in the journal directory may hang a report.

    Found by the verification gate: `append` refuses a link-like path and opens
    with `O_NOFOLLOW`, while `read` had no equivalent check, so a month file
    that was a FIFO or a symlink to a device blocked `doctor`, `conformance`
    and `effectiveness` forever. `open()` raising was guarded; `open()`
    blocking was not.
    """

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.home = Path(self._directory.name)
        self.journal = (
            self.home / ".local/share/graph-engineering-agent-harness/journal"
        )
        self.journal.mkdir(parents=True)

    def _real_month(self, name: str = "2026-08.jsonl") -> None:
        (self.journal / name).write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "event": "session_open",
                    "ts": "t",
                    "client": "claude",
                    "session_id": "s",
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def _read_within(self, seconds: float = 5.0) -> list[dict[str, object]]:
        """Read the journal in a subprocess, so a hang fails instead of wedging."""

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json,sys;"
                "sys.path.insert(0, sys.argv[1]);"
                "from agent_harness import journal;"
                "print(json.dumps([e for e in journal.read(sys.argv[2])]))",
                str(REPOSITORY),
                str(self.home),
            ],
            capture_output=True,
            text=True,
            timeout=seconds,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return cast("list[dict[str, object]]", json.loads(result.stdout))

    # os.mkfifo does not exist on Windows, and the CI matrix runs there.
    # The guard the test drives is the same on both, but only POSIX can build
    # the file that proves it.
    @unittest.skipIf(os.name == "nt", "POSIX named pipe")
    def test_a_fifo_month_file_is_skipped_rather_than_read(self) -> None:
        self._real_month()
        os.mkfifo(self.journal / "2026-09.jsonl")

        records = self._read_within()

        self.assertEqual(1, len(records), "the real month is still read")

    def test_a_month_symlinked_to_a_device_is_skipped(self) -> None:
        self._real_month()
        (self.journal / "2026-09.jsonl").symlink_to("/dev/zero")

        records = self._read_within()

        self.assertEqual(1, len(records))

    def test_a_directory_named_like_a_month_is_skipped(self) -> None:
        self._real_month()
        (self.journal / "2026-09.jsonl").mkdir()

        self.assertEqual(1, len(self._read_within()))

    def test_a_month_symlinked_to_a_regular_file_is_still_refused(self) -> None:
        # The write path refuses a link-like path outright; the read path now
        # matches it, so the two no longer disagree about what is safe.
        self._real_month()
        elsewhere = self.home / "elsewhere.jsonl"
        elsewhere.write_text(
            json.dumps({"schema_version": 2, "event": "turn_end", "ts": "t"}) + "\n",
            encoding="utf-8",
        )
        (self.journal / "2026-09.jsonl").symlink_to(elsewhere)

        self.assertEqual(1, len(self._read_within()))

    @unittest.skipIf(os.name == "nt", "POSIX symlink")
    def test_open_month_refuses_a_symlink_even_when_it_is_reached_directly(
        self,
    ) -> None:
        # `is_named_month` already refuses a symlink by lstat, so nothing
        # reaches `open_month` with one by the ordinary route, and every
        # test above it therefore passes whether or not O_NOFOLLOW is set.
        # That is the whole point of the flag: it closes the window between
        # the enumeration's lstat and this open, which a swap can exploit.
        # Reaching the opener directly is the only way to hold that window
        # open on purpose, and without it this assertion fails.
        elsewhere = self.home / "elsewhere.jsonl"
        elsewhere.write_text("{}\n", encoding="utf-8")
        link = self.journal / "2026-09.jsonl"
        link.symlink_to(elsewhere)

        self.assertIsNone(journal.open_month(link))

    def test_a_platform_without_o_nonblock_still_reads_its_months(self) -> None:
        # The condition on Windows 3.13, reproduced here because no local
        # machine has it. Windows has no O_NONBLOCK, so nothing is ever set
        # and there is nothing to clear -- but it grew `os.set_blocking` in
        # 3.12, so a `hasattr` guard passed, the call failed on a regular
        # file descriptor, and the OSError handler swallowed it. Every month
        # came back unreadable, `read` returned nothing, and roughly seventy
        # tests failed on that one CI leg while Windows 3.10 passed, having
        # no `os.set_blocking` to call.
        self._real_month()
        month = self.journal / "2026-08.jsonl"
        self.assertTrue(month.is_file(), "the fixture month was not written")

        with mock.patch.multiple(
            os,
            O_NONBLOCK=0,
            set_blocking=mock.Mock(side_effect=OSError(22, "Invalid argument")),
            create=True,
        ):
            handle = journal.open_month(month)

        self.assertIsNotNone(handle, "the month was lost where nothing was set")
        if handle is not None:
            handle.close()
        self.assertEqual(1, len(self._read_within()))

    @unittest.skipIf(os.name == "nt", "POSIX directory permissions")
    def test_an_unwritable_lock_directory_times_out_rather_than_raising_oserror(
        self,
    ) -> None:
        # The lock retries on PermissionError as well as FileExistsError,
        # because Windows raises access-denied when creating a lock its
        # previous holder is unlinking. I declared this unreachable from a
        # POSIX test on the grounds that POSIX raises FileExistsError; that
        # was simply wrong. POSIX raises PermissionError from `open` with
        # O_CREAT|O_EXCL whenever the *parent* is not writable, which any
        # test can arrange with chmod. Measured: errno 13, and not an
        # instance of FileExistsError.
        #
        # Without the PermissionError arm the error escapes as an OSError
        # and `journal.append` drops the record. With it, the wait ends in
        # the harness's own timeout, which is a refusal the caller can see.
        from agent_harness.errors import HarnessError
        from agent_harness.storage import FileLock

        locked = self.home / "unwritable"
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)

        with self.assertRaises(HarnessError) as refused:
            with FileLock(locked / "m.lock", timeout=0.2):
                pass  # pragma: no cover - the lock never opens

        self.assertIn("timed out waiting for lock", str(refused.exception))

    def test_an_unreadable_month_does_not_blind_the_rest(self) -> None:
        self._real_month()
        blocked = self.journal / "2026-09.jsonl"
        blocked.write_text("{}\n", encoding="utf-8")
        blocked.chmod(0o000)
        self.addCleanup(blocked.chmod, 0o600)

        self.assertEqual(1, len(self._read_within()))

    def test_one_month_that_cannot_be_stat_ed_does_not_lose_the_others(self) -> None:
        # The first fix called the link check outside its guard, and that
        # helper answers only a missing file, so one EACCES month raised out
        # of the enumeration and took every readable month with it.
        #
        # The blocked path is built from the *resolved* home. An earlier
        # version built it from the raw temporary directory while the code
        # resolves /var to /private/var, so the mock never fired once and the
        # test passed against the very defect it was written for.
        #
        # `Path.lstat`, not `os.stat`. Patching `os.stat` worked here on 3.12
        # and silently did nothing on 3.10, where `pathlib` binds the os
        # functions into an accessor at import time: measured, a patched
        # `os.stat` is reached by `Path.stat()` on 3.13 and is not on 3.10.
        # The suite was therefore red on three of the six CI legs, and only
        # this test's own liveness guard said so. `Path.lstat` is the call
        # the code actually makes, so it is the same seam on every version.
        #
        # Not replaced by a real permission error, which was the obvious
        # suggestion: `lstat` needs search permission on the parent, not read
        # permission on the file, so `chmod 0o000` on one month leaves
        # `lstat` succeeding — verified — and there is no way to fail it for
        # one entry of a flat directory without failing it for all of them.
        for name in ("2026-07.jsonl", "2026-08.jsonl", "2026-09.jsonl"):
            self._real_month(name)
        resolved = journal.journal_directory(self.home)
        blocked = str(resolved / "2026-08.jsonl")
        real_lstat = Path.lstat
        seen: list[str] = []

        def refusing(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            seen.append(str(self))
            if str(self) == blocked:
                raise PermissionError(13, "Permission denied", blocked)
            return real_lstat(self, *args, **kwargs)

        with mock.patch.object(Path, "lstat", refusing):
            months = [path.name for path in journal.month_files(self.home)]
            records = list(journal.read(self.home))

        self.assertIn(blocked, seen, "the mock must actually fire")
        # The unreadable month is dropped; the readable ones survive it.
        self.assertEqual(["2026-07.jsonl", "2026-09.jsonl"], months)
        self.assertEqual(2, len(records))

    def test_an_unsearchable_journal_directory_does_not_fail_a_command(self) -> None:
        self._real_month()
        self.journal.chmod(0o400)
        self.addCleanup(self.journal.chmod, 0o700)
        workspace = self.home / "work-unsearchable"
        workspace.mkdir()

        for command in ("doctor", "conformance", "effectiveness"):
            with self.subTest(command=command):
                result = subprocess.run(
                    [sys.executable, str(REPOSITORY / "scripts/graphctl.py"), command],
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    env={**os.environ, "GRAPH_HARNESS_HOME": str(self.home)},
                )
                self.assertEqual(0, result.returncode, result.stderr)

    # os.mkfifo does not exist on Windows, and the CI matrix runs there.
    # The guard the test drives is the same on both, but only POSIX can build
    # the file that proves it.
    @unittest.skipIf(os.name == "nt", "POSIX named pipe")
    def test_a_month_swapped_for_a_fifo_after_it_is_named_cannot_hang(self) -> None:
        # A check followed by an open is two syscalls with a window between
        # them, and a writer racing that window hung three reads in forty.
        # open_month decides from the descriptor it already holds, so a swap
        # can only make the open fail, never make it block.
        #
        # Driven in a subprocess under a timeout like every other test here.
        # An earlier version called open_month in process, so removing the
        # guard wedged the runner instead of failing it — in the one test
        # named for not hanging.
        self._real_month()
        path = self.journal / "2026-09.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        # Compared by name: the home is reached through /var, which resolves
        # to /private/var, so the paths differ while naming one file.
        named = [entry.name for entry in journal.month_files(self.home)]
        self.assertIn(path.name, named)

        path.unlink()
        os.mkfifo(path)

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys;"
                "sys.path.insert(0, sys.argv[1]);"
                "from pathlib import Path;"
                "from agent_harness import journal;"
                "print(journal.open_month(Path(sys.argv[2])) is None)",
                str(REPOSITORY),
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("True", result.stdout.strip())

    # os.mkfifo does not exist on Windows, and the CI matrix runs there.
    # The guard the test drives is the same on both, but only POSIX can build
    # the file that proves it.
    @unittest.skipIf(os.name == "nt", "POSIX named pipe")
    def test_every_reporting_command_finishes_with_a_fifo_present(self) -> None:
        self._real_month()
        os.mkfifo(self.journal / "2026-09.jsonl")
        workspace = self.home / "work"
        workspace.mkdir()

        for command in ("doctor", "conformance", "effectiveness"):
            with self.subTest(command=command):
                result = subprocess.run(
                    [sys.executable, str(REPOSITORY / "scripts/graphctl.py"), command],
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    env={**os.environ, "GRAPH_HARNESS_HOME": str(self.home)},
                )
                self.assertEqual(0, result.returncode, result.stderr)
