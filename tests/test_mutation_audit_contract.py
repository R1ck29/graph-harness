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
        # The refusal must not be so broad that the audit cannot run at all.
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)

        accepted = mutation_audit.assert_outside_live(Path(holder.name) / "repo")

        self.assertTrue(str(accepted))

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

    def test_a_guard_declared_uncovered_says_why_on_the_same_line(self) -> None:
        # An empty `pinned_by` claims no test can reach the guard. Both such
        # claims I originally made were false — a reviewer wrote the two
        # tests I had called impossible — so none remain, and this holds any
        # future one to carrying its reason where it is written.
        source = (REPOSITORY / "scripts/mutation_audit.py").read_text(encoding="utf-8")
        declarations = [line for line in source.splitlines() if 'pinned_by=""' in line]
        uncovered = [guard for guard in mutation_audit.GUARDS if not guard.pinned_by]

        self.assertEqual(len(uncovered), len(declarations))
        for line in declarations:
            with self.subTest(line=line.strip()):
                comment = line.split("#", 1)
                self.assertEqual(2, len(comment), "no reason given")
                self.assertTrue(comment[1].strip(), "the reason is empty")

    def test_every_named_test_exists_in_the_suite(self) -> None:
        # A guard pointing at a test that no longer exists would report as
        # uncovered and read as a lost guard rather than a stale entry. The
        # slow audit would say so eventually; this says so in a second.
        names = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((REPOSITORY / "tests").glob("test_*.py"))
        )

        for guard in mutation_audit.GUARDS:
            if not guard.pinned_by:
                continue
            with self.subTest(guard=guard.name):
                self.assertIn(f"def {guard.pinned_by}(", names)


if __name__ == "__main__":
    unittest.main()
