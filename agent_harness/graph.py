"""Vendor-neutral task graph validation and deterministic transitions."""

from __future__ import annotations

import copy
import json
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, cast

from .errors import HarnessError

# The statuses that let dependent work proceed. A superseded dependency is
# settled, not owed: its approach was abandoned rather than left undone, so a
# node waiting on it would wait for ever. Named once because the same test is
# made in six places, and the last of them to be written by hand — the list of
# dependencies `_next_action` tells a blocked node to wait for — read
# `!= "verified"` and so named one nothing would ever move.
SETTLED = frozenset({"verified", "superseded"})

STATUSES = {
    "blocked",
    "ready",
    "running",
    "awaiting_verification",
    "verified",
    "failed",
    "invalidated",
    # A node whose approach was abandoned. Not done, not owed: it holds its
    # failure history and stops blocking work that no longer needs it.
    "superseded",
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
# A quarter of the 4 MiB document ceiling enforced by the storage layer. The
# archive is the only part of the document that grows without an attempt being
# spent, so it is the only part that can push a graph past that ceiling on its
# own; the remaining three quarters stay available to the rest of the document.
MAX_REVIEW_HISTORY_BYTES = 1_048_576


def utc_now() -> str:
    """Return a stable ISO-8601 UTC timestamp."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def utc_from_epoch(seconds: int) -> str:
    """Return what :func:`utc_now` would have returned at *seconds*.

    Beside `utc_now` deliberately. A timestamp recovered from another tool's
    store is compared and sorted against timestamps stamped there, so the two
    have to agree on offset spelling and precision exactly; written apart,
    one of them would eventually gain a `Z` or a fractional second and the
    comparison would go quietly wrong rather than fail.
    """

    return (
        datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=0).isoformat()
    )


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
        version = doc.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version != 1:
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
        verification = node.get("verification")
        if verification is not None:
            self._validate_verification(
                verification, node_id, set(node["acceptance_criteria"])
            )
            if self._same_actor(verification["reviewer_id"], node.get("executor_id")):
                raise HarnessError(
                    f"node {node_id} verification reviewer must be independent"
                )
        if not isinstance(node.get("failure_history", []), list):
            raise HarnessError(f"node {node_id} failure_history must be a list")
        if not isinstance(node.get("attempt_history", []), list):
            raise HarnessError(f"node {node_id} attempt_history must be a list")
        self._validate_review_history(
            node.get("review_history", []),
            node_id,
            set(node["acceptance_criteria"]),
        )
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

    def _validate_verification(
        self, verification: Any, node_id: str, expected: set[str]
    ) -> None:
        required = {
            "result",
            "reviewer_id",
            "checked_criteria",
            "evidence",
            "reason",
            "faulty_node",
            "recommendation",
            "failed_criteria",
            "affected_downstream_nodes",
            "reviewed_at",
        }
        allowed = required | {"faulty_node_criteria"}
        if not isinstance(verification, dict):
            raise HarnessError(f"node {node_id} verification must be an object")
        if not required.issubset(verification) or set(verification) - allowed:
            raise HarnessError(
                f"node {node_id} verification fields do not match the v1 contract"
            )
        result = verification["result"]
        if not isinstance(result, str) or result not in RESULTS:
            raise HarnessError(f"node {node_id} verification result is invalid")
        for field in ("reviewer_id", "reviewed_at"):
            value = verification[field]
            if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
                raise HarnessError(
                    f"node {node_id} verification {field} must be non-empty"
                )
        for field in ("reason", "faulty_node", "recommendation"):
            value = verification[field]
            if value is not None and (
                not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT
            ):
                raise HarnessError(
                    f"node {node_id} verification {field} must be null or non-empty"
                )
        checked = verification["checked_criteria"]
        failed = verification["failed_criteria"]
        affected = verification["affected_downstream_nodes"]
        for field, values, limit in (
            ("checked_criteria", checked, MAX_CRITERIA),
            ("failed_criteria", failed, MAX_CRITERIA),
            ("affected_downstream_nodes", affected, MAX_NODES),
        ):
            if (
                not isinstance(values, list)
                or len(values) > limit
                or not all(
                    isinstance(value, str) and value.strip() and len(value) <= MAX_TEXT
                    for value in values
                )
            ):
                raise HarnessError(
                    f"node {node_id} verification {field} must be a unique string list"
                )
            if len(set(values)) != len(values):
                raise HarnessError(
                    f"node {node_id} verification {field} must be a unique string list"
                )
        if not set(checked).issubset(expected) or not set(failed).issubset(expected):
            raise HarnessError(
                f"node {node_id} verification references unknown criteria"
            )
        self._validate_evidence_list(verification["evidence"], node_id)
        reviewed = {item["criterion"] for item in verification["evidence"]}
        if not reviewed.issubset(expected):
            raise HarnessError(
                f"node {node_id} verification evidence references unknown criteria"
            )
        faulty_criteria = verification.get("faulty_node_criteria")
        if faulty_criteria is not None:
            if (
                not isinstance(faulty_criteria, list)
                or len(faulty_criteria) > MAX_CRITERIA
                or not all(
                    isinstance(value, str) and value.strip() and len(value) <= MAX_TEXT
                    for value in faulty_criteria
                )
            ):
                raise HarnessError(
                    f"node {node_id} verification faulty_node_criteria is invalid"
                )
            if len(set(faulty_criteria)) != len(faulty_criteria):
                raise HarnessError(
                    f"node {node_id} verification faulty_node_criteria is invalid"
                )
        if result == "pass" and (not checked or not verification["evidence"]):
            raise HarnessError(
                f"node {node_id} PASS verification requires criteria and evidence"
            )
        if result == "uncertain" and not verification["reason"]:
            raise HarnessError(
                f"node {node_id} UNCERTAIN verification requires a reason"
            )
        if result == "fail" and (
            not verification["reason"]
            or not verification["faulty_node"]
            or not verification["recommendation"]
            or not failed
            or not faulty_criteria
            or not verification["evidence"]
            or not set(failed).issubset(reviewed)
        ):
            raise HarnessError(
                f"node {node_id} FAIL verification is missing required evidence"
            )

    def _validate_review_history(
        self, history: Any, node_id: str, expected: set[str]
    ) -> None:
        if not isinstance(history, list):
            raise HarnessError(f"node {node_id} review_history must be a list")
        if len(history) > MAX_EVIDENCE:
            raise HarnessError(f"node {node_id} has too many review history entries")
        required = {
            "verification",
            "submission_evidence",
            "review_context",
            "submitted_at",
            "withdrawn_at",
        }
        for record in history:
            if not isinstance(record, dict) or set(record) != required:
                raise HarnessError(
                    f"node {node_id} review_history entries must contain only "
                    f"{sorted(required)}"
                )
            self._validate_verification(record["verification"], node_id, expected)
            if record["verification"]["result"] != "uncertain":
                raise HarnessError(
                    f"node {node_id} review_history may contain only UNCERTAIN reviews"
                )
            if not isinstance(record["submission_evidence"], list):
                raise HarnessError(
                    f"node {node_id} review_history submission_evidence must be a list"
                )
            self._validate_evidence_list(record["submission_evidence"], node_id)
            self._validate_review_context(record["review_context"], node_id)
            for field in ("submitted_at", "withdrawn_at"):
                timestamp = record[field]
                if (
                    not isinstance(timestamp, str)
                    or not timestamp.strip()
                    or len(timestamp) > MAX_TEXT
                ):
                    raise HarnessError(
                        f"node {node_id} review_history {field} must be non-empty"
                    )

    def _validate_runtime_consistency(self) -> None:
        by_id = {node["id"]: node for node in self.nodes}
        for node in self.nodes:
            # Read from the local map rather than through `self.node`, which
            # is a linear scan: this runs for every node on every validate.
            deps_verified = all(
                by_id[dependency]["status"] in SETTLED
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
            verification = node.get("verification")
            if node["status"] == "running" and verification is not None:
                raise HarnessError(f"running node {node['id']} cannot have a review")
            if node["status"] == "awaiting_verification" and verification is not None:
                if verification["result"] != "uncertain":
                    raise HarnessError(
                        f"awaiting node {node['id']} may retain only an UNCERTAIN review"
                    )
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
                if not reviewer_id or self._same_actor(reviewer_id, executor_id):
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

    def ancestors(self, node_id: str) -> list[str]:
        """Return every node this one transitively depends on, in graph order."""

        seen: set[str] = set()
        queue = deque(sorted(self.node(node_id)["depends_on"]))
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            queue.extend(sorted(self.node(current)["depends_on"]))
        order = self._topological_ids()
        return [candidate for candidate in order if candidate in seen]

    def add_node(
        self,
        node_id: str,
        description: str,
        acceptance_criteria: Iterable[str],
        depends_on: Iterable[str] | None = None,
        assigned_role: str = "implementer",
        max_attempts: int = 2,
    ) -> str:
        """Append one planned node and return the status validation gave it.

        Planning a dependency graph is the harness's own documented workflow,
        so it must not require hand-editing the file the protocol reserves for
        this CLI. The candidate document is validated before it is adopted, so
        a rejected addition leaves the graph exactly as it was.
        """

        candidate = self.to_dict()
        candidate["nodes"].append(
            {
                "id": node_id,
                "description": description,
                "status": "blocked",
                "depends_on": list(depends_on or []),
                "assigned_role": assigned_role,
                "acceptance_criteria": list(acceptance_criteria),
                "evidence": [],
                "attempts": 0,
                "max_attempts": max_attempts,
                "failure_reason": None,
            }
        )
        validated = Graph.from_dict(candidate)
        self.document = validated.document
        return str(self.node(node_id)["status"])

    def transition(self, node_id: str, status: str) -> None:
        """Reject arbitrary state changes; callers must use named operations."""

        self.node(node_id)
        if status not in STATUSES:
            raise HarnessError(f"invalid status: {status}")
        raise HarnessError(
            "direct transitions are forbidden; use start/submit/verify/withdraw/retry"
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
            if (
                node["status"] == "awaiting_verification"
                and str((node.get("verification") or {}).get("result", "")).lower()
                == "uncertain"
            ):
                raise HarnessError(
                    f"cannot submit {node_id} from awaiting_verification; "
                    "withdraw the uncertain submission first"
                )
            raise HarnessError(f"cannot submit {node_id} from {node['status']}")
        # Validate everything before touching the node. A rejected submit must
        # leave the previous evidence intact, so the identity and context checks
        # cannot sit between two mutations.
        if actor_id is not None:
            self._require_non_empty(actor_id, "actor_id")
            if not self._same_actor(actor_id, node.get("executor_id")):
                raise HarnessError(
                    "actor_id must match the executor that started the node"
                )
        items: list[dict[str, Any]] = [copy.deepcopy(dict(item)) for item in evidence]
        items = self._normalize_evidence(items, node)
        self._validate_evidence_list(items, node_id)
        criteria = set(node["acceptance_criteria"])
        unknown = {item["criterion"] for item in items} - criteria
        if unknown:
            raise HarnessError(
                f"evidence references unknown criteria: {sorted(unknown)}"
            )
        context = dict(review_context or {})
        self._validate_review_context(context, node_id)
        node["evidence"] = items
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
        if self._same_actor(reviewer_id, node.get("executor_id")):
            raise HarnessError("reviewer must be independent from executor")
        if result == "pass" and checked_criteria is None:
            raise HarnessError(
                "PASS requires explicit checked criteria from the reviewer"
            )
        if result in {"pass", "fail"} and review_evidence is None:
            raise HarnessError(
                f"{result.upper()} requires explicit review evidence from the reviewer"
            )
        failed = list(failed_criteria or [])
        if checked_criteria is None:
            checked = list(failed) if result == "fail" else []
        else:
            checked = list(checked_criteria)
        if len(set(checked)) != len(checked):
            raise HarnessError("checked criteria must be unique")
        expected = set(node["acceptance_criteria"])
        if not set(checked).issubset(expected):
            raise HarnessError("review references unknown acceptance criteria")
        if len(set(failed)) != len(failed) or not set(failed).issubset(expected):
            raise HarnessError("failed criteria must be unique acceptance criteria")
        faulty_failed = list(faulty_criteria or [])
        review_source: Iterable[Mapping[str, Any]] = (
            () if review_evidence is None else review_evidence
        )
        review_items = self._normalize_evidence(
            copy.deepcopy(list(review_source)), node
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
            self._record_verification(node, review)
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
            self._record_verification(node, review)
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
        if self._same_actor(reviewer_id, faulty_task.get("executor_id")):
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
        self._record_verification(node, review)
        faulty_task["status"] = "failed"
        faulty_task["failure_reason"] = reason
        if faulty != node_id:
            # Reopening a verified ancestor withdraws the PASS that made it
            # verified. Leaving that review attached would show a failed node
            # still carrying a passing verdict until someone calls retry.
            faulty_task["verification"] = None
            faulty_task.pop("reviewer_id", None)
            faulty_task.pop("verified_at", None)
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

    def withdraw_submission(self, node_id: str, actor_id: str) -> None:
        """Return an UNCERTAIN submission to its executor without a new attempt."""

        node = self.node(node_id)
        if node["status"] != "awaiting_verification":
            raise HarnessError(
                f"cannot withdraw {node_id} from {node['status']}; "
                "only an UNCERTAIN submission can be withdrawn"
            )
        verification = node.get("verification")
        if (
            not isinstance(verification, dict)
            or str(verification.get("result", "")).lower() != "uncertain"
        ):
            raise HarnessError("only an UNCERTAIN submission can be withdrawn")
        self._require_non_empty(actor_id, "actor_id")
        if not self._same_actor(actor_id, node.get("executor_id")):
            raise HarnessError("actor_id must match the executor that started the node")
        self._require_non_empty(node.get("submitted_at"), "submitted_at")
        self._archive_review(node, verification)
        node["verification"] = None
        node.pop("reviewer_id", None)
        node.pop("submitted_at", None)
        node["status"] = "running"
        self.validate()

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
        node["status"] = "ready" if self._dependencies_settled(node) else "blocked"
        self.validate()
        return cast(str, node["status"])

    def grant_attempt(self, node_id: str, granted_by: str, reason: str) -> int:
        """Record that a person granted one more attempt, and raise the ceiling.

        The protocol says to stop and escalate when the budget runs out. When
        the answer comes back "take one more", there has to be a way to say so
        that is not editing the graph by hand, which is the one thing agents
        are told not to do.

        ``attempts`` is never touched. The failures that spent the budget stay
        on the record and stay counted, and the grant sits beside them naming
        whoever gave it. An agent must not call this to unblock itself; it
        carries a grantor because the grantor is the point.
        """

        node = self.node(node_id)
        self._require_non_empty(granted_by, "granted_by")
        self._require_non_empty(reason, "reason")
        if node["status"] not in {"failed", "invalidated"}:
            raise HarnessError(
                f"only a failed or invalidated node can be granted an attempt: "
                f"{node_id} is {node['status']}"
            )
        if node["max_attempts"] >= MAX_ATTEMPTS:
            raise HarnessError(
                f"node {node_id} is at the absolute attempt ceiling of {MAX_ATTEMPTS}"
            )
        node["max_attempts"] += 1
        node.setdefault("granted_attempts", []).append(
            {
                "granted_by": granted_by,
                "reason": reason,
                "granted_at": utc_now(),
                "at_attempts": node["attempts"],
            }
        )
        self.validate()
        return cast(int, node["max_attempts"])

    def supersede(self, node_id: str, reason: str) -> list[str]:
        """Retire a node whose approach was abandoned, releasing its dependents.

        Without this, changing approach means rebuilding the whole graph, and a
        rebuild silently resets every attempt budget in it — the exact
        laundering the budget exists to prevent. Retiring one node leaves every
        other verdict, and this node's own failure history, where they are.

        A verified node cannot be superseded: retiring finished work would make
        the record say less than it truthfully can.

        Descendants are released, not restarted. An invalidated descendant
        still needs its own explicit ``retry``, because the protocol has one
        rule about discarded work — that somebody consciously picks it up
        again — and an escape hatch is not a reason to make an exception to it.
        What changes is that ``retry`` now accepts them, because their
        dependency is settled. The returned list names exactly those.
        """

        node = self.node(node_id)
        self._require_non_empty(reason, "reason")
        if node["status"] == "verified":
            raise HarnessError(f"cannot supersede a verified node: {node_id}")
        if node["status"] == "superseded":
            raise HarnessError(f"node {node_id} is already superseded")
        node["status"] = "superseded"
        node["superseded_reason"] = reason
        node["superseded_at"] = utc_now()
        self._promote_blocked_nodes()
        released = [
            descendant
            for descendant in self.descendants(node_id)
            if self.node(descendant)["status"] in {"ready", "blocked", "invalidated"}
            and self._dependencies_settled(self.node(descendant))
        ]
        self.validate()
        return released

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
            "upstream_verified_files": self._upstream_verified_files(node_id),
            "attempt": node["attempts"],
        }

    def _upstream_verified_files(self, node_id: str) -> list[dict[str, Any]]:
        """List the files each verified ancestor was reviewed against.

        A PASS records what a reviewer observed at one moment; it is not a
        binding on the files. Later work can rewrite the very code an ancestor
        was verified against, and nothing in the graph notices. The reviewer of
        the node doing that work is the one placed to catch it, so the packet
        names the surface at risk. A regression there is reported with
        `verify --fail --faulty-node ANCESTOR`, which reopens exactly that node
        and its descendants.

        A packet is only produced while the node awaits verification, and an
        active node's dependencies are verified transitively, so every ancestor
        reached here is verified.
        """

        upstream: list[dict[str, Any]] = []
        for ancestor_id in self.ancestors(node_id):
            context = self.node(ancestor_id).get("review_context", {})
            files = list(context.get("relevant_files", []))
            if files:
                upstream.append({"node": ancestor_id, "files": files})
        return upstream

    def _dependencies_settled(self, node: dict[str, Any]) -> bool:
        """Report whether every dependency of *node* is finished with."""

        return all(
            self.node(dependency)["status"] in SETTLED
            for dependency in node["depends_on"]
        )

    def completion_check(self) -> None:
        incomplete = [
            node["id"] for node in self.nodes if node["status"] not in SETTLED
        ]
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
            # Named rather than folded into the verified count: a reader must
            # be able to see that a node was retired, not finished.
            "superseded": [
                node["id"] for node in self.nodes if node["status"] == "superseded"
            ],
            "complete": counts["verified"] + counts["superseded"] == len(self.nodes),
            "nodes": [
                {
                    "id": node["id"],
                    "status": node["status"],
                    "attempts": node["attempts"],
                    "max_attempts": node["max_attempts"],
                    "failure_reason": node["failure_reason"],
                    "next_action": self._next_action(node),
                }
                for node in self.nodes
            ],
        }

    def _next_action(self, node: Mapping[str, Any]) -> str | None:
        node_id = node["id"]
        status = node["status"]
        if status == "blocked":
            pending = [
                dependency
                for dependency in node["depends_on"]
                if self.node(dependency)["status"] not in SETTLED
            ]
            return f"wait for dependencies: {', '.join(pending)}"
        if status == "ready":
            return f"start {node_id}"
        if status == "running":
            return f"submit {node_id} with executor evidence"
        if status == "awaiting_verification":
            verification = node.get("verification")
            if (
                isinstance(verification, Mapping)
                and str(verification.get("result", "")).lower() == "uncertain"
            ):
                return (
                    f"withdraw {node_id} as the executor, then submit replacement "
                    "evidence"
                )
            return f"verify {node_id} with an independent reviewer"
        if status in {"failed", "invalidated"}:
            if node["attempts"] >= node["max_attempts"]:
                return (
                    "budget exhausted; escalate to a person, who may record a "
                    f"decision with 'grant-attempt {node_id}' or "
                    f"'supersede {node_id}'"
                )
            return f"retry {node_id}"
        return None

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
            # The attempt being discarded was never judged on its own merit, so
            # it must not spend this node's budget. Without the refund, repeated
            # upstream failures strand a descendant that never failed a review:
            # `retry` and `start` both refuse it once `attempts` reaches
            # `max_attempts`, and no CLI command can recover it. Only states
            # that actually hold a discarded attempt are refunded, so a node
            # invalidated twice is not credited twice.
            if previous in {"running", "awaiting_verification", "verified"}:
                node["attempts"] = max(0, node["attempts"] - 1)
            node["status"] = "invalidated"
            node["failure_reason"] = f"Invalidated by failure of {node_id}"
            invalidated.append(descendant_id)
        return invalidated

    def _promote_blocked_nodes(self) -> None:
        for node_id in self._topological_ids():
            node = self.node(node_id)
            if node["status"] != "blocked":
                continue
            if self._dependencies_settled(node):
                node["status"] = "ready"

    @staticmethod
    def _require_non_empty(value: Any, label: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise HarnessError(f"{label} must be a non-empty string")

    @staticmethod
    def _archive_review(node: dict[str, Any], verification: Mapping[str, Any]) -> None:
        """Append one superseded UNCERTAIN review to the bounded audit trail.

        The trail is capped like every other list in the document. Refusing the
        append at the cap would wedge the node instead of bounding it: with a
        full history, `withdraw`, `submit`, and all three `verify` verdicts are
        rejected and no transition remains. Discarding the oldest record keeps
        the node movable and keeps the most recent doubt visible.

        A count alone does not bound it. Each record copies the submission it
        archives, so a few dozen large submissions reach the storage layer's
        document ceiling long before 512 records do -- and past that ceiling
        every transition fails to save, freezing the whole graph rather than one
        node. The trail is therefore bounded by serialized size as well, down to
        empty if one record alone exceeds the budget. Losing archived doubt is
        recoverable; a graph that cannot be written is not.
        """

        history: list[dict[str, Any]] = node.setdefault("review_history", [])
        history.append(
            {
                "verification": copy.deepcopy(dict(verification)),
                "submission_evidence": copy.deepcopy(node["evidence"]),
                "review_context": copy.deepcopy(node.get("review_context", {})),
                "submitted_at": node["submitted_at"],
                "withdrawn_at": utc_now(),
            }
        )
        del history[: max(0, len(history) - MAX_EVIDENCE)]
        while history and len(json.dumps(history)) > MAX_REVIEW_HISTORY_BYTES:
            del history[0]

    @classmethod
    def _record_verification(cls, node: dict[str, Any], review: dict[str, Any]) -> None:
        """Attach *review*, preserving an UNCERTAIN verdict it supersedes.

        `withdraw` archives an UNCERTAIN review so the recorded doubt survives
        resubmission. A verdict recorded straight over one has to do the same,
        or a reviewer's stated uncertainty disappears from the audit trail and
        the node reads as a clean single-review decision. Call this only after
        every check has passed, so a rejected verdict stays a no-op.
        """

        previous = node.get("verification")
        if (
            isinstance(previous, dict)
            and previous.get("result") == "uncertain"
            and isinstance(node.get("submitted_at"), str)
        ):
            cls._archive_review(node, previous)
        node["verification"] = review

    @staticmethod
    def _identity(value: Any) -> str | None:
        """Reduce an actor label to the form used for every identity check.

        Independence is the invariant this harness exists to protect, so the
        comparison must not be defeated by padding or capitalisation:
        `"agent-1 "` and `"Agent-1"` are the same actor as `"agent-1"`.
        Non-string values compare as absent rather than raising, so callers can
        pass a missing `executor_id` straight through.
        """

        if not isinstance(value, str):
            return None
        reduced = value.strip().casefold()
        return reduced or None

    @classmethod
    def _same_actor(cls, left: Any, right: Any) -> bool:
        """Return whether two labels name the same actor, absent never matching."""

        reduced = cls._identity(left)
        return reduced is not None and reduced == cls._identity(right)

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
