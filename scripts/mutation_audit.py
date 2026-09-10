#!/usr/bin/env python3
"""Prove each guard in the harness is held by a test, and name any that is not.

Why this is committed rather than run once
------------------------------------------
Three of this repository's defects were guards that looked load-bearing and
were not: `O_NOFOLLOW` in the journal reader, the `ValueError` arm that
answers an embedded NUL, and the type check that refuses a JSON array. Each
could be deleted with the whole suite still green, so each could be reverted
by accident and nothing would say so. A green suite does not tell you which
of its assertions is doing work.

Two independent reviewers rebuilt this mapping by hand to check a claim about
it, because it existed only as prose in the evidence record. Committed, it is
a regression guard rather than an anecdote: run it and it tells you which
guard is uncovered today.

How it works, and the rule it obeys
-----------------------------------
Each entry below names one guard, the exact source it occupies, a mutation
that disables it, and the test expected to object. The harness copies the
repository, applies one mutation to the copy, runs the suite there, and
subtracts a measured baseline so a pre-existing failure can never be read as
a mutation being caught.

**It never touches the working tree.** An earlier version of this audit
mutated the source in place and restored it in a `finally`; a filesystem
snapshot taken mid-run captured `if False:` live inside a guard, and that
mutation was later restored into the repository as though it were real
source. The target is asserted to be outside the live tree before anything
is written, and the audit exits rather than proceed if it is not.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

LIVE = Path(__file__).resolve().parents[1]

# Written where a mutation would otherwise need a helper exception type.
NEVER = "class _NeverRaised(Exception):\n    pass\n\n\n"


@dataclass(frozen=True)
class Guard:
    """One guard, the mutation that disables it, and its expected objector."""

    name: str
    relative: str
    old: str
    new: str
    pinned_by: str
    # Set where a mutation needs a helper class defined at module scope.
    needs_never: bool = False
    # Modules to run for the fast subset; empty means the whole suite.
    focus: tuple[str, ...] = field(default_factory=tuple)


GUARDS: tuple[Guard, ...] = (
    Guard(
        name="journal read: O_NOFOLLOW",
        relative="agent_harness/journal.py",
        old='flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | non_blocking',
        new="flags = os.O_RDONLY | non_blocking",
        pinned_by="test_open_month_refuses_a_symlink_even_when_it_is_reached_directly",
        focus=("tests.test_journal_contract",),
    ),
    Guard(
        name="journal read: clear O_NONBLOCK only where set",
        relative="agent_harness/journal.py",
        old="        if non_blocking:",
        new='        if hasattr(os, "set_blocking"):',
        pinned_by="test_a_platform_without_o_nonblock_still_reads_its_months",
        focus=("tests.test_journal_contract",),
    ),
    Guard(
        name="report loader: symlink refusal",
        relative="agent_harness/effectiveness.py",
        old="        if is_link_like(Path(path)):\n            return None, REFUSAL_SYMLINK",
        new="        if False:\n            return None, REFUSAL_SYMLINK",
        pinned_by="test_a_symlinked_graph_says_symlink_rather_than_outside",
        focus=("tests.test_effectiveness_contract",),
    ),
    Guard(
        name="report loader: ValueError arm (embedded NUL)",
        relative="agent_harness/effectiveness.py",
        old="    except (OSError, ValueError):",
        new="    except OSError:",
        pinned_by="test_a_path_with_an_embedded_nul_is_refused_not_raised",
        focus=("tests.test_effectiveness_contract",),
    ),
    Guard(
        name="report loader: OSError arm (deleted cwd)",
        relative="agent_harness/effectiveness.py",
        old="    except OSError:\n        # Reached when the failure is in resolving",
        new="    except _NeverRaised:\n        # Reached when the failure is in resolving",
        pinned_by="test_a_deleted_working_directory_is_refused_not_raised",
        needs_never=True,
        focus=("tests.test_effectiveness_contract",),
    ),
    Guard(
        name="report loader: regular-file check",
        relative="agent_harness/effectiveness.py",
        old="        if not stat.S_ISREG(stats.st_mode):",
        new="        if False:",
        pinned_by="test_a_graph_that_is_not_a_regular_file_cannot_hang_the_report",
        focus=("tests.test_effectiveness_contract",),
    ),
    Guard(
        name="report loader: size bound",
        relative="agent_harness/effectiveness.py",
        old="        if stats.st_size > MAX_REPORT_GRAPH_BYTES:",
        new="        if False:",
        pinned_by="test_a_graph_above_the_storage_bound_is_refused_not_read",
        focus=("tests.test_effectiveness_contract",),
    ),
    Guard(
        name="report loader: UnicodeDecodeError arm",
        relative="agent_harness/effectiveness.py",
        old="    except UnicodeDecodeError:",
        new="    except _NeverRaised:",
        pinned_by="test_a_graph_of_undecodable_bytes_is_refused_not_raised",
        needs_never=True,
        focus=("tests.test_effectiveness_contract",),
    ),
    Guard(
        name="report loader: not-a-dict check",
        relative="agent_harness/effectiveness.py",
        old="    if not isinstance(loaded, dict):",
        new="    if False:",
        pinned_by="test_a_graph_that_is_valid_json_but_not_an_object_is_refused",
        focus=("tests.test_effectiveness_contract",),
    ),
    Guard(
        name="test workspace sweep: refuse the repository",
        relative="tests/__init__.py",
        old='    if resolved == repository:\n        raise RuntimeError("refusing to remove the repository itself")',
        new='    if False:\n        raise RuntimeError("unreachable")',
        pinned_by="test_the_sweep_refuses_to_delete_the_repository",
        focus=("tests.test_suite_hygiene_contract",),
    ),
    Guard(
        name="test workspace sweep: refuse a symlink",
        relative="tests/__init__.py",
        old='    if root.is_symlink():\n        raise RuntimeError(f"workspace root is a symlink: {root}")',
        new='    if False:\n        raise RuntimeError("unreachable")',
        pinned_by="test_the_sweep_refuses_a_symlinked_workspace_root",
        focus=("tests.test_suite_hygiene_contract",),
    ),
    Guard(
        name="test workspace sweep: clear on import",
        relative="tests/__init__.py",
        old="_clear_workspace_root()\natexit.register(_clear_workspace_root)",
        new="atexit.register(_clear_workspace_root)",
        pinned_by="test_a_killed_run_is_cleaned_up_by_the_next_one",
        focus=("tests.test_suite_hygiene_contract",),
    ),
    Guard(
        name="install drift: compare only owned entries",
        relative="scripts/install_pc.py",
        old="        on_disk = _managed_shape(_load_settings(hook_path), managed_commands)\n        if on_disk != _managed_shape(document, managed_commands):",
        new="        if _load_settings(hook_path) != document:",
        pinned_by="test_a_third_party_hook_is_never_drift_whichever_side_of_ours_it_sits",
        focus=("tests.test_pc_install_contract",),
    ),
    Guard(
        name="install drift: keep entries per event",
        relative="scripts/install_pc.py",
        old="    return shape",
        new=(
            "    flattened = sorted(\n"
            "        (entry for entries in shape.values() for entry in entries),\n"
            "        key=repr,\n"
            "    )\n"
            '    return {"all": flattened}',
        )[0],
        pinned_by="test_check_reports_drift_for_a_managed_entry_moved_to_another_event",
        focus=("tests.test_pc_install_contract",),
    ),
    Guard(
        name="install drift: carry the matcher",
        relative="scripts/install_pc.py",
        old='                        carried["matcher"] = matcher.get("matcher")\n                        ours.append(carried)',
        new="                        ours.append(carried)",
        pinned_by="test_check_reports_drift_for_a_changed_managed_matcher",
        focus=("tests.test_pc_install_contract",),
    ),
    Guard(
        name="install drift: keep duplicates distinguishable",
        relative="scripts/install_pc.py",
        old="                        ours.append(carried)",
        new="                        ours[:] = [carried]",
        pinned_by="test_check_reports_drift_for_a_duplicated_managed_entry",
        focus=("tests.test_pc_install_contract",),
    ),
    Guard(
        name="lock: retry on Windows access-denied",
        relative="agent_harness/storage.py",
        old="            except (FileExistsError, PermissionError) as exc:",
        new="            except FileExistsError as exc:",
        pinned_by="test_an_unwritable_lock_directory_times_out_rather_than_raising_oserror",
        focus=("tests.test_journal_contract",),
    ),
    Guard(
        name="snapshot: decode git output as UTF-8",
        relative="agent_harness/worktree.py",
        old='            encoding="utf-8",\n            errors="replace",',
        new="",
        pinned_by="test_the_snapshot_is_the_same_whatever_the_locale_says",
        focus=("tests.test_journal_contract",),
    ),
)


def assert_outside_live(target: Path) -> Path:
    """Refuse a mutation target that lies inside the working tree.

    The rule this file exists to keep, and the reason it is a function with
    a test rather than a line inside the copier: an in-place version of this
    audit had its mutation captured by a filesystem snapshot and later
    restored into the repository as though it were real source.
    """

    resolved = target.resolve()
    live = LIVE.resolve()
    if resolved == live or resolved.is_relative_to(live):
        raise SystemExit(f"refusing to mutate inside the live tree: {resolved}")
    return resolved


def copy_repository() -> Path:
    """Copy the repository somewhere that is provably not the working tree."""

    root = Path(tempfile.mkdtemp(prefix="mutation-audit.")) / "repo"
    # Checked before anything is written, not after.
    assert_outside_live(root.parent)
    subprocess.run(
        (
            "rsync",
            "-a",
            "--exclude=.git",
            "--exclude=.venv",
            "--exclude=__pycache__",
            "--exclude=.mypy_cache",
            "--exclude=.test-workspaces",
            f"{LIVE}/",
            f"{root}/",
        ),
        check=True,
    )
    assert_outside_live(root)
    return root


def run_suite(root: Path, modules: tuple[str, ...]) -> set[str]:
    """Return the names of tests that failed or errored in *root*."""

    for cache in root.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    command = [sys.executable, "-m", "unittest"]
    command.extend(modules or ("discover",))
    done = subprocess.run(
        command,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=900,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return set(re.findall(r"^(?:FAIL|ERROR): (\w+)", done.stderr, re.MULTILINE))


def apply_mutation(root: Path, guard: Guard) -> None:
    """Disable one guard in the copy, refusing to guess if the source moved."""

    path = root / guard.relative
    source = path.read_text(encoding="utf-8")
    occurrences = source.count(guard.old)
    if occurrences != 1:
        raise SystemExit(
            f"{guard.name}: its source no longer matches ({occurrences} matches in "
            f"{guard.relative}). The guard may have moved or been rewritten; update "
            f"this entry rather than letting the audit skip it silently."
        )
    mutated = source.replace(guard.old, guard.new, 1)
    if guard.needs_never:
        mutated = mutated.replace("import stat\n", "import stat\n\n\n" + NEVER, 1)
    path.write_text(mutated, encoding="utf-8")


def audit(selected: tuple[Guard, ...], fast: bool) -> int:
    started = time.monotonic()
    print(f"auditing {len(selected)} guards ({'focused' if fast else 'full suite'})")
    baseline_root = copy_repository()
    baseline_modules = _modules(selected, fast)
    baseline = run_suite(baseline_root, baseline_modules)
    print(f"baseline failures: {sorted(baseline) or 'none'}\n")

    uncovered: list[Guard] = []
    wrong_test: list[tuple[Guard, set[str]]] = []
    contradicted: list[tuple[Guard, set[str]]] = []
    for guard in selected:
        root = copy_repository()
        apply_mutation(root, guard)
        caught = run_suite(root, _modules((guard,), fast)) - baseline
        if not caught:
            uncovered.append(guard)
            print(f"  {guard.name:52} UNCOVERED")
        elif not guard.pinned_by:
            # Declared unreachable, and something reached it. That is a
            # finding, not a pass: the declaration is the claim under test.
            # Without this branch an empty `pinned_by` short-circuited the
            # named-test check below, so anything at all falling out of the
            # run printed "pinned by ..." while the summary still called the
            # guard uncovered and the exit stayed 0 — which is how two false
            # "unreachable" labels of mine survived to review. The harness
            # could not falsify its own uncovered claim.
            contradicted.append((guard, caught))
            print(f"  {guard.name:52} DECLARED UNCOVERED, BUT SOMETHING OBJECTED")
            print(f"      caught: {', '.join(sorted(caught))}")
        elif guard.pinned_by and guard.pinned_by not in caught:
            wrong_test.append((guard, caught))
            print(f"  {guard.name:52} caught, but not by the named test")
            print(f"      named:  {guard.pinned_by}")
            print(f"      caught: {', '.join(sorted(caught))}")
        else:
            named = guard.pinned_by if guard.pinned_by in caught else sorted(caught)[0]
            print(f"  {guard.name:52} pinned by {named}")
        shutil.rmtree(root.parent, ignore_errors=True)
    shutil.rmtree(baseline_root.parent, ignore_errors=True)

    declared = [guard for guard in selected if not guard.pinned_by]
    surprises = [guard for guard in uncovered if guard.pinned_by]
    print()
    print(f"elapsed {time.monotonic() - started:.0f}s")
    if declared:
        print("declared uncovered, and why:")
        for guard in declared:
            print(f"  - {guard.name}")
    if surprises:
        print("GUARDS THAT LOST THEIR COVER:")
        for guard in surprises:
            print(f"  - {guard.name}: {guard.pinned_by} no longer objects")
    if wrong_test:
        print("GUARDS CAUGHT BY A DIFFERENT TEST THAN DECLARED:")
        for guard, caught in wrong_test:
            print(f"  - {guard.name}: {sorted(caught)}")
    if contradicted:
        print("DECLARED UNCOVERED, BUT SOMETHING OBJECTED:")
        for guard, caught in contradicted:
            print(f"  - {guard.name}: {sorted(caught)}")
            print(
                "    Either name the test that objected, or the declaration is wrong."
            )
    if not surprises and not wrong_test and not contradicted:
        # Counted from results rather than restated from the declarations,
        # so the line is a measurement instead of an echo of the input.
        covered = len(selected) - len(uncovered)
        print(
            f"{covered} of {len(selected)} guards pinned by their named test; "
            f"{len(declared)} declared uncovered"
        )
    return 1 if surprises or wrong_test or contradicted else 0


def _modules(guards: tuple[Guard, ...], fast: bool) -> tuple[str, ...]:
    if not fast:
        return ()
    focus: list[str] = []
    for guard in guards:
        for module in guard.focus:
            if module not in focus:
                focus.append(module)
    return tuple(focus)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--fast",
        action="store_true",
        help="run only each guard's own test module rather than the whole suite",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="SUBSTRING",
        help="audit only guards whose name contains this; repeat to add more",
    )
    parser.add_argument(
        "--list", action="store_true", help="print the guards and their tests"
    )
    arguments = parser.parse_args(argv)

    selected = tuple(
        guard
        for guard in GUARDS
        if not arguments.only
        or any(fragment in guard.name for fragment in arguments.only)
    )
    if arguments.list:
        for guard in selected:
            print(
                f"{guard.name}\n    {guard.relative}\n    {guard.pinned_by or '(declared uncovered)'}"
            )
        return 0
    if not selected:
        raise SystemExit("no guard matched --only")
    return audit(selected, arguments.fast)


if __name__ == "__main__":
    raise SystemExit(main())
