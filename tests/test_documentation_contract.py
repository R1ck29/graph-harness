from __future__ import annotations

import re
import unittest
from pathlib import Path

from agent_harness import (
    conformance,
    effectiveness,
    journal,
    session_hooks,
    worktree,
)
from scripts import install_pc

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

    def test_the_event_table_names_no_event_the_code_cannot_write(self) -> None:
        # The converse of the test above, and the one that was missing. A
        # reviewer once found the reference describing a `Bash` event that
        # had never been built, and nothing failed: the table could gain a
        # row for a signal that did not exist, because only code-to-docs was
        # checked. A promise of observation that is not kept is worse than
        # an admitted blind spot, because a reader stops looking.
        reference = _read("conformance.md")
        table = re.findall(r"^\| `([a-z_]+)` \|", reference, flags=re.MULTILINE)
        self.assertTrue(table, "the event table was not found")

        named = set(table) - set(conformance.VERDICTS)
        self.assertEqual(set(journal.EVENTS), named)

    def test_no_document_promises_silence_on_a_failed_observation(self) -> None:
        # how-it-works.md claimed that when the observation failed — no
        # version control, or a check that timed out — the session was
        # reported as `unobserved` rather than guessed at. It is not: such a
        # session is judged on its edit records like any other, so one that
        # edited and skipped the graph is reported as a bypass. The claim
        # also contradicted conformance.md, which had it right. A promise of
        # restraint the code does not practise is the same defect class as a
        # promise of observation it does not make.
        state = {
            "session_id": "s",
            "client": "claude",
            "repo": "r",
            "opened": {"ts": "2026-09-01T00:00:00+00:00", "snapshot": {"git": False}},
            "ended": {"ts": "2026-09-01T00:01:00+00:00"},
            "closed": True,
            "transitions": 0,
            "records": [
                {
                    "event": "edit",
                    "tool": "Edit",
                    "path_id": f"{index:012x}",
                    "outside": False,
                }
                for index in range(6)
            ],
        }

        judged = conformance._judge(state)

        self.assertEqual("bypass", judged["verdict"])
        self.assertNotIn("reported as unobserved", _read("how-it-works.md"))

    def test_the_watcher_count_matches_the_installer(self) -> None:
        # The guide said three watchers where the installer manages four.
        spelled = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}

        managed = len(install_pc.MANAGED_HOOK_EVENTS)

        self.assertIn(f"the same {spelled[managed]} watchers", _read("how-it-works.md"))

    def test_the_reference_lists_exactly_the_refusal_reasons_that_exist(self) -> None:
        # Read out of one fenced block, not scanned for anywhere in the
        # document. The first version of this test collected every backticked
        # lowercase token in the file and then filtered them by membership in
        # `REFUSALS`, which made the comparison one-directional by
        # construction: it could only ever check that every reason appears
        # somewhere, so documenting a reason the code cannot emit passed. It
        # leaked in the direction it did check, too — deleting `unreadable`
        # from the list passed, because the same word is backticked five
        # words earlier as the name of the field the list belongs to.
        #
        # A bounded block cannot do either. Whatever is inside it is the
        # documented set, and the assertion below is a real set equality.
        reference = _read("conformance.md")
        # findall, not search: `search` takes the first block, so a second
        # contradictory one placed below it would go unchecked. Requiring
        # exactly one closes that and costs a line.
        blocks = re.findall(r"```refusals\n(.*?)```", reference, flags=re.DOTALL)
        self.assertEqual(1, len(blocks), "expected exactly one refusals block")

        documented = set(blocks[0].split())

        self.assertEqual(set(effectiveness.REFUSALS), documented)

    def test_the_refusal_names_are_the_ones_the_loader_can_return(self) -> None:
        # `REFUSALS` is only worth holding documents to if it is itself the
        # truth. Every `REFUSAL_*` constant must appear in it, so a new
        # reason cannot be defined and quietly left out of the set the
        # documentation is checked against.
        defined = {
            value
            for name, value in vars(effectiveness).items()
            if name.startswith("REFUSAL_") and isinstance(value, str)
        }

        self.assertEqual(defined, set(effectiveness.REFUSALS))

    def test_every_bound_the_reference_names_exists_in_the_code(self) -> None:
        reference = _read("conformance.md")
        defined = {
            "MAX_JOURNAL_LINE_BYTES": journal.MAX_JOURNAL_LINE_BYTES,
            "MAX_JOURNAL_MONTHS": journal.MAX_JOURNAL_MONTHS,
            "MAX_UNTRACKED_BYTES": worktree.MAX_UNTRACKED_BYTES,
            "WARN_MIN_FILES": session_hooks.WARN_MIN_FILES,
            "WARN_MIN_EDITS": session_hooks.WARN_MIN_EDITS,
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
        readme = (REPOSITORY / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("The hook exits immediately when no", readme)
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


class SignalDocumentationTests(unittest.TestCase):
    """The documentation must not outlive the design it describes.

    Every one of these pins a sentence that was true of a superseded design
    and would have been left behind. A review of the round-three failure named
    shipping a knowingly false document as its own defect, separate from the
    code it described.
    """

    def _current_documents(self) -> dict[str, str]:
        """Return the documentation a reader is pointed at today.

        Two trees are excluded, for two different reasons, and both are worth
        stating rather than leaving as an unexplained glob.

        `tasks/artifacts/` holds verbatim records of what independent
        reviewers reported. Correcting one to match today's code would falsify
        a record of what somebody else observed, which is the opposite of what
        this repository is for; each carries a header saying so.

        `docs/superpowers/` holds dated specs and plans. A plan there quotes
        this very assertion as the test it asks an implementer to write, so
        including that tree makes this contract unsatisfiable by construction.
        They also record why an approach failed, which is the most useful
        thing in the repository.

        Everything a reader is actually pointed at — `docs/*.md`, `core/*.md`
        and the live plan in `tasks/*.md` — is covered.
        """

        found = {}
        for path in (
            sorted(DOCS.glob("*.md"))
            + sorted((REPOSITORY / "core").glob("*.md"))
            + sorted((REPOSITORY / "tasks").glob("*.md"))
        ):
            found[str(path.relative_to(REPOSITORY))] = path.read_text(encoding="utf-8")
        return found

    def test_every_verbatim_archive_says_it_must_not_be_corrected(self) -> None:
        # The exclusion above is only honest while the excluded files say what
        # they are.
        for path in sorted((REPOSITORY / "tasks" / "artifacts").glob("*.md")):
            text = path.read_text(encoding="utf-8")
            if "committed_size" in text or "keeps a pull" in text:
                self.assertIn("Verbatim archived record", text, str(path))

    def test_no_document_claims_commit_sizing_distinguishes_a_pull(self) -> None:
        for name, text in self._current_documents().items():
            self.assertNotIn("committed_size", text, name)
            self.assertNotIn("keeps a pull", text, name)

    def test_no_document_still_names_the_removed_verdict_as_a_verdict(self) -> None:
        reference = _read("conformance.md")
        table = reference[reference.index("| Verdict |") :]

        self.assertNotIn("`read_only` |", table)
        self.assertIn("`unattributed`", table)

    def test_the_reference_states_the_tree_is_context_and_never_evidence(self) -> None:
        reference = _read("conformance.md")

        self.assertIn("never evidence", reference.replace("**", ""))
        self.assertIn(
            "blind spot", reference.lower() + _read("how-it-works.md").lower()
        )

    def test_the_edit_hook_privacy_rule_is_in_the_security_notes(self) -> None:
        security = _read("security.md")

        self.assertIn("path_id", security)
        self.assertIn("never recorded", security)

    def test_the_protocol_carries_the_escape_hatches_and_their_rule(self) -> None:
        protocol = _read("../core/protocol.md")
        machine = _read("../core/state-machine.md")

        for text in (protocol, machine):
            self.assertIn("grant-attempt", text)
            self.assertIn("supersede", text)
        self.assertIn("superseded", machine)
        self.assertIn("explicit human instruction", protocol + machine)

    def test_the_platform_notes_record_the_measurements_with_reproductions(
        self,
    ) -> None:
        platform = _read("platform-compatibility.md")

        self.assertIn("PostToolUse", platform)
        self.assertIn("REBASE_HEAD", platform)
        self.assertIn("Reproduction", platform)
        # The Codex result the whole Codex branch rests on.
        self.assertIn("Codex", platform)
