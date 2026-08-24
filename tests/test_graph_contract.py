from __future__ import annotations

import unittest

from agent_harness.errors import HarnessError
from agent_harness.graph import Graph

from tests.helpers import graph, node


class GraphSchemaAndSchedulingTests(unittest.TestCase):
    def test_validate_accepts_minimal_valid_graph(self) -> None:
        task_graph = Graph.from_dict(graph(node("plan")))

        task_graph.validate()

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

        task_graph.verify("implement", "PASS", reviewer_id="reviewer-1")

        verified = task_graph.node("implement")
        self.assertEqual("verified", verified["status"])
        self.assertEqual("reviewer-1", verified["reviewer_id"])

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
