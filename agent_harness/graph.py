"""Vendor-neutral task graph validation and deterministic transitions."""

from __future__ import annotations

import copy
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, cast

from .errors import HarnessError

STATUSES = {
    "blocked",
    "ready",
    "running",
    "awaiting_verification",
    "verified",
    "failed",
    "invalidated",
}
RESULTS = {"pass", "fail", "uncertain"}
NODE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
REQUIRED_NODE_FIELDS = {
    "id",
    "description",
    "status",
    "depends_on",
    "assigned_role",
    "acceptance_criteria",
    "evidence",
    "attempts",
    "max_attempts",
    "failure_reason",
}
MAX_NODES = 1_000
MAX_DEPENDENCIES = 256
MAX_CRITERIA = 128
MAX_EVIDENCE = 512
MAX_TEXT = 32_768
MAX_ATTEMPTS = 20


def utc_now() -> str:
    """Return a stable ISO-8601 UTC timestamp."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Graph:
    """A validated in-memory task graph.

    The document is intentionally plain JSON data so both supported agent
    platforms can inspect and modify it through the deterministic CLI.
    """

    def __init__(self, document: Mapping[str, Any]):
        self.document: dict[str, Any] = copy.deepcopy(dict(document))
        self.validate()

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "Graph":
        return cls(document)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.document)

    @property
    def nodes(self) -> list[dict[str, Any]]:
        return cast("list[dict[str, Any]]", self.document["nodes"])

    def node(self, node_id: str) -> dict[str, Any]:
        for node in self.nodes:
            if node["id"] == node_id:
                return node
        raise HarnessError(f"unknown node: {node_id}")

    def validate(self) -> None:
        doc = self.document
        version = doc.get("version", doc.get("schema_version"))
        if version not in {1, "1.0"}:
            raise HarnessError("version must be 1")
        if not isinstance(doc.get("objective"), str) or not doc["objective"].strip():
            raise HarnessError("objective must be a non-empty string")
        if len(doc["objective"]) > MAX_TEXT:
            raise HarnessError("objective exceeds the text limit")
        if not isinstance(doc.get("nodes"), list) or not doc["nodes"]:
            raise HarnessError("nodes must be a non-empty list")
        if len(doc["nodes"]) > MAX_NODES:
            raise HarnessError(f"nodes exceeds the {MAX_NODES} node limit")

        ids: set[str] = set()
        for index, node in enumerate(doc["nodes"]):
            self._validate_node(node, index)
            node_id = node["id"]
            if node_id in ids:
                raise HarnessError(f"duplicate node id: {node_id}")
            ids.add(node_id)

        for node in doc["nodes"]:
            unknown = set(node["depends_on"]) - ids
            if unknown:
                raise HarnessError(
                    f"node {node['id']} has unknown dependencies: {sorted(unknown)}"
                )
            if node["id"] in node["depends_on"]:
                raise HarnessError(f"node {node['id']} cannot depend on itself")

        self._topological_ids()
        self._promote_blocked_nodes()
        self._validate_runtime_consistency()

    def _validate_node(self, node: Any, index: int) -> None:
        if not isinstance(node, dict):
            raise HarnessError(f"node at index {index} must be an object")
        missing = REQUIRED_NODE_FIELDS - set(node)
        if missing:
            raise HarnessError(
                f"node at index {index} missing fields: {sorted(missing)}"
            )
        node_id = node["id"]
        if not isinstance(node_id, str) or not NODE_ID.fullmatch(node_id):
            raise HarnessError(f"invalid node id at index {index}: {node_id!r}")
        for field in ("description", "assigned_role"):
            if not isinstance(node[field], str) or not node[field].strip():
                raise HarnessError(f"node {node_id} {field} must be non-empty")
            if len(node[field]) > MAX_TEXT:
                raise HarnessError(f"node {node_id} {field} exceeds the text limit")
        if node["status"] not in STATUSES:
            raise HarnessError(f"node {node_id} has invalid status: {node['status']!r}")
        if not isinstance(node["depends_on"], list) or not all(
            isinstance(value, str) for value in node["depends_on"]
        ):
            raise HarnessError(f"node {node_id} depends_on must be a string list")
        if len(set(node["depends_on"])) != len(node["depends_on"]):
            raise HarnessError(f"node {node_id} has duplicate dependencies")
        if len(node["depends_on"]) > MAX_DEPENDENCIES:
            raise HarnessError(f"node {node_id} has too many dependencies")
        criteria = node["acceptance_criteria"]
        if (
            not isinstance(criteria, list)
            or not criteria
            or not all(isinstance(value, str) and value.strip() for value in criteria)
        ):
            raise HarnessError(
                f"node {node_id} acceptance_criteria must be a non-empty string list"
            )
        if len(set(criteria)) != len(criteria):
            raise HarnessError(f"node {node_id} has duplicate acceptance criteria")
        if len(criteria) > MAX_CRITERIA or any(
            len(value) > MAX_TEXT for value in criteria
        ):
            raise HarnessError(f"node {node_id} acceptance criteria exceed limits")
        self._validate_evidence_list(node["evidence"], node_id)
        if not isinstance(node["attempts"], int) or isinstance(node["attempts"], bool):
            raise HarnessError(f"node {node_id} attempts must be an integer")
        if node["attempts"] < 0:
            raise HarnessError(f"node {node_id} attempts cannot be negative")
        if not isinstance(node["max_attempts"], int) or isinstance(
            node["max_attempts"], bool
        ):
            raise HarnessError(f"node {node_id} max_attempts must be an integer")
        if (
            node["max_attempts"] < 1
            or node["max_attempts"] > MAX_ATTEMPTS
            or node["attempts"] > node["max_attempts"]
        ):
            raise HarnessError(f"node {node_id} has invalid attempt limits")
        if node["failure_reason"] is not None and (
            not isinstance(node["failure_reason"], str)
            or not node["failure_reason"].strip()
        ):
            raise HarnessError(
                f"node {node_id} failure_reason must be null or non-empty"
            )
        if (
            "executor_id" in node
            and node["executor_id"] is not None
            and (
                not isinstance(node["executor_id"], str)
                or not node["executor_id"].strip()
            )
        ):
            raise HarnessError(f"node {node_id} executor_id must be null or non-empty")
        if not isinstance(node.get("failure_history", []), list):
            raise HarnessError(f"node {node_id} failure_history must be a list")
        if not isinstance(node.get("attempt_history", []), list):
            raise HarnessError(f"node {node_id} attempt_history must be a list")
        self._validate_review_context(node.get("review_context", {}), node_id)

    @staticmethod
    def _validate_evidence_list(evidence: Any, node_id: str) -> None:
        if not isinstance(evidence, list):
            raise HarnessError(f"node {node_id} evidence must be a list")
        if len(evidence) > MAX_EVIDENCE:
            raise HarnessError(f"node {node_id} has too many evidence entries")
        for item in evidence:
            if not isinstance(item, dict):
                raise HarnessError(f"node {node_id} evidence entries must be objects")
            for field in ("criterion", "kind", "summary"):
                if not isinstance(item.get(field), str) or not item[field].strip():
                    raise HarnessError(
                        f"node {node_id} evidence entry requires non-empty {field}"
                    )
                if len(item[field]) > MAX_TEXT:
                    raise HarnessError(
                        f"node {node_id} evidence entry {field} exceeds the text limit"
                    )

    def _validate_runtime_consistency(self) -> None:
        by_id = {node["id"]: node for node in self.nodes}
        for node in self.nodes:
            deps_verified = all(
                by_id[dependency]["status"] == "verified"
                for dependency in node["depends_on"]
            )
            if node["status"] == "ready" and not deps_verified:
                raise HarnessError(
                    f"ready node {node['id']} has unverified dependencies"
                )
            if node["status"] in {"running", "awaiting_verification", "verified"}:
                if not deps_verified:
                    raise HarnessError(
                        f"active node {node['id']} has unverified dependencies"
                    )
                if node["attempts"] < 1:
                    raise HarnessError(f"active node {node['id']} has no attempt")
            if node["status"] in {"running", "awaiting_verification"} and not node.get(
                "executor_id"
            ):
                raise HarnessError(f"active node {node['id']} has no executor_id")
            if node["status"] == "verified":
                verification = node.get("verification")
                executor_id = node.get("executor_id")
                if not isinstance(executor_id, str) or not executor_id.strip():
                    raise HarnessError(f"verified node {node['id']} has no executor_id")
                if not node["evidence"]:
                    raise HarnessError(f"verified node {node['id']} has no evidence")
                if (
                    not isinstance(verification, dict)
                    or str(verification.get("result", "")).lower() != "pass"
                ):
                    raise HarnessError(f"verified node {node['id']} has no PASS review")
                reviewer_id = verification.get("reviewer_id")
                if not reviewer_id or reviewer_id == executor_id:
                    raise HarnessError(
                        f"verified node {node['id']} lacks independent review"
                    )
                expected = set(node["acceptance_criteria"])
                submitted = {item["criterion"] for item in node["evidence"]}
                reviewed = {
                    item["criterion"] for item in verification.get("evidence", [])
                }
                checked = set(verification.get("checked_criteria", []))
                if submitted != expected or reviewed != expected or checked != expected:
                    raise HarnessError(
                        f"verified node {node['id']} does not cover every criterion"
                    )

    def _topological_ids(self) -> list[str]:
        indegree = {node["id"]: len(node["depends_on"]) for node in self.nodes}
        children: dict[str, list[str]] = {node_id: [] for node_id in indegree}
        for node in self.nodes:
            for dependency in node["depends_on"]:
                children[dependency].append(node["id"])
        queue = deque(
            sorted(node_id for node_id, degree in indegree.items() if degree == 0)
        )
        ordered: list[str] = []
        while queue:
            node_id = queue.popleft()
            ordered.append(node_id)
            for child in sorted(children[node_id]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if len(ordered) != len(self.nodes):
            raise HarnessError("task graph contains a dependency cycle")
        return ordered

    def ready_nodes(self) -> list[str]:
        return [node["id"] for node in self.nodes if node["status"] == "ready"]

    def ready_node_ids(self) -> list[str]:
        """Compatibility alias used by adapters and simple scripts."""

        self._promote_blocked_nodes()
        return self.ready_nodes()

    def descendants(self, node_id: str) -> list[str]:
        self.node(node_id)
        children: dict[str, list[str]] = {node["id"]: [] for node in self.nodes}
        for node in self.nodes:
            for dependency in node["depends_on"]:
                children[dependency].append(node["id"])
        seen: set[str] = set()
        queue = deque(sorted(children[node_id]))
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            queue.extend(sorted(children[current]))
        order = self._topological_ids()
        return [candidate for candidate in order if candidate in seen]

    def transition(self, node_id: str, status: str) -> None:
        """Reject arbitrary state changes; callers must use named operations."""

        self.node(node_id)
        if status not in STATUSES:
            raise HarnessError(f"invalid status: {status}")
        raise HarnessError(
            "direct transitions are forbidden; use start/submit/verify/retry"
        )

    def start(self, node_id: str, executor_id: str | None = None) -> None:
        node = self.node(node_id)
        executor_id = executor_id or node["assigned_role"]
        self._require_non_empty(executor_id, "executor_id")
        if node["status"] != "ready":
            raise HarnessError(f"cannot start {node_id} from {node['status']}")
        if node["attempts"] >= node["max_attempts"]:
            raise HarnessError(f"node {node_id} reached max_attempts")
        node["status"] = "running"
        node["attempts"] += 1
        node["executor_id"] = executor_id
        node["started_at"] = utc_now()
        node["verification"] = None
        node["failure_reason"] = None
        self.validate()

    def submit(
        self,
        node_id: str,
        evidence: Iterable[Mapping[str, Any]],
        actor_id: str | None = None,
        review_context: Mapping[str, Any] | None = None,
    ) -> None:
        node = self.node(node_id)
        if node["status"] != "running":
            raise HarnessError(f"cannot submit {node_id} from {node['status']}")
        items: list[dict[str, Any]] = [copy.deepcopy(dict(item)) for item in evidence]
        items = self._normalize_evidence(items, node)
        self._validate_evidence_list(items, node_id)
        criteria = set(node["acceptance_criteria"])
        unknown = {item["criterion"] for item in items} - criteria
        if unknown:
            raise HarnessError(
                f"evidence references unknown criteria: {sorted(unknown)}"
            )
        node["evidence"] = items
        if actor_id is not None:
            self._require_non_empty(actor_id, "actor_id")
            if actor_id != node.get("executor_id"):
                raise HarnessError(
                    "actor_id must match the executor that started the node"
                )
        context = dict(review_context or {})
        self._validate_review_context(context, node_id)
        node["review_context"] = context
        node["status"] = "awaiting_verification"
        node["submitted_at"] = utc_now()
        self.validate()

    def verify(
        self,
        node_id: str,
        result: str,
        reviewer_id: str,
        checked_criteria: Iterable[str] | None = None,
        review_evidence: Iterable[Mapping[str, Any]] | None = None,
        reason: str | None = None,
        faulty_node: str | None = None,
        recommendation: str | None = None,
        failed_criteria: Iterable[str] | None = None,
        faulty_criteria: Iterable[str] | None = None,
    ) -> list[str]:
        node = self.node(node_id)
        if node["status"] != "awaiting_verification":
            raise HarnessError(f"cannot verify {node_id} from {node['status']}")
        result = result.lower()
        if result not in RESULTS:
            raise HarnessError(f"invalid verification result: {result}")
        self._require_non_empty(reviewer_id, "reviewer_id")
        if reviewer_id == node.get("executor_id"):
            raise HarnessError("reviewer must be independent from executor")
        checked = list(checked_criteria or node["acceptance_criteria"])
        if len(set(checked)) != len(checked):
            raise HarnessError("checked criteria must be unique")
        expected = set(node["acceptance_criteria"])
        if not set(checked).issubset(expected):
            raise HarnessError("review references unknown acceptance criteria")
        failed = list(failed_criteria or [])
        if len(set(failed)) != len(failed) or not set(failed).issubset(expected):
            raise HarnessError("failed criteria must be unique acceptance criteria")
        faulty_failed = list(faulty_criteria or [])
        review_items = self._normalize_evidence(
            copy.deepcopy(list(review_evidence or node["evidence"])), node
        )
        self._validate_evidence_list(review_items, node_id)
        review_covered = {item["criterion"] for item in review_items}
        if not review_covered.issubset(expected):
            raise HarnessError("review evidence references unknown criteria")
        if result != "fail" and faulty_node is not None:
            raise HarnessError("faulty_node is only valid for a FAIL review")
        if result != "fail" and faulty_failed:
            raise HarnessError("faulty_criteria are only valid for a FAIL review")

        review = {
            "result": result,
            "reviewer_id": reviewer_id,
            "checked_criteria": checked,
            "evidence": review_items,
            "reason": reason,
            "faulty_node": faulty_node,
            "recommendation": recommendation,
            "failed_criteria": failed,
            "affected_downstream_nodes": [],
            "reviewed_at": utc_now(),
        }
        if result == "pass":
            covered = {item["criterion"] for item in node["evidence"]}
            if (
                set(checked) != expected
                or covered != expected
                or review_covered != expected
            ):
                raise HarnessError(
                    "PASS requires submission and review evidence for every criterion"
                )
            node["verification"] = review
            node["reviewer_id"] = reviewer_id
            node["status"] = "verified"
            node["failure_reason"] = None
            node["verified_at"] = utc_now()
            self._promote_blocked_nodes()
            self.validate()
            return []

        self._require_non_empty(reason, "reason")
        review["reason"] = reason
        if result == "uncertain":
            node["verification"] = review
            self.validate()
            return []

        faulty = faulty_node or node_id
        faulty_task = self.node(faulty)
        if faulty != node_id and (
            node_id not in self.descendants(faulty)
            or faulty_task["status"] != "verified"
        ):
            raise HarnessError(
                "faulty_node must be the reviewed node or one of its verified ancestors"
            )
        if reviewer_id == faulty_task.get("executor_id"):
            raise HarnessError("reviewer must be independent from faulty-node executor")
        faulty_expected = set(faulty_task["acceptance_criteria"])
        if faulty == node_id:
            faulty_failed = faulty_failed or failed
        if (
            not faulty_failed
            or len(set(faulty_failed)) != len(faulty_failed)
            or not set(faulty_failed).issubset(faulty_expected)
        ):
            raise HarnessError(
                "FAIL requires unique faulty-node criteria from that node's acceptance criteria"
            )
        if not failed:
            raise HarnessError("FAIL requires at least one failed criterion")
        if not review_items or not set(failed).issubset(review_covered):
            raise HarnessError(
                "FAIL requires review evidence for every failed criterion"
            )
        review["faulty_node"] = faulty
        review["failed_criteria"] = failed
        review["faulty_node_criteria"] = faulty_failed
        recommendation = (
            recommendation
            or f"Retry {faulty} with evidence addressing the failed criteria."
        )
        review["recommendation"] = recommendation
        node["verification"] = review
        faulty_task["status"] = "failed"
        faulty_task["failure_reason"] = reason
        failure = {
            "event_id": f"{faulty}:{faulty_task['attempts']}",
            "node": faulty,
            "detected_at_node": node_id,
            "attempt": faulty_task["attempts"],
            "failure_type": "verification_failure",
            "summary": reason,
            "reason": reason,
            "failed_criteria": failed,
            "failed_criteria_node": node_id,
            "faulty_node_criteria": faulty_failed,
            "reviewer_id": reviewer_id,
            "recommendation": recommendation,
            "prevention": [recommendation],
            "recorded_at": utc_now(),
        }
        invalidated = self._invalidate_descendants(faulty)
        review["affected_downstream_nodes"] = invalidated
        failure["affected_downstream_nodes"] = invalidated
        faulty_task.setdefault("failure_history", []).append(copy.deepcopy(failure))
        self.validate()
        return invalidated

    def retry(self, node_id: str) -> str:
        node = self.node(node_id)
        if node["status"] not in {"failed", "invalidated"}:
            raise HarnessError(f"cannot retry {node_id} from {node['status']}")
        if node["attempts"] >= node["max_attempts"]:
            raise HarnessError(f"node {node_id} reached max_attempts")
        node.setdefault("attempt_history", []).append(
            {
                "attempt": node["attempts"],
                "evidence": copy.deepcopy(node["evidence"]),
                "verification": copy.deepcopy(node.get("verification")),
                "failure_reason": node["failure_reason"],
                "archived_at": utc_now(),
            }
        )
        node["evidence"] = []
        node["verification"] = None
        node["executor_id"] = None
        node.pop("reviewer_id", None)
        node["failure_reason"] = None
        node.pop("started_at", None)
        node.pop("submitted_at", None)
        node.pop("verified_at", None)
        deps_verified = all(
            self.node(dependency)["status"] == "verified"
            for dependency in node["depends_on"]
        )
        node["status"] = "ready" if deps_verified else "blocked"
        self.validate()
        return cast(str, node["status"])

    def review_packet(self, node_id: str) -> dict[str, Any]:
        node = self.node(node_id)
        if node["status"] != "awaiting_verification":
            raise HarnessError(
                "review packet is only available while awaiting verification"
            )
        return {
            "original_objective": self.document["objective"],
            "node_objective": node["description"],
            "node_id": node_id,
            "acceptance_criteria": copy.deepcopy(node["acceptance_criteria"]),
            "evidence": copy.deepcopy(node["evidence"]),
            "relevant_files": copy.deepcopy(
                node.get("review_context", {}).get("relevant_files", [])
            ),
            "diff_artifact": node.get("review_context", {}).get("diff_artifact"),
            "test_output_artifact": node.get("review_context", {}).get(
                "test_output_artifact"
            ),
            "attempt": node["attempts"],
        }

    def completion_check(self) -> None:
        incomplete = [node["id"] for node in self.nodes if node["status"] != "verified"]
        if incomplete:
            raise HarnessError(f"graph is not complete; unverified nodes: {incomplete}")

    def status_summary(self) -> dict[str, Any]:
        counts = {status: 0 for status in sorted(STATUSES)}
        for node in self.nodes:
            counts[node["status"]] += 1
        return {
            "objective": self.document["objective"],
            "counts": counts,
            "ready": self.ready_nodes(),
            "complete": counts["verified"] == len(self.nodes),
            "nodes": [
                {
                    "id": node["id"],
                    "status": node["status"],
                    "attempts": node["attempts"],
                    "max_attempts": node["max_attempts"],
                    "failure_reason": node["failure_reason"],
                }
                for node in self.nodes
            ],
        }

    def failure_records(
        self,
        node: str | None = None,
        failure_type: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return a bounded projection of graph-resident failure memory."""

        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise HarnessError("failure query limit must be between 1 and 1000")
        records: list[dict[str, Any]] = []
        for task in self.nodes:
            for record in task.get("failure_history", []):
                if node is not None and record.get("node") != node:
                    continue
                if (
                    failure_type is not None
                    and record.get("failure_type") != failure_type
                ):
                    continue
                records.append(copy.deepcopy(record))
        records.sort(key=lambda record: str(record.get("recorded_at", "")))
        return records[-limit:]

    def _invalidate_descendants(self, node_id: str) -> list[str]:
        invalidated: list[str] = []
        for descendant_id in self.descendants(node_id):
            node = self.node(descendant_id)
            previous = node["status"]
            if previous == "failed":
                continue
            node.setdefault("failure_history", []).append(
                {
                    "node": descendant_id,
                    "failure_type": "upstream_invalidation",
                    "summary": f"Invalidated by failure of {node_id}",
                    "upstream_node": node_id,
                    "previous_status": previous,
                    "recorded_at": utc_now(),
                }
            )
            node["status"] = "invalidated"
            node["failure_reason"] = f"Invalidated by failure of {node_id}"
            invalidated.append(descendant_id)
        return invalidated

    def _promote_blocked_nodes(self) -> None:
        for node_id in self._topological_ids():
            node = self.node(node_id)
            if node["status"] != "blocked":
                continue
            if all(
                self.node(dependency)["status"] == "verified"
                for dependency in node["depends_on"]
            ):
                node["status"] = "ready"

    @staticmethod
    def _require_non_empty(value: Any, label: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise HarnessError(f"{label} must be a non-empty string")

    @staticmethod
    def _normalize_evidence(
        evidence: Iterable[Mapping[str, Any]], node: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        criteria = node["acceptance_criteria"]
        for raw in evidence:
            item = dict(raw)
            if "summary" not in item and isinstance(item.get("value"), str):
                item["summary"] = item["value"]
            if "criterion" not in item and len(criteria) == 1:
                item["criterion"] = criteria[0]
            normalized.append(item)
        return normalized

    @staticmethod
    def _validate_review_context(context: Any, node_id: str) -> None:
        if not isinstance(context, Mapping):
            raise HarnessError(f"node {node_id} review context must be an object")
        allowed = {"relevant_files", "diff_artifact", "test_output_artifact"}
        if set(context) - allowed:
            raise HarnessError(f"node {node_id} review context has unknown fields")
        relevant = context.get("relevant_files", [])
        if not isinstance(relevant, list) or not all(
            isinstance(path, str) and path.strip() for path in relevant
        ):
            raise HarnessError(f"node {node_id} relevant_files must be a string list")
        if len(relevant) > MAX_EVIDENCE:
            raise HarnessError(f"node {node_id} has too many relevant files")
        paths = list(relevant)
        for field in ("diff_artifact", "test_output_artifact"):
            value = context.get(field)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise HarnessError(f"node {node_id} {field} must be null or non-empty")
            if isinstance(value, str):
                paths.append(value)
        for value in paths:
            if len(value) > MAX_TEXT:
                raise HarnessError(f"node {node_id} review context path is too long")
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise HarnessError(
                    f"node {node_id} review context paths must be repository-relative"
                )
