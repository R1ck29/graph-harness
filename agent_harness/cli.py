"""Command-line interface for deterministic graph state changes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .errors import HarnessError
from .graph import Graph
from .paths import workspace_path
from .storage import GraphStore


def _evidence(value: str, criterion: str | None = None) -> dict[str, str]:
    if value.startswith("@"):
        evidence_path = _workspace_path(value[1:])
        if evidence_path.stat().st_size > 1_048_576:
            raise HarnessError("evidence file exceeds 1 MiB")
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise HarnessError("evidence file must contain one JSON object")
        return {str(key): str(item) for key, item in payload.items()}
    if value.startswith("{"):
        payload = json.loads(value)
        if not isinstance(payload, dict):
            raise HarnessError("evidence JSON must be an object")
        return {str(key): str(item) for key, item in payload.items()}
    item = {"kind": "observation", "summary": value, "value": value}
    if criterion:
        item["criterion"] = criterion
    return item


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="graphctl")
    parser.add_argument(
        "--graph", default="task-graph.json", help="task graph JSON path"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate")
    validate.add_argument("path", nargs="?")
    ready = commands.add_parser("ready")
    ready.add_argument("path", nargs="?")
    commands.add_parser("status")
    commands.add_parser("completion-check")

    start = commands.add_parser("start")
    start.add_argument("node")
    start.add_argument("--executor-id")

    submit = commands.add_parser("submit")
    submit.add_argument("node")
    submit.add_argument("--evidence", action="append", default=[])
    submit.add_argument("--criterion")
    submit.add_argument("--actor-id")
    submit.add_argument("--relevant-file", action="append", default=[])
    submit.add_argument("--diff-artifact")
    submit.add_argument("--test-output-artifact")

    verify = commands.add_parser("verify")
    verify.add_argument("node")
    verdict = verify.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--pass", dest="passed", action="store_true")
    verdict.add_argument("--fail", dest="failed", action="store_true")
    verdict.add_argument("--uncertain", action="store_true")
    verify.add_argument(
        "--reviewer-id", "--reviewer", dest="reviewer_id", required=True
    )
    verify.add_argument("--criterion", action="append", default=[])
    verify.add_argument("--evidence", action="append", default=[])
    verify.add_argument("--reason")
    verify.add_argument("--recommendation")
    verify.add_argument("--failed-criterion", action="append", default=[])
    verify.add_argument("--faulty-node")
    verify.add_argument("--faulty-criterion", action="append", default=[])

    retry = commands.add_parser("retry")
    retry.add_argument("node")
    packet = commands.add_parser("review-packet")
    packet.add_argument("node")

    failures = commands.add_parser("failures")
    failures.add_argument("--node")
    failures.add_argument("--type", dest="failure_type")
    failures.add_argument("--limit", type=int, default=20)
    return parser


_workspace_path = workspace_path


def _store(args: argparse.Namespace) -> GraphStore:
    path = getattr(args, "path", None) or args.graph
    return GraphStore(_workspace_path(path))


def run(args: argparse.Namespace) -> Any:
    store = _store(args)
    if args.command == "validate":
        graph = store.load()
        return {"valid": True, "nodes": len(graph.nodes)}
    if args.command == "ready":
        return {"ready": store.load().ready_node_ids()}
    if args.command == "status":
        return store.load().status_summary()
    if args.command == "completion-check":
        graph = store.load()
        graph.completion_check()
        return {"complete": True}
    if args.command == "review-packet":
        return store.load().review_packet(args.node)
    if args.command == "failures":
        return {
            "failures": store.load().failure_records(
                node=args.node, failure_type=args.failure_type, limit=args.limit
            )
        }
    if args.command == "start":
        store.mutate(lambda graph: graph.start(args.node, args.executor_id))
        return {"node": args.node, "status": "running"}
    if args.command == "submit":
        items = [_evidence(value, args.criterion) for value in args.evidence]
        review_context = {
            "relevant_files": args.relevant_file,
            "diff_artifact": args.diff_artifact,
            "test_output_artifact": args.test_output_artifact,
        }
        store.mutate(
            lambda graph: graph.submit(
                args.node,
                items,
                actor_id=args.actor_id,
                review_context=review_context,
            )
        )
        return {"node": args.node, "status": "awaiting_verification"}
    if args.command == "retry":
        status = store.mutate(lambda graph: graph.retry(args.node))
        return {"node": args.node, "status": status}
    if args.command == "verify":
        result = "pass" if args.passed else "fail" if args.failed else "uncertain"
        review_items = [_evidence(value) for value in args.evidence]

        def operation(graph: Graph) -> list[str]:
            invalidated = graph.verify(
                args.node,
                result,
                args.reviewer_id,
                checked_criteria=args.criterion or None,
                review_evidence=review_items or None,
                reason=args.reason,
                recommendation=args.recommendation,
                failed_criteria=args.failed_criterion or None,
                faulty_node=args.faulty_node,
                faulty_criteria=args.faulty_criterion or None,
            )
            return invalidated

        invalidated = store.mutate(operation)
        return {
            "node": args.node,
            "faulty_node": (
                (args.faulty_node or args.node) if result == "fail" else None
            ),
            "result": result.upper(),
            "invalidated": invalidated,
        }
    raise HarnessError(f"unsupported command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        result = run(parser.parse_args(argv))
        _print(result)
        return 0
    except (HarnessError, OSError, json.JSONDecodeError) as exc:
        print(f"graphctl: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
