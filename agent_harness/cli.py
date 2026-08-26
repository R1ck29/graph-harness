"""Command-line interface for deterministic graph state changes."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

from .errors import HarnessError
from .graph import Graph
from .paths import workspace_path
from .storage import MAX_GRAPH_BYTES, FileLock, GraphStore

MAX_EVIDENCE_FILE_BYTES = 1_048_576


def _read_evidence_file(path: Path) -> str:
    """Read one bounded regular file without following a replaced leaf link."""

    initial = path.lstat()
    if not stat.S_ISREG(initial.st_mode):
        raise HarnessError(f"evidence path must be a regular file: {path}")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            initial.st_dev,
            initial.st_ino,
        ):
            raise HarnessError(f"evidence file changed while it was inspected: {path}")
        if opened.st_size > MAX_EVIDENCE_FILE_BYTES:
            raise HarnessError("evidence file exceeds 1 MiB")
        chunks: list[bytes] = []
        remaining = MAX_EVIDENCE_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if len(payload) > MAX_EVIDENCE_FILE_BYTES:
        raise HarnessError("evidence file exceeds 1 MiB")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HarnessError("evidence file must be UTF-8 JSON") from exc


def _evidence(value: str, criterion: str | None = None) -> dict[str, str]:
    if value.startswith("@"):
        evidence_path = _workspace_path(value[1:])
        payload = json.loads(_read_evidence_file(evidence_path))
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


def _lock_diagnostics(path: Path) -> dict[str, Any]:
    report: dict[str, Any] = {
        "lock_path": str(path),
        "lock_present": False,
        "lock_pid": None,
        "warnings": [],
    }
    try:
        initial = path.lstat()
    except FileNotFoundError:
        return report
    report["lock_present"] = True
    if not stat.S_ISREG(initial.st_mode):
        report["warnings"].append(
            "The lock is not a regular file, so its process ID was not read."
        )
        return report
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        report["warnings"].append(
            f"The lock process ID could not be read safely: {exc}"
        )
        return report
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            initial.st_dev,
            initial.st_ino,
        ):
            report["warnings"].append(
                "The lock changed while it was inspected, so its process ID was not read."
            )
            return report
        payload = os.read(descriptor, 129)
    finally:
        os.close(descriptor)
    if len(payload) > 128:
        report["warnings"].append(
            "The lock contents are unexpectedly large, so its process ID was not used."
        )
        return report
    try:
        recorded = payload.decode("ascii").strip()
        pid = int(recorded)
    except (UnicodeDecodeError, ValueError):
        report["warnings"].append("The lock does not contain a valid process ID.")
        return report
    if pid <= 0:
        report["warnings"].append("The lock does not contain a valid process ID.")
        return report
    report["lock_pid"] = pid
    return report


def _save_new_graph(store: GraphStore, graph: Graph) -> None:
    """Publish a complete graph atomically without replacing an existing path."""

    rendered = (json.dumps(graph.to_dict(), indent=2, sort_keys=False) + "\n").encode(
        "utf-8"
    )
    if len(rendered) > MAX_GRAPH_BYTES:
        raise HarnessError(f"graph file exceeds {MAX_GRAPH_BYTES} bytes")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{store.path.name}.", suffix=".tmp", dir=store.path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, store.path)
        except FileExistsError as exc:
            raise HarnessError(
                f"graph file already exists: {store.path}; choose another --graph path"
            ) from exc
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="graphctl",
        description="Track task progress and independent review in a local JSON graph.",
    )
    parser.add_argument(
        "--graph", default="task-graph.json", help="task graph JSON path"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser(
        "init", help="create a new one-task graph without overwriting a file"
    )
    init.add_argument("--objective", required=True, help="the outcome to achieve")
    init.add_argument(
        "--criterion",
        action="append",
        required=True,
        help="acceptance criterion; repeat for each criterion",
    )
    init.add_argument("--node", default="task", help="task identifier (default: task)")
    init.add_argument(
        "--description",
        help="the observable outcome of the first task (default: the objective)",
    )
    init.add_argument(
        "--role", default="implementer", help="assigned role (default: implementer)"
    )
    init.add_argument(
        "--max-attempts",
        type=int,
        default=2,
        dest="max_attempts",
        help="attempt budget (default: 2)",
    )
    add = commands.add_parser(
        "add-node", help="add one planned task to an existing graph"
    )
    add.add_argument("node", help="task identifier")
    add.add_argument(
        "--description", required=True, help="the observable outcome of the task"
    )
    add.add_argument(
        "--criterion",
        action="append",
        required=True,
        help="acceptance criterion; repeat for each criterion",
    )
    add.add_argument(
        "--depends-on",
        action="append",
        default=[],
        dest="depends_on",
        help="existing task this one requires; repeat for each dependency",
    )
    add.add_argument(
        "--role", default="implementer", help="assigned role (default: implementer)"
    )
    add.add_argument(
        "--max-attempts",
        type=int,
        default=2,
        dest="max_attempts",
        help="attempt budget (default: 2)",
    )
    commands.add_parser(
        "doctor", help="report graph and lock diagnostics without changing files"
    )

    validate = commands.add_parser("validate", help="validate a graph file")
    validate.add_argument("path", nargs="?")
    ready = commands.add_parser("ready", help="list tasks ready to start")
    ready.add_argument("path", nargs="?")
    commands.add_parser("status", help="summarize progress")
    commands.add_parser("completion-check", help="check whether every task is verified")

    start = commands.add_parser("start", help="start a ready task and open an attempt")
    start.add_argument("node")
    start.add_argument("--executor-id")

    submit = commands.add_parser("submit", help="submit executor evidence for review")
    submit.add_argument("node")
    evidence_help = (
        "evidence; repeat as needed. Warning: content persists in the graph and may "
        "remain in shell history; use a redacted @file when appropriate"
    )
    submit.add_argument("--evidence", action="append", default=[], help=evidence_help)
    submit.add_argument("--criterion")
    submit.add_argument("--actor-id")
    submit.add_argument("--relevant-file", action="append", default=[])
    submit.add_argument("--diff-artifact")
    submit.add_argument("--test-output-artifact")

    verify = commands.add_parser("verify", help="record an independent review result")
    verify.add_argument("node")
    verdict = verify.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--pass", dest="passed", action="store_true")
    verdict.add_argument("--fail", dest="failed", action="store_true")
    verdict.add_argument("--uncertain", action="store_true")
    verify.add_argument(
        "--reviewer-id", "--reviewer", dest="reviewer_id", required=True
    )
    verify.add_argument("--criterion", action="append", default=[])
    verify.add_argument("--evidence", action="append", default=[], help=evidence_help)
    verify.add_argument("--reason")
    verify.add_argument("--recommendation")
    verify.add_argument("--failed-criterion", action="append", default=[])
    verify.add_argument("--faulty-node")
    verify.add_argument("--faulty-criterion", action="append", default=[])

    withdraw = commands.add_parser(
        "withdraw",
        help="return an UNCERTAIN submission to its executor for resubmission",
    )
    withdraw.add_argument("node")
    withdraw.add_argument(
        "--actor-id", required=True, help="must match the task executor"
    )

    retry = commands.add_parser("retry", help="reset a failed task for a new attempt")
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
    if args.command == "init":
        document = {
            "version": 1,
            "objective": args.objective,
            "nodes": [
                {
                    "id": args.node,
                    "description": args.description or args.objective,
                    "status": "blocked",
                    "depends_on": [],
                    "assigned_role": args.role,
                    "acceptance_criteria": args.criterion,
                    "evidence": [],
                    "attempts": 0,
                    "max_attempts": args.max_attempts,
                    "failure_reason": None,
                }
            ],
        }
        graph = Graph.from_dict(document)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        if store.path.exists():
            raise HarnessError(
                f"graph file already exists: {store.path}; choose another --graph path"
            )
        with FileLock(store.lock_path):
            if store.path.exists():
                raise HarnessError(
                    f"graph file already exists: {store.path}; choose another --graph path"
                )
            _save_new_graph(store, graph)
        return {
            "created": str(store.path),
            "node": args.node,
            "status": graph.node(args.node)["status"],
            "next_action": (
                f"Start node {args.node!r} with the executor identity; use the same "
                "--graph value supplied to this command."
            ),
        }
    if args.command == "add-node":
        status = store.mutate(
            lambda graph: graph.add_node(
                args.node,
                args.description,
                args.criterion,
                depends_on=args.depends_on,
                assigned_role=args.role,
                max_attempts=args.max_attempts,
            )
        )
        return {
            "node": args.node,
            "status": status,
            "next_action": (
                f"Add the tasks that depend on {args.node!r}, then validate the "
                "graph before execution."
            ),
        }
    if args.command == "doctor":
        lock_report = _lock_diagnostics(store.lock_path)
        lock_present = bool(lock_report["lock_present"])
        report: dict[str, Any] = {
            "graph": str(store.path),
            "graph_exists": store.path.exists(),
            "graph_valid": False,
            **lock_report,
        }
        if lock_present:
            report["warnings"].append(
                "A lock is present. Another operation may be active; do not delete it "
                "unless you have confirmed no graphctl process is running."
            )
        try:
            graph = store.load()
        except (HarnessError, OSError, json.JSONDecodeError) as exc:
            report["graph_error"] = str(exc)
        else:
            report["graph_valid"] = True
            report["node_count"] = len(graph.nodes)
        report["healthy"] = report["graph_valid"] and not lock_present
        return report
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
    if args.command == "withdraw":
        store.mutate(lambda graph: graph.withdraw_submission(args.node, args.actor_id))
        return {
            "node": args.node,
            "status": "running",
            "next_action": (
                f"Submit replacement evidence for node {args.node!r} using the "
                "same executor identity."
            ),
        }
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
        response = {
            "node": args.node,
            "faulty_node": (
                (args.faulty_node or args.node) if result == "fail" else None
            ),
            "result": result.upper(),
            "invalidated": invalidated,
        }
        if result == "uncertain":
            response["next_action"] = (
                f"Have the original executor withdraw node {args.node!r}, then "
                "submit updated evidence."
            )
        return response
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
