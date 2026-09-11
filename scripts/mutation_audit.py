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
import unittest
from dataclasses import dataclass, field
from pathlib import Path

LIVE = Path(__file__).resolve().parents[1]

# What a copy of the checkout must not carry. `tests/__init__.py` names the
# same list for the same reason; this script cannot import it, because
# importing `tests` sweeps a workspace and rebinds the harness home, which a
# script whose whole contract is to leave the live tree alone must not do.
COPY_EXCLUSIONS = (".git", ".venv", "__pycache__", ".mypy_cache", ".test-workspaces")

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
    # Modules to run for the fast subset. Not optional in practice: a guard
    # that names a test must name the module it lives in, because the credit
    # is checked against both, and `objected` refuses one that does not.
    focus: tuple[str, ...] = field(default_factory=tuple)


GUARDS: tuple[Guard, ...] = (
    Guard(
        # This guard and the three below moved with the code. They used to
        # sit inside `journal.open_month` and `effectiveness._load`, which
        # had worked the same rules out separately; they now describe the one
        # primitive every reader of an untrusted file shares. The tests that
        # pin them are still the ones written against those two readers,
        # which is the point: a shared primitive has to keep both contracts.
        name="bounded open: refuse a symlink",
        relative="agent_harness/paths.py",
        old="    if stat.S_ISLNK(initial.st_mode):\n        raise FileRefusal(REFUSAL_SYMLINK, path)",
        new="    if False:\n        raise FileRefusal(REFUSAL_SYMLINK, path)",
        # Pinned on the refusal happening before the open rather than on a
        # symlink being refused: `O_NOFOLLOW` refuses one too, and it is zero
        # on Windows, so a test that checks only the outcome passes on POSIX
        # whether this line is here or not — which is what an audit on macOS
        # reported when it called this guard uncovered.
        pinned_by="test_a_symlink_is_refused_before_it_is_opened",
        focus=("tests.test_journal_contract",),
    ),
    Guard(
        name="bounded open: clear O_NONBLOCK only where it was set",
        relative="agent_harness/paths.py",
        old="        if non_blocking:\n            os.set_blocking(descriptor, True)",
        new='        if hasattr(os, "set_blocking"):\n            os.set_blocking(descriptor, True)',
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
        name="bounded open: refuse anything that is not a regular file",
        relative="agent_harness/paths.py",
        old="    if not stat.S_ISREG(initial.st_mode):\n        raise FileRefusal(REFUSAL_NOT_A_REGULAR_FILE, path)",
        new="    if False:\n        raise FileRefusal(REFUSAL_NOT_A_REGULAR_FILE, path)",
        # The `fstat` after the open refuses a FIFO as well, and reaching it
        # is only safe because `O_NONBLOCK` is non-zero here. Where it is
        # zero, removing this line blocks for ever instead of failing, so the
        # test asserts that nothing was opened.
        pinned_by="test_a_named_pipe_is_refused_before_it_is_opened",
        focus=("tests.test_journal_contract",),
    ),
    Guard(
        name="bounded open: refuse a file over the caller's bound",
        relative="agent_harness/paths.py",
        old="        if opened.st_size > max_bytes:",
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
        name="test workspace sweep: not in a pool worker",
        relative="tests/__init__.py",
        old="    if multiprocessing.parent_process() is not None:\n        return",
        new="    if False:\n        return",
        pinned_by="test_a_pool_worker_does_not_sweep_the_workspaces_its_parent_is_using",
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
            '    return {"all": flattened}'
        ),
        pinned_by="test_check_reports_drift_for_a_managed_entry_moved_to_another_event",
        focus=("tests.test_pc_install_contract",),
    ),
    Guard(
        name="install drift: carry the matcher",
        relative="scripts/install_pc.py",
        old='                carried["matcher"] = matcher.get("matcher")\n                yield event, carried',
        new="                yield event, carried",
        pinned_by="test_check_reports_drift_for_a_changed_managed_matcher",
        focus=("tests.test_pc_install_contract",),
    ),
    Guard(
        name="install drift: keep duplicates distinguishable",
        relative="scripts/install_pc.py",
        old="        shape[event].append(carried)",
        new="        shape[event][:] = [carried]",
        pinned_by="test_check_reports_drift_for_a_duplicated_managed_entry",
        focus=("tests.test_pc_install_contract",),
    ),
    Guard(
        name="next action: a settled dependency is not pending",
        relative="agent_harness/graph.py",
        old='                if self.node(dependency)["status"] not in SETTLED',
        new='                if self.node(dependency)["status"] != "verified"',
        pinned_by="test_a_blocked_node_waits_only_for_dependencies_that_can_still_move",
        focus=("tests.test_graph_contract",),
    ),
    Guard(
        name="repo read: the prefilter is what avoids the parse",
        relative="agent_harness/journal.py",
        old="                if marker not in line:\n                    continue",
        new="                if False:\n                    continue",
        pinned_by="test_a_record_from_another_repository_is_never_parsed",
        focus=("tests.test_journal_contract",),
    ),
    Guard(
        # Reverting the call site drops the repository filter with it, which
        # is a change in what is reported rather than in what it costs. The
        # test that objects is the one that was already there.
        name="repo read: the call site still filters by repository",
        relative="agent_harness/session_hooks.py",
        old="    history = list(journal.read_repo(repo))",
        new="    history = list(journal.read())",
        pinned_by="test_a_session_in_another_repository_is_not_reported",
        focus=("tests.test_session_hook_contract",),
    ),
    Guard(
        name="repo read: the parsed field decides, not the substring",
        relative="agent_harness/journal.py",
        old='                    and entry.get("repo") == repo\n                ):',
        new="                ):",
        pinned_by="test_a_repository_named_only_inside_another_field_is_not_returned",
        focus=("tests.test_journal_contract",),
    ),
    Guard(
        name="repo read: build the marker with the encoder that wrote the line",
        relative="agent_harness/journal.py",
        old="    marker = '\"repo\":' + json.dumps(repo)",
        new="    marker = '\"repo\":\"' + repo + '\"'",
        pinned_by="test_a_path_needing_json_escaping_still_matches_its_own_records",
        focus=("tests.test_journal_contract",),
    ),
    Guard(
        name="codex timestamps: rendered into the hooks' own form",
        relative="agent_harness/codex_sessions.py",
        old="        return utc_from_epoch(number)",
        new="        return number  # type: ignore[return-value]",
        pinned_by="test_a_codex_timestamp_sorts_against_a_hook_timestamp",
        focus=("tests.test_conformance_contract",),
    ),
    Guard(
        name="codex timestamps: millisecond columns are told apart",
        relative="agent_harness/codex_sessions.py",
        old="    if number > 100_000_000_000:\n        number //= 1000",
        new="    if False:\n        number //= 1000",
        pinned_by="test_a_millisecond_column_names_the_same_instant_as_a_second_one",
        focus=("tests.test_conformance_contract",),
    ),
    Guard(
        name="codex timestamps: every arm of the converter's handler",
        relative="agent_harness/codex_sessions.py",
        # Narrowed to one arm rather than emptied. Each arm is reached by a
        # different magnitude after the millisecond division — 1e18 raises
        # ValueError, 1e21 OSError, 1e24 OverflowError — so a test that only
        # reached the first would pass while two thirds of the handler went
        # unexercised. That was the state a reviewer found.
        old="    except (OSError, OverflowError, ValueError):",
        new="    except ValueError:",
        pinned_by="test_a_column_too_large_for_the_platform_is_refused_not_raised",
        focus=("tests.test_conformance_contract",),
    ),
    Guard(
        name="journal write: the repository is never shed",
        relative="agent_harness/journal.py",
        old='SHEDDABLE_FIELDS = ("snapshot", "reason", "source", "actor", "command")',
        new='SHEDDABLE_FIELDS = ("snapshot", "reason", "source", "actor", "command", "repo")',
        pinned_by="test_a_record_that_cannot_name_its_repository_is_not_written",
        focus=("tests.test_journal_contract",),
    ),
    Guard(
        name="bounded open: the fstat after the open refuses a FIFO too",
        relative="agent_harness/paths.py",
        old="        if not stat.S_ISREG(opened.st_mode):\n            raise FileRefusal(REFUSAL_NOT_A_REGULAR_FILE, path)",
        new="        if False:\n            raise FileRefusal(REFUSAL_NOT_A_REGULAR_FILE, path)",
        # Pinned on the window this check exists for, not on a FIFO being
        # refused: the `lstat` check above answers that first, so an audit
        # reported this guard uncovered. The test hands the pre-open check
        # the stat of a regular file while the path is a pipe.
        pinned_by="test_a_pipe_that_looked_regular_is_refused_after_the_open",
        focus=("tests.test_journal_contract",),
    ),
    Guard(
        name="bounded open: the descriptor is the file that was asked for",
        relative="agent_harness/paths.py",
        old="        if (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino):",
        new="        if False:",
        pinned_by="test_a_file_swapped_for_another_between_the_checks_is_refused",
        focus=("tests.test_journal_contract",),
    ),
    Guard(
        name="graph store: reads through the bounded opener",
        relative="agent_harness/storage.py",
        old="            descriptor = open_bounded_regular_file(self.path, MAX_GRAPH_BYTES)",
        # Opened non-blocking rather than plainly. A plain `os.open` on the
        # FIFO the pinning test creates blocks for ever, so the mutant hung
        # the suite and the audit died on its own 900-second timeout instead
        # of reporting anything. A mutation has to fail, not wait.
        new='            descriptor = os.open(\n                self.path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)\n            )',
        pinned_by="test_a_graph_reached_through_a_symlink_is_refused",
        focus=("tests.test_storage_and_cli_contract",),
    ),
    Guard(
        name="contested: only an observed window may contest",
        relative="agent_harness/conformance.py",
        old='        if bounds == BOUNDS_OBSERVED:\n            by_repo.setdefault(verdict["repo"], []).append(verdict)',
        new='        if True:\n            by_repo.setdefault(verdict["repo"], []).append(verdict)',
        pinned_by="test_a_codex_row_lifetime_does_not_contest_an_observed_session",
        focus=("tests.test_conformance_contract",),
    ),
    Guard(
        name="contested: an observed window still contests another",
        relative="agent_harness/conformance.py",
        old="        if bounds == BOUNDS_OBSERVED:",
        new="        if bounds == BOUNDS_THREAD_LIFETIME:",
        pinned_by="test_two_observed_sessions_still_contest_each_other",
        focus=("tests.test_conformance_contract",),
    ),
    Guard(
        name="codex timestamps: an infinite column costs one session only",
        relative="agent_harness/codex_sessions.py",
        old="    except (TypeError, ValueError, OverflowError):",
        new="    except (TypeError, ValueError):",
        pinned_by="test_an_infinite_column_costs_one_session_and_not_every_session",
        focus=("tests.test_conformance_contract",),
    ),
    Guard(
        name="contested: a verdict must say what its window is",
        relative="agent_harness/conformance.py",
        old="        if bounds not in BOUNDS:",
        new="        if False:",
        pinned_by="test_a_verdict_that_says_nothing_about_its_window_is_refused",
        focus=("tests.test_conformance_contract",),
    ),
    Guard(
        name="contested: the runner-up keeps a session off itself",
        relative="agent_harness/conformance.py",
        old="        chosen = runner_up if best[1] == index else best",
        new="        chosen = best",
        pinned_by="test_the_narrowed_search_agrees_with_comparing_every_pair",
        focus=("tests.test_conformance_contract",),
    ),
    Guard(
        name="contested: a missing bound keeps a session out of the ordering",
        relative="agent_harness/conformance.py",
        old="        (verdict for verdict in group if _bounded(verdict)),",
        new="        (verdict for verdict in group),",
        pinned_by="test_the_narrowed_search_agrees_with_comparing_every_pair",
        focus=("tests.test_conformance_contract",),
    ),
    Guard(
        name="doctor: the runtime config goes through the bounded opener",
        relative="agent_harness/cli.py",
        old="        descriptor = open_bounded_regular_file(config, MAX_RUNTIME_CONFIG_BYTES)",
        # Non-blocking, so the mutant fails instead of hanging the suite for
        # the audit's full timeout — the mistake made once already on the
        # graph store's own guard.
        new='        descriptor = os.open(\n            config, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)\n        )',
        # Pinned on the size bound, which is the only outcome that changes.
        # A pipe does not work: a non-blocking plain open reads it as empty
        # and reports what the refusal reports. Nor does a symlink:
        # `user_data_path` refuses a link before this function opens
        # anything. Both of those tests are kept for what they do hold, but
        # neither could tell this line's presence from its absence.
        pinned_by="test_a_runtime_config_over_the_bound_is_refused_rather_than_read",
        focus=("tests.test_conformance_contract",),
    ),
    Guard(
        name="doctor: a month refused before it opens is still named",
        relative="agent_harness/journal.py",
        old='            skipped.append((path.name, "not a plain file"))',
        new="            pass",
        pinned_by="test_a_month_refused_before_it_is_opened_is_reported",
        focus=("tests.test_conformance_contract",),
    ),
    Guard(
        name="doctor: a month that cannot be opened is named",
        relative="agent_harness/journal.py",
        old='            skipped.append((path.name, "cannot be opened"))',
        new="            pass",
        pinned_by="test_a_month_that_passes_its_name_and_cannot_be_opened_is_reported",
        focus=("tests.test_conformance_contract",),
    ),
    Guard(
        name="doctor: a readable month is not named as skipped",
        relative="agent_harness/journal.py",
        old="        handle = open_month(path)\n        if handle is None:",
        new="        handle = open_month(path)\n        if True:",
        pinned_by="test_a_readable_month_is_not_reported_as_unreadable",
        focus=("tests.test_conformance_contract",),
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
    # `shutil` rather than `rsync`: the same copy with the same exclusions is
    # already made this way in `tests/__init__.py` and by the installer, and
    # `rsync` is not present on Windows, which this project otherwise tests.
    shutil.copytree(LIVE, root, ignore=shutil.ignore_patterns(*COPY_EXCLUSIONS))
    assert_outside_live(root)
    return root


def run_suite(root: Path, modules: tuple[str, ...]) -> set[tuple[str, str]]:
    """Return the names of tests that failed or errored in *root*."""

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
    return parse_failures(done.stderr)


def parse_failures(stderr: str) -> set[tuple[str, str]]:
    """Return (method, context) for each test unittest printed as failing.

    Both halves of the line are kept, because neither is sufficient alone and
    which one carries what depends on the interpreter:

        3.10   FAIL: test_x (tests.test_mod.Case)
        3.11+  FAIL: test_x (tests.test_mod.Case.test_x)

    unittest only began appending the method to the parenthesised id in 3.11.
    Reading the method from the *end* of that id therefore compared the class
    name on 3.10, so no guard could ever match and the audit reported all
    eighteen as caught by the wrong test — on the project's own minimum
    interpreter, and on one of the two CI legs. The bare half before the
    parenthesis is present on every version, so the method comes from there
    and the module comes from the front of the id.
    """

    found: set[tuple[str, str]] = set()
    for method, context in re.findall(
        r"^(?:FAIL|ERROR): (\S+) \(([\w.]+)", stderr, re.MULTILINE
    ):
        found.add((method, context))
    return found


def render(caught: set[tuple[str, str]]) -> str:
    """Show the failures as unittest would name them, for a reader."""

    return ", ".join(sorted(f"{context}.{method}" for method, context in caught))


def objected(caught: set[tuple[str, str]], guard: Guard) -> bool:
    """Did *this guard's* named test object, in the module it was declared in?

    Both halves are required, and the module half is why the qualified
    identifier is captured at all. Comparing only the final component is
    comparing the bare method name, which two modules may share: a guard
    could then be credited to a test that never ran while its author
    believed the other one pinned it. A reviewer demonstrated exactly that,
    with a same-named no-op bound rather than defined so a source scan
    could not see it, and the guard still read as pinned.
    """

    if not guard.focus:
        # Refused rather than waved through. Falling back to the bare name
        # here would re-open, for any guard authored without a focus, the
        # exact hole the module requirement closes — a default that quietly
        # disables a protection, which is a mistake this file has already
        # made once elsewhere.
        raise SystemExit(
            f"{guard.name}: a guard with a named test must declare the module "
            "it lives in, so the credit can be checked against it."
        )
    for method, context in caught:
        if method != guard.pinned_by:
            continue
        # The module is the front of the parenthesised id on every version:
        # `tests.test_mod.Case` and `tests.test_mod.Case.test_x` both begin
        # with the module. Matched by prefix so a nested test package works.
        if any(
            context == module or context.startswith(f"{module}.")
            for module in guard.focus
        ):
            return True
    return False


def collected_names(tests_dir: Path) -> dict[str, list[str]]:
    """Map each test method name to the qualified ids unittest collects for it.

    Asked of the loader rather than of the source text. Counting `def` lines
    missed a test bound rather than defined — a reviewer used exactly that to
    smuggle a same-named no-op past the check — and miscounted a name that
    also appeared inside an indented docstring.
    """

    loader = unittest.defaultTestLoader
    found: dict[str, list[str]] = {}

    def walk(suite: object) -> None:
        for item in suite:  # type: ignore[attr-defined]
            if isinstance(item, unittest.TestCase):
                identifier = item.id()
                found.setdefault(identifier.split(".")[-1], []).append(identifier)
            else:
                walk(item)

    # start at the package and name the top level explicitly: on 3.10
    # `discover` refuses a start directory that is not importable, and a
    # temporary fixture root never is.
    walk(
        loader.discover(
            str(tests_dir),
            pattern="test_*.py",
            top_level_dir=str(tests_dir.parent),
        )
    )
    return found


def declared_once(guards: tuple[Guard, ...], tests_dir: Path | None = None) -> None:
    """Refuse a declared name that does not resolve to exactly one test.

    Checked before any mutation runs, so the audit refuses rather than
    reporting a guard as pinned by whichever same-named test happened to
    fail. Zero is refused as well as many: a stale name would otherwise read
    as a guard that lost its cover rather than as an entry to update.
    """

    collected = collected_names(tests_dir if tests_dir is not None else LIVE / "tests")
    for guard in guards:
        if not guard.pinned_by:
            continue
        found = collected.get(guard.pinned_by, [])
        if len(found) == 1:
            continue
        where = ", ".join(sorted(found)) or "nowhere"
        raise SystemExit(
            f"{guard.name}: its named test {guard.pinned_by} resolves to "
            f"{len(found)} collected tests ({where}). A guard can only be "
            "credited to one test; rename the duplicate, or point the guard "
            "at the test you mean."
        )


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
    declared_once(selected)
    print(f"auditing {len(selected)} guards ({'focused' if fast else 'full suite'})")
    baseline_root = copy_repository()
    baseline_modules = _modules(selected, fast)
    baseline = run_suite(baseline_root, baseline_modules)
    print(f"baseline failures: {sorted(baseline) or 'none'}\n")

    uncovered: list[Guard] = []
    wrong_test: list[tuple[Guard, set[tuple[str, str]]]] = []
    contradicted: list[tuple[Guard, set[tuple[str, str]]]] = []
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
            print(f"      caught: {render(caught)}")
        elif guard.pinned_by and not objected(caught, guard):
            wrong_test.append((guard, caught))
            print(f"  {guard.name:52} caught, but not by the named test")
            print(f"      named:  {guard.pinned_by}")
            print(f"      caught: {render(caught)}")
        else:
            # `objected` is already known true here: the branch above took
            # every case where it is not, so re-asking it could only produce
            # the arm that cannot be reached.
            print(f"  {guard.name:52} pinned by {guard.pinned_by}")
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
