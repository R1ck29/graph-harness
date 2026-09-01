from __future__ import annotations

import re
import unittest
from pathlib import Path

from agent_harness import conformance, journal, session_hooks, worktree

REPOSITORY = Path(__file__).resolve().parents[1]
DOCS = REPOSITORY / "docs"


def _read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


class DocumentationContractTests(unittest.TestCase):
    """Documentation that drifts from the code is worse than none."""

    def test_every_verdict_the_code_can_produce_is_documented(self) -> None:
        reference = _read("conformance.md")
        guide = _read("how-it-works.md")

        for verdict in conformance.VERDICTS:
            self.assertIn(f"`{verdict}`", reference, verdict)
            self.assertIn(f"`{verdict}`", guide, verdict)

    def test_every_recorded_event_is_documented(self) -> None:
        reference = _read("conformance.md")

        for event in journal.EVENTS:
            self.assertIn(f"`{event}`", reference, event)

    def test_every_bound_the_reference_names_exists_in_the_code(self) -> None:
        reference = _read("conformance.md")
        defined = {
            "MAX_JOURNAL_LINE_BYTES": journal.MAX_JOURNAL_LINE_BYTES,
            "MAX_JOURNAL_MONTHS": journal.MAX_JOURNAL_MONTHS,
            "MAX_UNTRACKED_BYTES": worktree.MAX_UNTRACKED_BYTES,
            "MAX_SNAPSHOT_PATHS": worktree.MAX_SNAPSHOT_PATHS,
            "WARN_MIN_FILES": session_hooks.WARN_MIN_FILES,
            "WARN_MIN_LINES": session_hooks.WARN_MIN_LINES,
            "MAX_CANDIDATES": session_hooks.MAX_CANDIDATES,
        }
        named = set(re.findall(r"`(MAX_[A-Z_]+|WARN_[A-Z_]+)`", reference))

        self.assertTrue(named)
        self.assertEqual(set(), named - set(defined), named - set(defined))
        for name in defined:
            self.assertIn(f"`{name}`", reference, name)

    def test_the_security_notes_no_longer_claim_the_hooks_do_not_write(
        self,
    ) -> None:
        # The session hooks write to the journal. Two statements here were
        # true of the older hook and became false with them.
        security = _read("security.md")

        self.assertNotIn("The example hook has no network", security)
        self.assertNotIn("It returns immediately when that repository has no", security)
        self.assertIn("The installed session hooks do write", security)
        self.assertIn("when there is no graph it records the", security)

    def test_the_journal_is_documented_as_forgeable_and_not_an_audit_trail(
        self,
    ) -> None:
        for name in ("security.md", "conformance.md", "how-it-works.md"):
            self.assertIn("audit trail", _read(name), name)

    def test_the_measured_codex_hook_result_is_recorded_with_its_reproduction(
        self,
    ) -> None:
        compatibility = _read("platform-compatibility.md")

        self.assertIn("They did not", compatibility)
        self.assertIn("CODEX_HOME", compatibility)
        self.assertIn("bypass_suspected", compatibility)

    def test_the_plain_language_guide_is_linked_from_the_readme(self) -> None:
        readme = (REPOSITORY / "README.md").read_text(encoding="utf-8")

        self.assertIn("docs/how-it-works.md", readme)


if __name__ == "__main__":
    unittest.main()
