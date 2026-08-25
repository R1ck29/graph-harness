from __future__ import annotations

import unittest

from agent_harness.errors import HarnessError
from agent_harness.graph import Graph

from tests.helpers import graph, node


class GraphSchemaAndSchedulingTests(unittest.TestCase):
    def test_validate_accepts_minimal_valid_graph(self) -> None:
        task_graph = Graph.from_dict(graph(node("plan")))

        task_graph.validate()

    def test_validate_rejects_legacy_schema_version_without_canonical_version(
        self,
    ) -> None:
        invalid = graph(node("plan"))
        invalid["schema_version"] = "1.0"
        del invalid["version"]

        with self.assertRaisesRegex(HarnessError, "version must be 1"):
            Graph.from_dict(invalid)

    def test_validate_rejects_missing_required_node_field(self) -> None:
        invalid = node("plan")
        del invalid["assigned_role"]

        with self.assertRaises(HarnessError):
            Graph.from_dict(graph(invalid)).validate()

    def test_validate_rejects_unknown_dependency(self) -> None:
        with self.assertRaises(HarnessError) as error:
            Graph.from_dict(graph(node("implement", depends_on=["missing"]))).validate()

        self.assertIn("missing", str(error.exception))

    def test_validate_rejects_cycles(self) -> None:
        cyclic = graph(node("a", depends_on=["b"]), node("b", depends_on=["a"]))

        with self.assertRaises(HarnessError) as error:
            Graph.from_dict(cyclic).validate()

        self.assertIn("cycle", str(error.exception).lower())

    def test_only_nodes_with_all_verified_dependencies_are_ready(self) -> None:
        task_graph = Graph.from_dict(
            graph(
                node("plan", status="verified"),
                node("implement", depends_on=["plan"]),
                node("review", depends_on=["implement"]),
                node("independent"),
            )
        )

        self.assertEqual(["implement", "independent"], task_graph.ready_node_ids())

    def test_start_rejects_blocked_node_and_illegal_transition(self) -> None:
        task_graph = Graph.from_dict(
            graph(node("upstream"), node("downstream", depends_on=["upstream"]))
        )

        with self.assertRaises(HarnessError):
            task_graph.start("downstream")
        with self.assertRaises(HarnessError):
            task_graph.transition("upstream", "verified")

    def test_transition_rejects_unknown_status(self) -> None:
        task_graph = Graph.from_dict(graph(node("plan")))

        with self.assertRaises(HarnessError):
            task_graph.transition("plan", "done")

    def test_validate_rejects_forged_verified_node_without_evidence(self) -> None:
        forged = node("plan")
        forged["status"] = "verified"

        with self.assertRaises(HarnessError):
            Graph.from_dict(graph(forged))

    def test_validate_rejects_verified_node_without_executor_identity(self) -> None:
        forged = node("plan", status="verified")
        forged.pop("executor_id")

        with self.assertRaises(HarnessError):
            Graph.from_dict(graph(forged))

    def test_validate_rejects_non_object_review_context(self) -> None:
        malformed = node("plan")
        malformed["review_context"] = [{"not": "an object"}]

        with self.assertRaises(HarnessError):
            Graph.from_dict(graph(malformed))

    def test_validate_rejects_malformed_withdrawal_review_history(self) -> None:
        malformed = node("plan")
        malformed["review_history"] = [
            {"verification": {}, "submission_evidence": "not-a-list"}
        ]

        with self.assertRaisesRegex(HarnessError, "review_history"):
            Graph.from_dict(graph(malformed))

    def test_validate_rejects_malformed_live_and_archived_reviews(self) -> None:
        task_graph = Graph.from_dict(graph(node("plan")))
        task_graph.start("plan", executor_id="executor")
        task_graph.submit(
            "plan", [{"kind": "test", "summary": "incomplete"}], actor_id="executor"
        )
        task_graph.verify(
            "plan",
            "uncertain",
            reviewer_id="reviewer",
            reason="more evidence needed",
        )

        malformed_live = task_graph.to_dict()
        del malformed_live["nodes"][0]["verification"]["reviewer_id"]
        with self.assertRaisesRegex(HarnessError, "verification fields"):
            Graph.from_dict(malformed_live)

        task_graph.withdraw_submission("plan", actor_id="executor")
        malformed_history = task_graph.to_dict()
        del malformed_history["nodes"][0]["review_history"][0]["verification"][
            "reviewer_id"
        ]
        with self.assertRaisesRegex(HarnessError, "verification fields"):
            Graph.from_dict(malformed_history)

    def test_status_summary_includes_actionable_next_step_per_node(self) -> None:
        task_graph = Graph.from_dict(graph(node("plan")))

        self.assertIn(
            "start plan", task_graph.status_summary()["nodes"][0]["next_action"]
        )
        task_graph.start("plan", executor_id="executor")
        self.assertIn(
            "submit plan", task_graph.status_summary()["nodes"][0]["next_action"]
        )
        task_graph.submit(
            "plan", [{"kind": "test", "summary": "initial result"}], actor_id="executor"
        )
        task_graph.verify(
            "plan",
            "uncertain",
            reviewer_id="reviewer",
            reason="need stronger evidence",
        )
        self.assertIn(
            "withdraw plan", task_graph.status_summary()["nodes"][0]["next_action"]
        )


