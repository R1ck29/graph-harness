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

from . import journal, paths

# Imported rather than mirrored: doctor names the floor the hook actually
# enforces, and a second copy of it could drift into naming a different one.
from .claude_hook import MINIMUM_PYTHON
from .errors import HarnessError
from .graph import Graph
from .paths import (
    open_bounded_regular_file,
    repository_key,
    user_data_path,
    workspace_path,
)
from .storage import MAX_GRAPH_BYTES, FileLock, GraphStore

MAX_EVIDENCE_FILE_BYTES = 1_048_576

# A `pyvenv.cfg` is a handful of `key = value` lines. The bound is generous
# rather than tight, because its job is to refuse something that is not that
# file at all, not to police a real one.
MAX_RUNTIME_CONFIG_BYTES = 64 * 1024


# The commands that move graph state. A session that edits code and runs none
# of these is the case the journal exists to make visible.
MUTATING_COMMANDS = frozenset(
    {
        "init",
        "add-node",
        "start",
        "submit",
        "verify",
        "withdraw",
        "retry",
        "grant-attempt",
        "supersede",
    }
)


def _read_evidence_file(path: Path) -> str:
    """Read one bounded regular file without following a replaced leaf link.

    This function was where the four guards were worked out; they now live in
    `paths.open_bounded_regular_file`, which the graph store, the journal
    reader and the report loader all share. What stays here is the part that
    is this command's own: the bound it applies and the words it fails with,
    which a person typing `graphctl submit` has to be able to act on.

    The size is still checked again after reading. A file that grew between
    the `fstat` and the read would otherwise pass the bound and arrive over
    it, and the whole point of the bound is that nothing unbounded reaches
    the graph.
    """

    try:
        descriptor = open_bounded_regular_file(path, MAX_EVIDENCE_FILE_BYTES)
    except paths.FileRefusal as exc:
        if exc.reason == paths.REFUSAL_TOO_LARGE:
            raise HarnessError("evidence file exceeds 1 MiB") from exc
        if exc.reason == paths.REFUSAL_NOT_A_REGULAR_FILE:
            raise HarnessError(f"evidence path must be a regular file: {path}") from exc
        raise HarnessError(f"evidence file cannot be read: {path}") from exc
    try:
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


def _runtime_diagnostics() -> dict[str, Any]:
    """Report the interpreter that will actually run the installed hooks.

    A hook configured against a system interpreter older than the supported
    floor fails closed on every session, which looks like a broken harness
    rather than a misconfigured one. The version is read from the runtime's
    own ``pyvenv.cfg`` rather than by launching it, so the diagnosis has no
    side effects.
    """

    report: dict[str, Any] = {
        "runtime_path": None,
        "runtime_version": None,
        "runtime_supported": None,
    }
    try:
        config = user_data_path("venv/pyvenv.cfg")
    except (HarnessError, OSError):
        # Same guard as the journal reads below. Resolving this path touches
        # the filesystem, so a home whose install root is a regular file
        # raises an errno rather than a harness error, and doctor is the one
        # command that must not break when the installation is broken.
        return report
    report["runtime_path"] = str(config.parent)
    # Through the bounded opener, like every other reader of a file this
    # harness did not write. `read_text` was the last one left: a FIFO named
    # `pyvenv.cfg` blocked this function for ever, and so blocked `doctor`,
    # which is the one command the recovery procedure depends on. Verified by
    # driving it on a thread that never returned.
    try:
        descriptor = open_bounded_regular_file(config, MAX_RUNTIME_CONFIG_BYTES)
    except (paths.FileRefusal, OSError, ValueError):
        return report
    try:
        with os.fdopen(descriptor, "rb") as handle:
            lines = handle.read().decode("utf-8").splitlines()
    except (OSError, ValueError):
        return report
    for line in lines:
        name, separator, value = line.partition("=")
        if separator and name.strip() == "version":
            version = value.strip()
            report["runtime_version"] = version
            digits = version.split(".")
            try:
                report["runtime_supported"] = (
                    int(digits[0]),
                    int(digits[1]),
                ) >= MINIMUM_PYTHON
            except (IndexError, ValueError):
                report["runtime_supported"] = None
            break
    return report


def _remember(latest: dict[str, str], entry: dict[str, Any]) -> None:
    """Keep the newest timestamp each client is named by, ignoring anything else."""

    client, stamp = entry.get("client"), entry.get("ts")
    if isinstance(client, str) and isinstance(stamp, str):
        latest[client] = max(stamp, latest.get(client, ""))


