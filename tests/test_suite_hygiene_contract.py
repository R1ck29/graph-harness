"""The suite must leave the repository as it found it, and never damage it.

Two defects are recorded here. The first: tests that need a workspace inside
this repository took one from `tempfile.TemporaryDirectory(dir=REPOSITORY)`,
which cleans up on a normal exit and not on a kill, so every interrupted run
left a `tmpXXXXXXXX/` directory at the top level. Forty-two accumulated before
anyone noticed, because each held only an empty directory and git cannot see
an empty directory.

The second is why the guards in `_clear_workspace_root` exist at all. The
first fix for the leak swept the workspace root with a module-level
`shutil.rmtree(WORKSPACE_ROOT, ignore_errors=True)`. A mutation test then set
`WORKSPACE_ROOT = REPOSITORY` to prove the sweep was load-bearing, and
importing the module deleted the repository — source, history and virtualenv
— recovered only from a filesystem snapshot.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import sys
import unittest
from pathlib import Path

from tests import (
    REPOSITORY,
    WORKSPACE_ROOT,
    _clear_workspace_root,
    workspace_root,
)

PROGRAM = (
    "import os, sys;"
    "from pathlib import Path;"
    "sys.path.insert(0, {repository!r});"
    "import tests;"
    "{body}"
)


def _run(body: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    root = cwd or REPOSITORY
    return subprocess.run(
        [sys.executable, "-c", PROGRAM.format(repository=str(root), body=body)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=root,
    )


class SuiteHygieneTests(unittest.TestCase):
    def test_workspaces_live_under_one_named_directory_inside_the_repository(
        self,
    ) -> None:
        # Inside the repository because confinement requires it, and under a
        # single name so what is left is unambiguously ours.
        root = workspace_root()

        self.assertTrue(root.is_dir())
        self.assertEqual(REPOSITORY, root.parent)
        self.assertEqual(WORKSPACE_ROOT, root)

    def test_the_named_directory_is_ignored_by_git(self) -> None:
        ignored = (REPOSITORY / ".gitignore").read_text(encoding="utf-8")

        self.assertIn(f"/{WORKSPACE_ROOT.name}/", ignored)

    def test_a_killed_run_is_cleaned_up_by_the_next_one(self) -> None:
        # The case the old code could not handle. `os._exit` skips `atexit`,
        # so this leaves a workspace behind exactly as a kill would.
        left = _run(
            "p = tests.workspace_root() / 'killed-run';"
            "p.mkdir();"
            "(p / 'inner').mkdir();"
            "print(p);"
            # os._exit skips the flush as well as atexit, so without this the
            # path never reaches the pipe and the assertion below compares
            # Path(''), which always exists. It passed for the wrong reason
            # until this was added.
            "sys.stdout.flush();"
            "os._exit(0)"
        )
        self.assertEqual(0, left.returncode, left.stderr)
        stranded = Path(left.stdout.strip())
        self.assertTrue(stranded.name, "the child printed no path")
        self.assertTrue(stranded.is_dir(), "the kill did not leave a workspace")

        # Asked *inside* the next process, immediately after import. Asking
        # from here after it exits proves nothing: its own `atexit` would
        # have cleared the root on the way out, so the assertion passed
        # whether or not the import-time sweep existed at all.
        after = _run(f"print(Path({str(stranded)!r}).exists())")

        self.assertEqual(0, after.returncode, after.stderr)
        self.assertEqual(
            "False",
            after.stdout.strip(),
            "the import-time sweep did not clear a killed run's workspace",
        )
        self.assertFalse(stranded.exists(), "a killed run's workspace survived")

    def test_the_sweep_refuses_to_delete_the_repository(self) -> None:
        # The exact mutation that destroyed this repository, replayed against
        # a copy. It must raise and leave the tree standing. Nothing about
        # this test may be pointed at the real checkout.
        # TemporaryDirectory with addCleanup rather than enterContext,
        # which is Python 3.11 and this project supports 3.10.
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        root = Path(holder.name) / "repo"
        shutil.copytree(
            REPOSITORY,
            root,
            ignore=shutil.ignore_patterns(
                ".git", ".venv", "__pycache__", ".mypy_cache", ".test-workspaces"
            ),
        )
        self.assertNotEqual(REPOSITORY, root)
        init = root / "tests/__init__.py"
        source = init.read_text(encoding="utf-8")
        mutated = source.replace(
            'WORKSPACE_ROOT = REPOSITORY / ".test-workspaces"',
            "WORKSPACE_ROOT = REPOSITORY",
            1,
        )
        self.assertNotEqual(source, mutated, "the mutation did not apply")
        init.write_text(mutated, encoding="utf-8")
        survivor = root / "AGENTS.md"
        self.assertTrue(survivor.is_file())

        done = _run("pass", cwd=root)

        self.assertNotEqual(0, done.returncode, "the sweep did not refuse")
        self.assertIn("refusing to remove the repository itself", done.stderr)
        self.assertTrue(survivor.is_file(), "the repository copy was deleted")

    # Skipped on Windows because the sweep it exercises reads
    # `Path.is_symlink`, whose meaning for a junction differs there and
    # is covered separately. Not, as I first wrote, because symlink
    # creation needs elevation: the last Windows CI run shows four
    # symlink tests reporting ok rather than skipped, which proves
    # creation succeeds on the runner. The reason was wrong even though
    # the skip is harmless, and a wrong reason in a comment is how the
    # next person learns something untrue.
    @unittest.skipIf(os.name == "nt", "POSIX symlink semantics")
    def test_the_sweep_refuses_a_symlinked_workspace_root(self) -> None:
        # Following a link out of the repository would delete whatever it
        # points at — the same failure as deleting the repository, by a
        # different route.
        #
        # Driven against a copy, never the live checkout. The first version
        # of this test replaced the real `.test-workspaces` with a symlink
        # and relied on `addCleanup` to put it back; interrupted in between,
        # every later `import tests` would raise and the suite would be
        # unrunnable until somebody unlinked it by hand. A test for a
        # destructive guard must not itself be able to break the tree.
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        root = Path(holder.name) / "repo"
        shutil.copytree(
            REPOSITORY,
            root,
            ignore=shutil.ignore_patterns(
                ".git", ".venv", "__pycache__", ".mypy_cache", ".test-workspaces"
            ),
        )
        outside = Path(holder.name) / "outside"
        outside.mkdir()
        keep = outside / "keep.txt"
        keep.write_text("do not delete me", encoding="utf-8")
        (root / ".test-workspaces").symlink_to(outside, target_is_directory=True)

        done = _run("pass", cwd=root)

        self.assertNotEqual(0, done.returncode, "the sweep did not refuse")
        self.assertIn("workspace root is a symlink", done.stderr)
        self.assertTrue(keep.is_file(), "the sweep followed the link")
        self.assertTrue(outside.is_dir())

    def test_an_empty_leftover_is_invisible_to_git_which_is_why_this_exists(
        self,
    ) -> None:
        # The reason `git status` was never going to catch the leak.
        # Recorded so the next person does not reach for it and trust it.
        stray = workspace_root() / "empty-leftover"
        stray.mkdir()
        self.addCleanup(stray.rmdir)

        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
        )

        self.assertNotIn("empty-leftover", status.stdout)


if __name__ == "__main__":
    unittest.main()