class GraphVerificationTests(unittest.TestCase):
    def _submitted_graph(self) -> Graph:
        task_graph = Graph.from_dict(graph(node("implement")))
        task_graph.start("implement", executor_id="implementer-1")
        task_graph.submit(
            "implement",
            [{"kind": "test", "value": "python -m unittest: pass"}],
            actor_id="implementer-1",
        )
        return task_graph

    def test_pass_is_rejected_without_required_evidence(self) -> None:
        task_graph = Graph.from_dict(graph(node("implement")))
        task_graph.start("implement", executor_id="implementer-1")
        task_graph.submit("implement", [], actor_id="implementer-1")

        with self.assertRaises(HarnessError):
            task_graph.verify("implement", "PASS", reviewer_id="reviewer-1")

        self.assertEqual(
            "awaiting_verification", task_graph.node("implement")["status"]
        )

    def test_reviewer_id_must_be_independent_from_submitter(self) -> None:
        task_graph = self._submitted_graph()

        with self.assertRaises(HarnessError):
            task_graph.verify("implement", "PASS", reviewer_id="implementer-1")

        self.assertEqual(
            "awaiting_verification", task_graph.node("implement")["status"]
        )

    def test_submitter_cannot_replace_executor_identity(self) -> None:
        task_graph = Graph.from_dict(graph(node("implement")))
        task_graph.start("implement", executor_id="executor-1")

        with self.assertRaises(HarnessError):
            task_graph.submit(
                "implement",
                [{"kind": "test", "value": "pass"}],
                actor_id="different-actor",
            )

        self.assertEqual("running", task_graph.node("implement")["status"])

    def test_independent_reviewer_can_verify_evidenced_submission(self) -> None:
        task_graph = self._submitted_graph()

        task_graph.verify(
            "implement",
            "PASS",
            reviewer_id="reviewer-1",
            checked_criteria=["relevant tests pass"],
            review_evidence=[
                {
                    "kind": "test",
                    "summary": "reviewer reran the test suite successfully",
                }
            ],
        )

        verified = task_graph.node("implement")
        self.assertEqual("verified", verified["status"])
        self.assertEqual("reviewer-1", verified["reviewer_id"])

    def test_pass_requires_explicit_review_criteria_and_review_evidence(self) -> None:
        task_graph = self._submitted_graph()

        with self.assertRaisesRegex(HarnessError, "explicit checked criteria"):
            task_graph.verify(
                "implement",
                "PASS",
                reviewer_id="reviewer-1",
                review_evidence=[{"kind": "test", "summary": "review pass"}],
            )

        with self.assertRaisesRegex(HarnessError, "explicit review evidence"):
            task_graph.verify(
                "implement",
                "PASS",
                reviewer_id="reviewer-1",
                checked_criteria=["relevant tests pass"],
            )

        with self.assertRaisesRegex(HarnessError, "every criterion"):
            task_graph.verify(
                "implement",
                "PASS",
                reviewer_id="reviewer-1",
                checked_criteria=[],
                review_evidence=[],
            )

        self.assertEqual(
            "awaiting_verification", task_graph.node("implement")["status"]
        )

    def test_uncertain_submission_can_be_withdrawn_and_replaced_in_same_attempt(
        self,
    ) -> None:
        task_graph = self._submitted_graph()
        original = task_graph.node("implement")
        original["review_context"] = {
            "relevant_files": ["src/old.py"],
            "diff_artifact": "artifacts/old.diff",
            "test_output_artifact": "artifacts/old-tests.txt",
        }
        original_submitted_at = original["submitted_at"]
        task_graph.verify(
            "implement",
            "UNCERTAIN",
            reviewer_id="reviewer-1",
            reason="test output is incomplete",
        )

        task_graph.withdraw_submission("implement", actor_id="implementer-1")
        task_graph.submit(
            "implement",
            [{"kind": "test", "summary": "complete test output"}],
            actor_id="implementer-1",
        )

        resubmitted = task_graph.node("implement")
        self.assertEqual("awaiting_verification", resubmitted["status"])
        self.assertEqual(1, resubmitted["attempts"])
        self.assertEqual("complete test output", resubmitted["evidence"][0]["summary"])
        self.assertIsNone(resubmitted.get("verification"))
        self.assertEqual(
            "test output is incomplete",
            resubmitted["review_history"][-1]["verification"]["reason"],
        )
        archived = resubmitted["review_history"][-1]
        self.assertEqual(original_submitted_at, archived["submitted_at"])
        self.assertEqual(
            {
                "relevant_files": ["src/old.py"],
                "diff_artifact": "artifacts/old.diff",
                "test_output_artifact": "artifacts/old-tests.txt",
            },
            archived["review_context"],
        )

    def test_withdraw_requires_uncertain_review_and_matching_executor(self) -> None:
        task_graph = self._submitted_graph()

        with self.assertRaisesRegex(HarnessError, "UNCERTAIN"):
            task_graph.withdraw_submission("implement", actor_id="implementer-1")

        task_graph.verify(
            "implement",
            "uncertain",
            reviewer_id="reviewer-1",
            reason="need a clearer artifact",
        )
        self.assertEqual([], task_graph.node("implement")["verification"]["evidence"])
        self.assertEqual(
            [], task_graph.node("implement")["verification"]["checked_criteria"]
        )
        with self.assertRaisesRegex(HarnessError, "must match the executor"):
            task_graph.withdraw_submission("implement", actor_id="someone-else")

    def test_review_packet_carries_reproducible_context_artifacts(self) -> None:
        task_graph = Graph.from_dict(graph(node("implement")))
        task_graph.start("implement", executor_id="executor-1")
        task_graph.submit(
            "implement",
            [{"kind": "test", "value": "pass"}],
            actor_id="executor-1",
            review_context={
                "relevant_files": ["src/example.py"],
                "diff_artifact": "artifacts/change.diff",
                "test_output_artifact": "artifacts/tests.txt",
            },
        )

        packet = task_graph.review_packet("implement")

        self.assertEqual(["src/example.py"], packet["relevant_files"])
        self.assertEqual("artifacts/change.diff", packet["diff_artifact"])
        self.assertEqual("artifacts/tests.txt", packet["test_output_artifact"])

    def test_failed_verification_preserves_structured_failure_history(self) -> None:
        task_graph = self._submitted_graph()

        task_graph.verify(
            "implement",
            "FAIL",
            reviewer_id="reviewer-1",
            reason="acceptance test fails on empty input",
            failed_criteria=["relevant tests pass"],
            review_evidence=[
                {"kind": "test", "summary": "reviewer reproduced the failure"}
            ],
        )

        failed = task_graph.node("implement")
        self.assertEqual("failed", failed["status"])
        self.assertEqual(
            "acceptance test fails on empty input", failed["failure_reason"]
        )
        self.assertEqual(
            "acceptance test fails on empty input",
            failed["failure_history"][-1]["reason"],
        )

    def test_failure_invalidates_only_descendants_and_keeps_unrelated_branch(
        self,
    ) -> None:
        task_graph = Graph.from_dict(
            graph(
                node("a", status="verified"),
                node("b", depends_on=["a"]),
                node("c", depends_on=["b"]),
                node("d", depends_on=["c"]),
                node("unrelated", status="verified"),
            )
        )
        task_graph.start("b", executor_id="impl")
        task_graph.submit("b", [{"kind": "test", "value": "fails"}], actor_id="impl")

        invalidated = task_graph.verify(
            "b",
            "FAIL",
            reviewer_id="reviewer",
            reason="bug",
            failed_criteria=["relevant tests pass"],
            review_evidence=[
                {"kind": "test", "summary": "reviewer reproduced the bug"}
            ],
        )

        self.assertEqual(["c", "d"], invalidated)
        self.assertEqual("failed", task_graph.node("b")["status"])
        self.assertEqual("invalidated", task_graph.node("c")["status"])
        self.assertEqual("invalidated", task_graph.node("d")["status"])
        self.assertEqual("verified", task_graph.node("a")["status"])
        self.assertEqual("verified", task_graph.node("unrelated")["status"])
        self.assertEqual(
            ["c", "d"],
            task_graph.node("b")["failure_history"][-1]["affected_downstream_nodes"],
        )

    def test_retry_respects_max_attempts_and_retains_each_failure_reason(self) -> None:
        task_graph = Graph.from_dict(graph(node("fix", max_attempts=2)))
        task_graph.start("fix", executor_id="impl-1")
        task_graph.submit(
            "fix", [{"kind": "test", "value": "first"}], actor_id="impl-1"
        )
        task_graph.verify(
            "fix",
            "FAIL",
            reviewer_id="reviewer-1",
            reason="first failure",
            failed_criteria=["relevant tests pass"],
            review_evidence=[
                {"kind": "test", "summary": "reviewer observed first failure"}
            ],
        )

        task_graph.retry("fix")
        self.assertEqual("ready", task_graph.node("fix")["status"])
        task_graph.start("fix", executor_id="impl-2")
        task_graph.submit(
            "fix", [{"kind": "test", "value": "second"}], actor_id="impl-2"
        )
        task_graph.verify(
            "fix",
            "FAIL",
            reviewer_id="reviewer-2",
            reason="second failure",
            failed_criteria=["relevant tests pass"],
            review_evidence=[
                {"kind": "test", "summary": "reviewer observed second failure"}
            ],
        )

        with self.assertRaises(HarnessError):
            task_graph.retry("fix")
        failed = task_graph.node("fix")
        self.assertEqual(2, failed["attempts"])
        self.assertEqual(
            ["first failure", "second failure"],
            [x["reason"] for x in failed["failure_history"]],
        )

    def test_fail_requires_observed_evidence_and_failed_criterion(self) -> None:
        task_graph = Graph.from_dict(graph(node("implement")))
        task_graph.start("implement", executor_id="executor")
        task_graph.submit("implement", [], actor_id="executor")

        with self.assertRaises(HarnessError):
            task_graph.verify(
                "implement",
                "fail",
                reviewer_id="reviewer",
                reason="suspected failure",
                failed_criteria=["relevant tests pass"],
            )

        self.assertEqual(
            "awaiting_verification", task_graph.node("implement")["status"]
        )

    def test_fail_requires_explicit_reviewer_evidence(self) -> None:
        task_graph = self._submitted_graph()

        with self.assertRaisesRegex(HarnessError, "explicit review evidence"):
            task_graph.verify(
                "implement",
                "fail",
                reviewer_id="reviewer",
                reason="reviewer found a defect",
                failed_criteria=["relevant tests pass"],
            )

        self.assertEqual(
            "awaiting_verification", task_graph.node("implement")["status"]
        )

    def test_fail_defaults_checked_criteria_to_only_failed_criteria(self) -> None:
        task = node("implement")
        task["acceptance_criteria"] = ["unit tests pass", "docs are current"]
        task_graph = Graph.from_dict(graph(task))
        task_graph.start("implement", executor_id="executor")
        task_graph.submit(
            "implement",
            [
                {"criterion": "unit tests pass", "kind": "test", "summary": "fail"},
                {
                    "criterion": "docs are current",
                    "kind": "inspection",
                    "summary": "current",
                },
            ],
            actor_id="executor",
        )

        task_graph.verify(
            "implement",
            "fail",
            reviewer_id="reviewer",
            reason="unit test failure",
            failed_criteria=["unit tests pass"],
            review_evidence=[
                {
                    "criterion": "unit tests pass",
                    "kind": "test",
                    "summary": "reviewer reproduced failure",
                }
            ],
        )

        self.assertEqual(
            ["unit tests pass"],
            task_graph.node("implement")["verification"]["checked_criteria"],
        )

    def test_downstream_review_can_reopen_verified_faulty_ancestor(self) -> None:
        faulty = node("b", status="verified", depends_on=["a"])
        faulty["acceptance_criteria"] = ["b contract holds"]
        faulty["evidence"][0]["criterion"] = "b contract holds"
        faulty["verification"]["checked_criteria"] = ["b contract holds"]
        faulty["verification"]["evidence"][0]["criterion"] = "b contract holds"
        task_graph = Graph.from_dict(
            graph(
                node("a", status="verified"),
                faulty,
                node("c", depends_on=["b"]),
            )
        )
        task_graph.start("c", executor_id="c-executor")
        task_graph.submit(
            "c", [{"kind": "test", "value": "contract mismatch"}], actor_id="c-executor"
        )

        invalidated = task_graph.verify(
            "c",
            "fail",
            reviewer_id="independent-reviewer",
            reason="b produced an invalid contract",
            faulty_node="b",
            failed_criteria=["relevant tests pass"],
            faulty_criteria=["b contract holds"],
            review_evidence=[
                {"kind": "test", "summary": "reviewer observed contract mismatch"}
            ],
        )

        self.assertEqual(["c"], invalidated)
        self.assertEqual("verified", task_graph.node("a")["status"])
        self.assertEqual("failed", task_graph.node("b")["status"])
        self.assertEqual("invalidated", task_graph.node("c")["status"])
        self.assertEqual(
            "c", task_graph.node("b")["failure_history"][-1]["detected_at_node"]
        )
        self.assertEqual(
            ["relevant tests pass"],
            task_graph.node("b")["failure_history"][-1]["failed_criteria"],
        )
        self.assertEqual(
            ["b contract holds"],
            task_graph.node("b")["failure_history"][-1]["faulty_node_criteria"],
        )
        self.assertEqual("ready", task_graph.retry("b"))