def _journal_diagnostics() -> dict[str, Any]:
    """Report what the journal has actually observed, per client.

    Liveness is read from recorded sessions rather than simulated. A client
    whose hooks are configured but never fire leaves no records, which is the
    only honest way to tell a working installation from a decorative one.
    """

    report: dict[str, Any] = {
        "journal_path": None,
        "journal_bytes": 0,
        "journal_months": [],
        "last_observed": {},
        "edit_hook_last_seen": {},
        "warnings": [],
    }
    # Every read below is guarded, and the guards are the point of the
    # command: doctor exists to report that the observation is broken, so it
    # is the one command that must not break when it is. Building the report
    # with these calls inline left an OSError to escape when the journal path
    # was a regular file rather than a directory, and doctor exited 2 with a
    # raw errno where conformance and effectiveness both survived.
    # Named before anything resolves it, and identically on every platform.
    # A regular file where a directory belongs raises NotADirectoryError out
    # of the path walk on POSIX and raises nothing at all on Windows, where
    # the glob simply yields no months — so the same broken install reported
    # a warning on one platform and looked perfectly healthy on the other,
    # which is the one thing doctor exists to prevent. Reporting the errno
    # was also the weaker message: it named the leaf the caller asked for
    # rather than the ancestor that is actually wrong.
    blocking = paths.blocking_ancestor(journal.JOURNAL_DIRECTORY)
    if blocking is not None:
        report["warnings"].append(
            f"The journal directory is unusable: {blocking} is not a directory"
        )
        return report
    try:
        report["journal_path"] = str(journal.journal_directory())
        report["journal_bytes"] = journal.total_bytes()
        report["journal_months"] = [path.name for path in journal.month_files()]
    except (HarnessError, OSError) as exc:
        report["warnings"].append(f"The journal directory is unusable: {exc}")
        return report
    # The months that are not read, which is the silence this command exists
    # to break: every reader skips them, so the history is short and the only
    # place that could say so listed the readable ones and left a reader to
    # assume that was all of them. Which files those are, and why, is the
    # journal's own question; doctor only reports the answer.
    for name, reason in journal.skipped_months():
        if not name:
            # Capitalised like every other warning here. The reason is a
            # fragment rather than a sentence, so it cannot be interpolated
            # at the front without this.
            report["warnings"].append(
                f"{reason[:1].upper()}{reason[1:]}, so every report is empty "
                "rather than short. The journal is not missing a month; it "
                "is unreachable."
            )
            continue
        report["warnings"].append(
            f"The journal file {name} is named like a month and is not read "
            f"({reason}), so every report is missing it."
        )
    latest: dict[str, str] = {}
    edits: dict[str, str] = {}
    try:
        entries = list(journal.read())
    except (HarnessError, OSError) as exc:
        report["warnings"].append(f"The journal could not be read: {exc}")
        return report
    for entry in entries:
        # Two vocabularies, because they answer two questions. An edit record
        # is the only producer that proves `PostToolUse` fires: a client can
        # record boundaries perfectly and still have no edit hook installed,
        # in which case nothing can attribute any change to it and every one
        # of its sessions reads as `unattributed`, which a reader has to be
        # able to tell apart from a client that genuinely only read code. A
        # boundary record is the only thing that proves a session hook ran —
        # a `graph_transition` proves that `graphctl` ran, which a person can
        # do by hand with no hook installed at all.
        if entry.get("event") == "edit":
            _remember(edits, entry)
        elif entry.get("event") in journal.BOUNDARY_EVENTS:
            _remember(latest, entry)
    report["last_observed"] = latest
    report["edit_hook_last_seen"] = edits
    for client in sorted(latest):
        if client not in edits:
            report["warnings"].append(
                f"No {client} edit has been observed. Its PostToolUse hook may "
                "not be installed, in which case there is no signal at all for "
                "that client and every one of its sessions reports as "
                "unattributed."
            )
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
    conform = commands.add_parser(
        "conformance", help="report whether sessions followed the protocol"
    )
    conform.add_argument("--repo", help="limit the report to one repository")
    conform.add_argument(
        "--min-files",
        type=int,
        default=0,
        dest="min_files",
        help="omit sessions that changed fewer files than this",
    )
    conform.add_argument(
        "--no-codex",
        action="store_true",
        dest="no_codex",
        help="omit sessions recovered from the Codex session store",
    )
    conform.add_argument(
        "--prune",
        action="store_true",
        help="delete journal months beyond the retention bound",
    )
    conform.add_argument(
        "--keep-months",
        type=int,
        default=journal.MAX_JOURNAL_MONTHS,
        dest="keep_months",
        help="months of history to retain when pruning",
    )
    commands.add_parser("status", help="summarize progress")
    commands.add_parser("completion-check", help="check whether every task is verified")

    effect = commands.add_parser(
        "effectiveness",
        help="report what independent review caught and what rework cost",
    )
    effect.add_argument(
        "--graph",
        action="append",
        default=[],
        dest="graphs",
        help="a graph file to fold in; repeat for archives. Defaults to every "
        "task-graph*.json beside the workspace graph",
    )

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

    grant = commands.add_parser(
        "grant-attempt",
        help="record that a person granted one more attempt to a failed task",
    )
    grant.add_argument("node")
    grant.add_argument(
        "--granted-by",
        required=True,
        help="who decided this; an agent must not grant itself an attempt",
    )
    grant.add_argument("--reason", required=True, help="why the budget was extended")

    supersede = commands.add_parser(
        "supersede",
        help="retire a task whose approach was abandoned, releasing its dependents",
    )
    supersede.add_argument("node")
    supersede.add_argument(
        "--reason", required=True, help="why this approach was abandoned"
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
        journal_report = _journal_diagnostics()
        report["warnings"].extend(journal_report.pop("warnings"))
        report.update(journal_report)
        report.update(_runtime_diagnostics())
        if report["runtime_supported"] is False:
            report["warnings"].append(
                f"The installed runtime reports Python {report['runtime_version']}, "
                f"below the supported {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]}. Hooks "
                "using it fail closed on every session; reinstall against a "
                "supported interpreter."
            )
        for client in ("claude", "codex"):
            if client not in report["last_observed"]:
                report["warnings"].append(
                    f"No {client} session has been observed. Its hooks may not be "
                    "installed, or the client may not run them. Run "
                    "'python scripts/install_pc.py --check' to compare the "
                    "installed configuration with what this harness expects."
                )
        # Journal and runtime findings are advisory. They describe how much of
        # the harness is observable, not whether this graph can be worked on,
        # so they must not change the health of the graph itself.
        report["healthy"] = report["graph_valid"] and not lock_present
        return report
    if args.command == "validate":
        graph = store.load()
        return {"valid": True, "nodes": len(graph.nodes)}
    if args.command == "ready":
        return {"ready": store.load().ready_node_ids()}
    if args.command == "conformance":
        # Imported here, as `effectiveness` already is: it reaches
        # `codex_sessions` and so `sqlite3`, which is about a third of this
        # module's import cost and is paid by every other subcommand too.
        from . import conformance

        if args.prune:
            return {"pruned": journal.prune(args.keep_months)}
        return conformance.report(
            repo=args.repo,
            min_files=args.min_files,
            include_codex=not args.no_codex,
        )
    if args.command == "status":
        return store.load().status_summary()
    if args.command == "effectiveness":
        from . import effectiveness

        chosen = [Path(name) for name in args.graphs]
        if not chosen:
            chosen = sorted(Path.cwd().glob("task-graph*.json"))
        return effectiveness.report(chosen)
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
    if args.command == "grant-attempt":
        ceiling = store.mutate(
            lambda graph: graph.grant_attempt(
                args.node, granted_by=args.granted_by, reason=args.reason
            )
        )
        return {
            "node": args.node,
            "max_attempts": ceiling,
            "granted_by": args.granted_by,
            "next_action": f"retry {args.node}",
        }
    if args.command == "supersede":
        released = store.mutate(
            lambda graph: graph.supersede(args.node, reason=args.reason)
        )
        return {
            "node": args.node,
            "status": "superseded",
            "released": released,
            "next_action": (
                f"retry each released task: {', '.join(released)}" if released else None
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


def _observe(args: argparse.Namespace, result: Any) -> None:
    """Record one state change so a session that skipped the graph stands out.

    Journaling happens here, at the single point where a command has already
    succeeded, rather than inside each handler. A command that raised never
    reaches this line, so "a failed command records nothing" holds by
    construction instead of by every mutating call site agreeing.
    """

    if args.command not in MUTATING_COMMANDS or not isinstance(result, dict):
        return
    try:
        # Claude Code exports the session id into tool subprocesses, so a
        # transition can name the session that ran it directly. Codex does
        # not, and those records are matched by repository and time instead.
        session = os.environ.get("CLAUDE_CODE_SESSION_ID")
        journal.append(
            journal.record(
                "graph_transition",
                client="claude" if session else None,
                session_id=session,
                repo=repository_key(),
                command=args.command,
                node=result.get("node"),
                # A verify reports its verdict as "result" rather than a
                # status, and folding a session's history needs to tell a PASS
                # from a FAIL.
                status=result.get("status") or result.get("result"),
                actor=getattr(args, "actor_id", None)
                or getattr(args, "executor_id", None)
                or getattr(args, "reviewer_id", None),
            )
        )
    except Exception:  # noqa: BLE001 - the command already succeeded
        # The command's work is committed by the time this runs. Anything
        # raised here would reach main's handler and report exit 2 for a
        # change that actually happened, which is a worse outcome than losing
        # one observation. Path.cwd() alone can raise when the working
        # directory has been removed underneath the process.
        return


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        arguments = parser.parse_args(argv)
        result = run(arguments)
        _observe(arguments, result)
        _print(result)
        return 0
    except (HarnessError, OSError, json.JSONDecodeError) as exc:
        print(f"graphctl: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
