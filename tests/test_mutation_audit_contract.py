"""Contracts for the harness that proves the other guards are held.

This exists because the mapping it encodes used to live only as prose in an
evidence record, and two independent reviewers rebuilt it by hand to check a
claim about it. A harness whose own guarantees are unpinned would be the same
mistake one level up.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import mutation_audit

REPOSITORY = Path(__file__).resolve().parents[1]


class MutationAuditContractTests(unittest.TestCase):
    def test_it_refuses_a_target_inside_the_live_tree(self) -> None:
        # The rule the audit exists to keep. An in-place version of it had a
        # mutation captured by a filesystem snapshot and restored into the
        # repository as though it were real source, so the target is checked
        # before anything is written.
        for inside in (
            REPOSITORY,
            REPOSITORY / "agent_harness",
            REPOSITORY / "does-not-exist-yet" / "deeper",
        ):
            with self.subTest(target=str(inside)):
                with self.assertRaises(SystemExit) as refused:
                    mutation_audit.assert_outside_live(inside)

                self.assertIn(
                    "refusing to mutate inside the live tree", str(refused.exception)
                )

    def test_it_accepts_a_target_outside_the_live_tree(self) -> None:
        # The refusal must not be so broad that the audit cannot run.
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)

        accepted = mutation_audit.assert_outside_live(Path(holder.name) / "repo")

        self.assertTrue(str(accepted))

    def test_a_guard_is_credited_on_either_interpreters_output(self) -> None:
        # The assertion whose absence shipped a regression. unittest only
        # began appending the method to the parenthesised id in 3.11:
        #
        #     3.10   FAIL: test_x (tests.test_mod.Case)
        #     3.11+  FAIL: test_x (tests.test_mod.Case.test_x)
        #
        # Reading the method off the end of that id compared the class name
        # on 3.10, so no guard matched and the audit called all eighteen
        # wrongly-caught, on the project's own minimum interpreter. The
        # previous version of this test fed only the 3.11 shape, so it was
        # green on 3.10 while the tool was broken. Both shapes are fed here.
        guard = mutation_audit.Guard(
            name="probe",
            relative="agent_harness/journal.py",
            old="x",
            new="y",
            pinned_by="test_x",
            focus=("tests.test_mod",),
        )

        for label, line in (
            ("3.10", "FAIL: test_x (tests.test_mod.Case)"),
            ("3.11+", "FAIL: test_x (tests.test_mod.Case.test_x)"),
            ("error", "ERROR: test_x (tests.test_mod.Case.test_x)"),
        ):
            with self.subTest(interpreter=label):
                parsed = mutation_audit.parse_failures(line + "\n")

                self.assertTrue(parsed, "nothing parsed")
                self.assertTrue(mutation_audit.objected(parsed, guard))

        # And the method really is read from the bare half, not from the id.
        self.assertEqual(
            {("test_x", "tests.test_mod.Case")},
            mutation_audit.parse_failures("FAIL: test_x (tests.test_mod.Case)\n"),
        )

    def test_a_guard_is_credited_only_in_the_module_it_declared(self) -> None:
        # The credit is structural: the right method name in the wrong module
        # is not this guard's test. A reviewer showed that comparing the bare
        # name alone let a same-named no-op in another module read as the
        # objector while the author believed it was doing the work.
        guard = mutation_audit.Guard(
            name="probe",
            relative="agent_harness/journal.py",
            old="x",
            new="y",
            pinned_by="test_shared",
            focus=("tests.test_journal_contract",),
        )

        self.assertTrue(
            mutation_audit.objected(
                {("test_shared", "tests.test_journal_contract.Case")}, guard
            )
        )
        self.assertFalse(
            mutation_audit.objected(
                {("test_shared", "tests.test_effectiveness_contract.Case")}, guard
            )
        )
        self.assertFalse(
            mutation_audit.objected(
                {("test_sha", "tests.test_journal_contract.Case")}, guard
            )
        )

    def test_a_nested_test_package_is_still_credited(self) -> None:
        # The module is matched by prefix, so a test package with more
        # components than `tests.test_mod` is not refused for its depth.
        nested = mutation_audit.Guard(
            name="probe",
            relative="agent_harness/journal.py",
            old="x",
            new="y",
            pinned_by="test_shared",
            focus=("tests.pkg.test_mod",),
        )

        self.assertTrue(
            mutation_audit.objected(
                {("test_shared", "tests.pkg.test_mod.Case.test_shared")}, nested
            )
        )

    def test_a_guard_with_a_named_test_must_declare_its_module(self) -> None:
        # Refused, not waved through. Falling back to the bare name for a
        # guard authored without a focus would re-open the exact hole the
        # module requirement closes: a default that quietly disables a
        # protection, which this file has already got wrong once elsewhere.
        focusless = mutation_audit.Guard(
            name="probe",
            relative="agent_harness/journal.py",
            old="x",
            new="y",
            pinned_by="test_shared",
        )

        with self.assertRaises(SystemExit) as refused:
            mutation_audit.objected({("test_shared", "tests.anywhere.Case")}, focusless)

        self.assertIn("must declare the module", str(refused.exception))

    def test_every_guard_with_a_named_test_declares_a_focus(self) -> None:
        for guard in mutation_audit.GUARDS:
            if not guard.pinned_by:
                continue
            with self.subTest(guard=guard.name):
                self.assertTrue(guard.focus, "no module declared")

    def test_every_guard_names_a_distinct_source_and_a_real_file(self) -> None:
        seen: set[tuple[str, str]] = set()
        for guard in mutation_audit.GUARDS:
            with self.subTest(guard=guard.name):
                self.assertTrue((REPOSITORY / guard.relative).is_file())
                self.assertNotEqual(
                    guard.old, guard.new, "the mutation changes nothing"
                )
                key = (guard.relative, guard.old)
                self.assertNotIn(key, seen, "two guards claim the same source")
                seen.add(key)

    def test_every_declared_mutation_still_matches_its_source_exactly_once(
        self,
    ) -> None:
        # The audit refuses to run rather than skip a guard whose source has
        # moved. This is that refusal, checked cheaply on every suite run so
        # the slow audit is not the only thing that notices a rename.
        for guard in mutation_audit.GUARDS:
            with self.subTest(guard=guard.name):
                source = (REPOSITORY / guard.relative).read_text(encoding="utf-8")

                self.assertEqual(
                    1,
                    source.count(guard.old),
                    f"{guard.name}: update its entry in scripts/mutation_audit.py",
                )

    def test_every_named_test_resolves_to_exactly_one_collected_test(self) -> None:
        # Asked of the loader, against the live suite. The previous version
        # counted `def test_` source lines, which cannot see a test bound
        # rather than defined; a reviewer used exactly that to smuggle a
        # same-named no-op past this check, leaving the gap open in the cheap
        # layer while only the slow audit caught it. Calling the audit's own
        # refusal means the two layers cannot disagree.
        mutation_audit.declared_once(mutation_audit.GUARDS)

    def test_names_are_counted_as_the_loader_collects_them(self) -> None:
        # A test bound rather than defined is invisible to a source scan.
        # The fixture package is uniquely named because `tests` is already
        # imported by the running process and would otherwise resolve to it.
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        package = Path(holder.name) / "probetests_collected"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "test_alpha.py").write_text(
            "import unittest\n\n\n"
            "class A(unittest.TestCase):\n"
            "    def test_shared(self):\n"
            '        """test_shared appears here too, indented."""\n'
            "        pass\n",
            encoding="utf-8",
        )
        (package / "test_beta.py").write_text(
            "import unittest\n\n\n"
            "class B(unittest.TestCase):\n"
            "    test_shared = lambda self: None\n",
            encoding="utf-8",
        )

        collected = mutation_audit.collected_names(package)

        self.assertEqual(2, len(collected["test_shared"]), collected["test_shared"])

    def test_a_guard_declared_uncovered_says_why_on_the_same_line(self) -> None:
        # An empty `pinned_by` claims no test can reach the guard. Both such
        # claims originally made here were false, so none remain; this holds
        # any future one to carrying its reason where it is written.
        source = (REPOSITORY / "scripts/mutation_audit.py").read_text(encoding="utf-8")
        declarations = [line for line in source.splitlines() if 'pinned_by=""' in line]
        uncovered = [guard for guard in mutation_audit.GUARDS if not guard.pinned_by]

        self.assertEqual(len(uncovered), len(declarations))
        for line in declarations:
            with self.subTest(line=line.strip()):
                comment = line.split("#", 1)
                self.assertEqual(2, len(comment), "no reason given")
                self.assertTrue(comment[1].strip(), "the reason is empty")

    def test_the_audit_refuses_an_ambiguous_declared_name(self) -> None:
        # Built rather than borrowed: `declared_once` takes the directory, so
        # the fixture is made here instead of leaning on the suite's own
        # duplicate name, and both the many and the none cases are covered.
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        package = Path(holder.name) / "probetests_ambiguous"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        for name in ("alpha", "beta"):
            (package / f"test_{name}.py").write_text(
                "import unittest\n\n\n"
                f"class {name.title()}(unittest.TestCase):\n"
                "    def test_shared(self):\n        pass\n",
                encoding="utf-8",
            )
        guard = mutation_audit.Guard(
            name="probe",
            relative="agent_harness/journal.py",
            old="x",
            new="y",
            pinned_by="test_shared",
        )

        with self.assertRaises(SystemExit) as twice:
            mutation_audit.declared_once((guard,), tests_dir=package)
        self.assertIn("resolves to 2 collected tests", str(twice.exception))

        absent = mutation_audit.Guard(
            name="probe",
            relative="agent_harness/journal.py",
            old="x",
            new="y",
            pinned_by="test_absent",
        )
        with self.assertRaises(SystemExit) as never:
            mutation_audit.declared_once((absent,), tests_dir=package)
        self.assertIn("resolves to 0 collected tests", str(never.exception))
        self.assertIn("nowhere", str(never.exception))

    def test_the_audit_refuses_before_it_copies_anything(self) -> None:
        # A refusal that fires after the tree has been mutated is a weaker
        # guarantee. Proved by making the copier raise if it is reached.
        guard = mutation_audit.Guard(
            name="probe",
            relative="agent_harness/journal.py",
            old="x",
            new="y",
            pinned_by="test_absent_everywhere",
            focus=("tests.test_journal_contract",),
        )

        with mock.patch.object(
            mutation_audit, "copy_repository", side_effect=AssertionError("copied")
        ) as copier:
            with self.assertRaises(SystemExit):
                mutation_audit.audit((guard,), fast=True)

        copier.assert_not_called()


if __name__ == "__main__":
    unittest.main()