class ReviewerIndependenceTests(unittest.TestCase):
    """Independence is the invariant the harness exists to protect."""

    def _submitted(self, executor_id: str) -> Graph:
        task_graph = Graph.from_dict(graph(node("solo")))
        task_graph.start("solo", executor_id=executor_id)
        task_graph.submit(
            "solo",
            [{"kind": "test", "summary": "implemented"}],
            actor_id=executor_id,
        )
        return task_graph

    def _pass(self, task_graph: Graph, reviewer_id: str) -> None:
        task_graph.verify(
            "solo",
            "pass",
            reviewer_id,
            checked_criteria=["relevant tests pass"],
            review_evidence=[
                {"kind": "independent_test", "summary": "reviewer reran the suite"}
            ],
        )

    def test_padded_or_recased_reviewer_is_not_independent(self) -> None:
        for reviewer_id in ("agent-1", "Agent-1", "  agent-1  ", "AGENT-1\t"):
            with self.subTest(reviewer_id=reviewer_id):
                task_graph = self._submitted("agent-1 ")

                with self.assertRaisesRegex(HarnessError, "independent"):
                    self._pass(task_graph, reviewer_id)

                self.assertEqual(
                    "awaiting_verification", task_graph.node("solo")["status"]
                )

    def test_a_genuinely_separate_reviewer_still_passes(self) -> None:
        task_graph = self._submitted("agent-1")

        self._pass(task_graph, "reviewer-2")

        self.assertEqual("verified", task_graph.node("solo")["status"])
        task_graph.completion_check()

    def test_padded_executor_may_still_submit_and_withdraw_its_own_node(self) -> None:
        task_graph = self._submitted("agent-1")
        task_graph.verify(
            "solo",
            "uncertain",
            "reviewer-2",
            reason="needs stronger evidence",
        )

        task_graph.withdraw_submission("solo", " AGENT-1 ")
        self.assertEqual("running", task_graph.node("solo")["status"])

        task_graph.submit(
            "solo", [{"kind": "test", "summary": "stronger"}], actor_id=" Agent-1 "
        )
        self.assertEqual("awaiting_verification", task_graph.node("solo")["status"])
        self.assertEqual(1, task_graph.node("solo")["attempts"])

    def test_stored_graph_with_padded_identities_is_rejected_on_load(self) -> None:
        task_graph = self._submitted("agent-1")
        self._pass(task_graph, "reviewer-2")
        document = task_graph.to_dict()
        document["nodes"][0]["executor_id"] = "Reviewer-2 "

        with self.assertRaisesRegex(HarnessError, "independent"):
            Graph.from_dict(document)


