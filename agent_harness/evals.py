"""Portable evaluation case loading and deterministic protocol comparison."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .errors import HarnessError
from .graph import Graph

MAX_EVAL_CASE_BYTES = 2 * 1024 * 1024


def load_cases(directory: str | Path) -> list[dict[str, Any]]:
    root = Path(directory)
    cases: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            if path.stat().st_size > MAX_EVAL_CASE_BYTES:
                raise HarnessError(f"eval case {path} exceeds 2 MiB")
            case = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HarnessError(f"invalid eval case {path}: {exc}") from exc
        if not isinstance(case, dict):
            raise HarnessError(f"eval case {path} must be an object")
        for field in ("id", "objective", "task_graph", "failure_node"):
            if not case.get(field):
                raise HarnessError(f"eval case {path} missing {field}")
        graph = Graph.from_dict(case["task_graph"])
        try:
            graph.node(case["failure_node"])
        except HarnessError as exc:
            raise HarnessError(
                f"eval case {path} has invalid failure_node: {case['failure_node']}"
            ) from exc
        cases.append(case)
    if not cases:
        raise HarnessError(f"no eval cases found in {root}")
    return cases


def compare_case(case: dict[str, Any]) -> dict[str, Any]:
    """Compare full-replay baseline with graph descendant-only retry.

    This is an offline protocol benchmark, not a claim about model quality.
    """

    started = time.perf_counter()
    graph = Graph.from_dict(case["task_graph"])
    failure_node = case["failure_node"]
    descendants = graph.descendants(failure_node)
    ancestors = _ancestors(graph, failure_node)
    _complete_ancestors(graph, failure_node, ancestors)
    target = graph.node(failure_node)
    graph.start(failure_node, executor_id=f"eval-executor:{failure_node}")
    evidence = _criterion_evidence(target, "injected pre-review evidence")
    graph.submit(failure_node, evidence, actor_id=f"eval-executor:{failure_node}")
    unrelated = {
        node["id"]: node["status"]
        for node in graph.nodes
        if node["id"] not in set(descendants) | ancestors | {failure_node}
    }
    invalidated = graph.verify(
        failure_node,
        "fail",
        reviewer_id=f"eval-reviewer:{failure_node}",
        reason="injected evaluation failure",
        failed_criteria=[target["acceptance_criteria"][0]],
        review_evidence=_criterion_evidence(
            target, "injected independent failure evidence"
        ),
    )
    unrelated_preserved = all(
        graph.node(node_id)["status"] == status for node_id, status in unrelated.items()
    )
    retry_status = graph.retry(failure_node)
    total = len(graph.nodes)
    graph_reruns = 1 + len(descendants)
    baseline_reruns = total
    return {
        "case_id": case["id"],
        "mode": "offline_protocol_simulation",
        "task_success": None,
        "first_pass_success": None,
        "reviewer_detection_rate": None,
        "retry_count": 1,
        "baseline_reruns": baseline_reruns,
        "graph_reruns": graph_reruns,
        "unnecessary_reruns_avoided": max(0, baseline_reruns - graph_reruns),
        "regressions": None,
        "wall_clock_seconds": round(time.perf_counter() - started, 6),
        "token_usage": None,
        "protocol_checks": {
            "descendant_invalidation": invalidated == descendants,
            "unrelated_branch_preserved": unrelated_preserved,
            "failed_node_retryable": retry_status == "ready",
        },
    }


def _ancestors(graph: Graph, node_id: str) -> set[str]:
    found: set[str] = set()
    pending = list(graph.node(node_id)["depends_on"])
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        pending.extend(graph.node(current)["depends_on"])
    return found


def _criterion_evidence(node: dict[str, Any], summary: str) -> list[dict[str, str]]:
    return [
        {"criterion": criterion, "kind": "simulation", "summary": summary}
        for criterion in node["acceptance_criteria"]
    ]


def _complete_ancestors(graph: Graph, failure_node: str, ancestors: set[str]) -> None:
    remaining = set(ancestors)
    while remaining:
        progress = False
        for node_id in list(remaining):
            if node_id not in graph.ready_node_ids():
                continue
            node = graph.node(node_id)
            executor = f"eval-executor:{node_id}"
            graph.start(node_id, executor_id=executor)
            evidence = _criterion_evidence(node, "simulated prerequisite evidence")
            graph.submit(node_id, evidence, actor_id=executor)
            graph.verify(
                node_id,
                "pass",
                reviewer_id=f"eval-reviewer:{node_id}",
                checked_criteria=node["acceptance_criteria"],
                review_evidence=_criterion_evidence(
                    node, "simulated independent review evidence"
                ),
            )
            remaining.remove(node_id)
            progress = True
        if not progress:
            raise HarnessError(
                f"eval case cannot prepare failure node {failure_node}; blocked ancestors: {sorted(remaining)}"
            )


def run_suite(directory: str | Path) -> dict[str, Any]:
    results = [compare_case(case) for case in load_cases(directory)]
    return {
        "mode": "offline_protocol_simulation",
        "case_count": len(results),
        "results": results,
        "totals": {
            "baseline_reruns": sum(item["baseline_reruns"] for item in results),
            "graph_reruns": sum(item["graph_reruns"] for item in results),
            "unnecessary_reruns_avoided": sum(
                item["unnecessary_reruns_avoided"] for item in results
            ),
        },
    }
