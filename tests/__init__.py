"""Contract tests for the vendor-neutral task-graph core.

Importing this package points harness state at a throwaway directory for the
whole run. The library records observations under the user's home, so without
this a test that runs ``graphctl`` or a hook would append to the developer's
own journal, and a CI run would append to the runner's. Individual tests still
set the variable themselves when they need to read back what they wrote.
"""

from __future__ import annotations

import atexit
import multiprocessing
import os
import shutil
import tempfile
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]

# One named home for every workspace a test builds inside the repository.
#
# Several tests need their workspace to be inside this repository, and they
# used to take it straight from `tempfile.TemporaryDirectory(dir=REPOSITORY)`.
# That cleans up on a normal exit and not on a kill, so an interrupted run
# left `tmpXXXXXXXX/` behind at the top level — 42 had accumulated before
# anyone noticed, because each held only an empty directory and git cannot
# see an empty directory, so `git status` stayed clean.
#
# Collecting them under one name fixes both halves: what is left is
# unambiguously ours, so it can be removed without pattern-matching names
# that might belong to somebody else, and one `.gitignore` entry is visible
# where `tmp*` scattered at the root was not.
WORKSPACE_ROOT = REPOSITORY / ".test-workspaces"


def _clear_workspace_root() -> None:
    """Remove the workspace root, refusing to remove anything else.

    Every check below is here because the unguarded version of this function
    deleted this repository. It was three lines — a module-level
    ``shutil.rmtree(WORKSPACE_ROOT, ignore_errors=True)`` — and a mutation
    test that set ``WORKSPACE_ROOT = REPOSITORY`` to prove the sweep was
    load-bearing. Importing the module was enough to fire it: source,
    history and virtualenv were gone in one call, recovered only from a
    filesystem snapshot.

    So: the target is resolved and checked to be a directory of the expected
    name directly inside the repository, never the repository itself and
    never reached through a symlink. And it raises rather than passing
    quietly — ``ignore_errors=True`` is what made the original silent.
    """

    # Not in a multiprocessing child. Under the spawn start method — the
    # default on macOS and Windows — every pool worker re-imports this
    # package and so runs this sweep, and eight of them raced each other
    # inside one `shutil.rmtree`: the loser raised `FileNotFoundError` out of
    # its own import, which the pool reported only as a worker that died for
    # no stated reason, and the interrupted walk left directory entries that
    # `os.scandir` then answered with `Stale NFS file handle`. Measured at 11
    # failures in 12 attempts with the root populated, which is why
    # `test_concurrent_appends_lose_no_line_and_interleave_none` failed
    # intermittently and only on the spawn platforms.
    #
    # The sweep cannot be made forgiving — `ignore_errors=True` is what
    # destroyed this repository once — so the answer is not to run it where
    # it has nothing to clean. A child inherits a run its parent already
    # swept for, and its own workspaces are the parent's to remove.
    if multiprocessing.parent_process() is not None:
        return
    root = WORKSPACE_ROOT
    repository = REPOSITORY.resolve()
    if root.is_symlink():
        raise RuntimeError(f"workspace root is a symlink: {root}")
    if not root.exists():
        return
    resolved = root.resolve()
    if resolved == repository:
        raise RuntimeError("refusing to remove the repository itself")
    if resolved.parent != repository:
        raise RuntimeError(f"workspace root is not inside the repository: {resolved}")
    if resolved.name != ".test-workspaces":
        raise RuntimeError(f"unexpected workspace root name: {resolved.name}")
    shutil.rmtree(resolved)


# What a copy of the checkout must not carry: `.git` would make the copy a
# repository of its own, `.venv` is large and machine-specific, and
# `.test-workspaces` is the sweep target the tests that copy the tree are
# testing. Named once, because a test that drives a destructive guard against
# a copy must not be able to get this list wrong.
COPY_EXCLUSIONS = (".git", ".venv", "__pycache__", ".mypy_cache", ".test-workspaces")


def copy_repository(into: Path) -> Path:
    """Copy the checkout to *into* and return it."""

    shutil.copytree(REPOSITORY, into, ignore=shutil.ignore_patterns(*COPY_EXCLUSIONS))
    return into


def workspace_root() -> Path:
    """Return the directory that holds test workspaces, ready for use."""

    WORKSPACE_ROOT.mkdir(exist_ok=True)
    return WORKSPACE_ROOT


# Cleared on import as well as at exit, so a run that was killed is cleaned
# by the next one rather than accumulating.
_clear_workspace_root()
atexit.register(_clear_workspace_root)

# Set unconditionally rather than deferring to an exported value. No test
# needs the deference — the ones that care set the variable themselves in a
# subprocess environment — while deferring would let a developer or a CI job
# that happens to export it write records into real harness state, which is
# the very thing this exists to prevent.
_isolated_home = tempfile.mkdtemp(prefix="graph-harness-tests.")
os.environ["GRAPH_HARNESS_HOME"] = _isolated_home
atexit.register(shutil.rmtree, _isolated_home, True)
