"""Small, explicit graph fixtures shared by the public-contract tests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def node(
    node_id: str,
    *,
    depends_on: list[str] | None = None,
    status: str = "blocked",
    max_attempts: int = 2,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": node_id,
        "description": f"Complete {node_id}",
        "status": status,
        "depends_on": depends_on or [],
        "assigned_role": "implementer",
        "acceptance_criteria": ["relevant tests pass"],
        "evidence": [],
        "attempts": 0,
        "max_attempts": max_attempts,
        "failure_reason": None,
    }
    if status == "verified":
        criterion = result["acceptance_criteria"][0]
        evidence = {"criterion": criterion, "kind": "test", "summary": "pass"}
        result.update(
            {
                "evidence": [evidence],
                "attempts": 1,
                "executor_id": f"{node_id}-executor",
                "reviewer_id": f"{node_id}-reviewer",
                "verification": {
                    "result": "pass",
                    "reviewer_id": f"{node_id}-reviewer",
                    "checked_criteria": [criterion],
                    "evidence": [evidence],
                },
            }
        )
    return result


def graph(*nodes: dict[str, Any]) -> dict[str, Any]:
    """Return the minimal portable on-disk graph document."""
    return {"version": 1, "objective": "exercise the graph", "nodes": list(nodes)}


def clone(value: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(value)