class UpstreamInvalidationBudgetTests(unittest.TestCase):
    """Upstream churn must not spend a descendant's own retry budget."""

    def _chain(self, down_max_attempts: int) -> Graph:
        return Graph.from_dict(
            graph(
                node("up", status="verified"),
                node("down", depends_on=["up"], max_attempts=down_max_attempts),
                node("probe", depends_on=["up"]),
            )
        )

    def _fail_probe_blaming_up(self, task_graph: Graph) -> list[str]:
        task_graph.start("probe", executor_id="probe-executor")
        task_graph.submit(
            "probe", [{"kind": "test", "summary": "built"}], actor_id="probe-executor"
        )
        return task_graph.verify(
            "probe",
            "fail",
            "probe-reviewer",
            reason="up produced a broken contract",
            faulty_node="up",
            failed_criteria=["relevant tests pass"],
            faulty_criteria=["relevant tests pass"],
            review_evidence=[
                {"kind": "test", "summary": "reviewer traced the fault upstream"}
            ],
        )

    def _verify(self, task_graph: Graph, node_id: str) -> None:
        task_graph.start(node_id, executor_id=f"{node_id}-executor")
        task_graph.submit(
            node_id,
            [{"kind": "test", "summary": "done"}],
            actor_id=f"{node_id}-executor",
        )
        task_graph.verify(
            node_id,
            "pass",
            f"{node_id}-reviewer",
            checked_criteria=["relevant tests pass"],
            review_evidence=[{"kind": "independent_test", "summary": "reran"}],
        )

    def test_invalidation_refunds_the_discarded_attempt(self) -> None:
        task_graph = self._chain(down_max_attempts=1)
        self._verify(task_graph, "down")
        self.assertEqual(1, task_graph.node("down")["attempts"])

        self._fail_probe_blaming_up(task_graph)

        down = task_graph.node("down")
        self.assertEqual("invalidated", down["status"])
        self.assertEqual(0, down["attempts"])

    def test_a_descendant_that_never_failed_review_stays_recoverable(self) -> None:
        task_graph = self._chain(down_max_attempts=1)
        self._verify(task_graph, "down")
        self._fail_probe_blaming_up(task_graph)

        self.assertEqual("ready", task_graph.retry("up"))
        self._verify(task_graph, "up")
        self.assertEqual("ready", task_graph.retry("down"))
        self._verify(task_graph, "down")
        self.assertEqual("ready", task_graph.retry("probe"))
        self._verify(task_graph, "probe")

        task_graph.completion_check()

    def test_a_node_invalidated_twice_is_refunded_only_once(self) -> None:
        task_graph = self._chain(down_max_attempts=2)
        self._verify(task_graph, "down")
        self._fail_probe_blaming_up(task_graph)
        self.assertEqual(0, task_graph.node("down")["attempts"])

        task_graph.retry("up")
        self._verify(task_graph, "up")
        task_graph.retry("probe")
        self._fail_probe_blaming_up(task_graph)

        self.assertEqual(0, task_graph.node("down")["attempts"])

    def test_a_nodes_own_review_failures_still_exhaust_the_budget(self) -> None:
        task_graph = Graph.from_dict(graph(node("solo", max_attempts=2)))
        for _ in range(2):
            task_graph.start("solo", executor_id="solo-executor")
            task_graph.submit(
                "solo", [{"kind": "test", "summary": "try"}], actor_id="solo-executor"
            )
            task_graph.verify(
                "solo",
                "fail",
                "solo-reviewer",
                reason="still broken",
                failed_criteria=["relevant tests pass"],
                review_evidence=[{"kind": "test", "summary": "reviewer saw a failure"}],
            )
            if task_graph.node("solo")["attempts"] < 2:
                task_graph.retry("solo")

        self.assertEqual(2, task_graph.node("solo")["attempts"])
        with self.assertRaisesRegex(HarnessError, "max_attempts"):
            task_graph.retry("solo")


