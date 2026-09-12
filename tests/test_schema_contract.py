"""Contracts for the public JSON Schemas without a runtime validator dependency."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[1]
TASK_GRAPH_SCHEMA = REPOSITORY / "core" / "task-graph.schema.json"
REVIEW_RESULT_SCHEMA = REPOSITORY / "core" / "review-result.schema.json"


def _conditional_properties(schema: dict[str, Any], result: str) -> dict[str, Any]:
    """Return the ``then.properties`` contract for a review result."""

    for branch in schema["allOf"]:
        condition = branch.get("if", {}).get("properties", {})
        if condition.get("result", {}).get("const") == result:
            properties = branch["then"]["properties"]
            if not isinstance(properties, dict):
                raise AssertionError(
                    f"conditional schema properties for {result} must be an object"
                )
            return properties
    raise AssertionError(f"missing conditional schema branch for {result}")


class PublicSchemaContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.task_graph = json.loads(TASK_GRAPH_SCHEMA.read_text(encoding="utf-8"))
        self.review_result = json.loads(
            REVIEW_RESULT_SCHEMA.read_text(encoding="utf-8")
        )

    def test_task_graph_schema_names_every_status_the_harness_writes(self) -> None:
        from agent_harness.graph import STATUSES

        node = self.task_graph["$defs"]["node"]
        self.assertEqual(STATUSES, set(node["properties"]["status"]["enum"]))

    def test_task_graph_schema_preserves_withdrawal_review_history(self) -> None:
        node = self.task_graph["$defs"]["node"]
        review_history = node["properties"]["review_history"]

        self.assertEqual("array", review_history["type"])
        self.assertEqual(512, review_history["maxItems"])
        history_item = review_history["items"]
        self.assertEqual(
            {
                "verification",
                "submission_evidence",
                "review_context",
                "submitted_at",
                "withdrawn_at",
            },
            set(history_item["required"]),
        )
        self.assertEqual(
            "array", history_item["properties"]["submission_evidence"]["type"]
        )
        self.assertEqual("string", history_item["properties"]["withdrawn_at"]["type"])
        self.assertEqual(1, history_item["properties"]["withdrawn_at"]["minLength"])
        self.assertEqual(
            "#/$defs/review_context",
            history_item["properties"]["review_context"]["$ref"],
        )

    def test_review_result_schema_requires_reason_for_uncertain_reviews(self) -> None:
        uncertain = _conditional_properties(self.review_result, "uncertain")

        self.assertIn("reason", self.review_result["required"])
        self.assertEqual("string", uncertain["reason"]["type"])
        self.assertEqual(1, uncertain["reason"]["minLength"])

    def test_review_result_schema_rejects_empty_pass_review_evidence(self) -> None:
        passed = _conditional_properties(self.review_result, "pass")

        self.assertTrue(
            {"checked_criteria", "evidence"}.issubset(self.review_result["required"])
        )
        self.assertEqual(1, passed["checked_criteria"]["minItems"])
        self.assertEqual(1, passed["evidence"]["minItems"])

    def test_task_graph_verification_uses_structured_review_contract(self) -> None:
        node = self.task_graph["$defs"]["node"]
        verification = self.task_graph["$defs"]["verification"]

        self.assertEqual(
            "#/$defs/verification",
            node["properties"]["verification"]["anyOf"][0]["$ref"],
        )
        self.assertEqual(
            "#/$defs/verification",
            node["properties"]["review_history"]["items"]["properties"]["verification"][
                "allOf"
            ][0]["$ref"],
        )
        self.assertEqual(
            "uncertain",
            node["properties"]["review_history"]["items"]["properties"]["verification"][
                "allOf"
            ][1]["properties"]["result"]["const"],
        )
        self.assertEqual(
            "#/$defs/evidence", verification["properties"]["evidence"]["items"]["$ref"]
        )
        self.assertEqual(
            {"criterion", "kind", "summary"},
            set(self.review_result["$defs"]["evidence"]["required"]),
        )