class VerificationRecordIntegrityTests(unittest.TestCase):
    """A rejected call changes nothing, and recorded doubt is not erasable."""

    def _awaiting(self) -> Graph:
        task_graph = Graph.from_dict(graph(node("solo")))
        task_graph.start("solo", executor_id="e1")
        task_graph.submit(
            "solo", [{"kind": "test", "summary": "implemented"}], actor_id="e1"
        )
        return task_graph

    def test_a_pass_over_an_uncertain_preserves_the_uncertain_review(self) -> None:
        task_graph = self._awaiting()
        task_graph.verify("solo", "uncertain", "r1", reason="evidence too thin")

        task_graph.verify(
            "solo",
            "pass",
            "r2",
            checked_criteria=["relevant tests pass"],
            review_evidence=[{"kind": "independent_test", "summary": "reran"}],
        )

        history = task_graph.node("solo")["review_history"]
        self.assertEqual(1, len(history))
        self.assertEqual("uncertain", history[0]["verification"]["result"])
        self.assertEqual("evidence too thin", history[0]["verification"]["reason"])
        self.assertEqual("pass", task_graph.node("solo")["verification"]["result"])

    def test_a_fail_over_an_uncertain_preserves_the_uncertain_review(self) -> None:
        task_graph = self._awaiting()
        task_graph.verify("solo", "uncertain", "r1", reason="cannot tell yet")

        task_graph.verify(
            "solo",
            "fail",
            "r2",
            reason="the suite is red",
            failed_criteria=["relevant tests pass"],
            review_evidence=[{"kind": "test", "summary": "reviewer saw a failure"}],
        )

        history = task_graph.node("solo")["review_history"]
        self.assertEqual(
            ["cannot tell yet"], [r["verification"]["reason"] for r in history]
        )

    def test_a_rejected_verdict_archives_nothing(self) -> None:
        task_graph = self._awaiting()
        task_graph.verify("solo", "uncertain", "r1", reason="cannot tell yet")

        with self.assertRaises(HarnessError):
            task_graph.verify(
                "solo",
                "pass",
                "r2",
                checked_criteria=["relevant tests pass"],
            )

        solo = task_graph.node("solo")
        self.assertEqual([], solo.get("review_history", []))
        self.assertEqual("uncertain", solo["verification"]["result"])

    def test_a_rejected_submit_leaves_the_previous_evidence_intact(self) -> None:
        task_graph = Graph.from_dict(graph(node("solo")))
        task_graph.start("solo", executor_id="e1")
        task_graph.node("solo")["evidence"] = [
            {"kind": "test", "summary": "GOOD", "criterion": "relevant tests pass"}
        ]

        with self.assertRaisesRegex(HarnessError, "actor_id must match"):
            task_graph.submit(
                "solo", [{"kind": "test", "summary": "ATTACKER"}], actor_id="intruder"
            )

        solo = task_graph.node("solo")
        self.assertEqual("GOOD", solo["evidence"][0]["summary"])
        self.assertEqual("running", solo["status"])

    def test_a_rejected_submit_leaves_the_previous_review_context(self) -> None:
        task_graph = Graph.from_dict(graph(node("solo")))
        task_graph.start("solo", executor_id="e1")
        task_graph.node("solo")["review_context"] = {"relevant_files": ["kept.py"]}

        with self.assertRaises(HarnessError):
            task_graph.submit(
                "solo",
                [{"kind": "test", "summary": "new"}],
                actor_id="intruder",
                review_context={"relevant_files": ["clobbered.py"]},
            )

        self.assertEqual(
            ["kept.py"], task_graph.node("solo")["review_context"]["relevant_files"]
        )

    def test_a_reopened_ancestor_does_not_keep_its_passing_review(self) -> None:
        task_graph = Graph.from_dict(
            graph(node("root", status="verified"), node("leaf", depends_on=["root"]))
        )
        self.assertEqual("pass", task_graph.node("root")["verification"]["result"])
        task_graph.start("leaf", executor_id="leaf-executor")
        task_graph.submit(
            "leaf", [{"kind": "test", "summary": "built"}], actor_id="leaf-executor"
        )

        task_graph.verify(
            "leaf",
            "fail",
            "leaf-reviewer",
            reason="root produced a broken contract",
            faulty_node="root",
            failed_criteria=["relevant tests pass"],
            faulty_criteria=["relevant tests pass"],
            review_evidence=[{"kind": "test", "summary": "traced upstream"}],
        )

        root = task_graph.node("root")
        self.assertEqual("failed", root["status"])
        self.assertIsNone(root["verification"])
        self.assertNotIn("reviewer_id", root)
        self.assertNotIn("verified_at", root)
        self.assertEqual("ready", task_graph.retry("root"))

    def test_a_self_faulted_node_keeps_its_failing_review(self) -> None:
        task_graph = self._awaiting()

        task_graph.verify(
            "solo",
            "fail",
            "r1",
            reason="the suite is red",
            failed_criteria=["relevant tests pass"],
            review_evidence=[{"kind": "test", "summary": "reviewer saw a failure"}],
        )

        solo = task_graph.node("solo")
        self.assertEqual("failed", solo["status"])
        self.assertEqual("fail", solo["verification"]["result"])
