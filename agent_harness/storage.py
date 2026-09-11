"""Atomic JSON storage and append-only failure memory."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .errors import HarnessError
from .graph import Graph
from .paths import (
    REFUSAL_TOO_LARGE,
    FileRefusal,
    is_link_like,
    open_bounded_regular_file,
)

MAX_GRAPH_BYTES = 4 * 1024 * 1024


class FileLock:
    """Small cross-platform lock based on exclusive file creation."""

    def __init__(self, path: Path, timeout: float = 5.0):
        self.path = path
        self.timeout = timeout
        self._fd: int | None = None
        self._identity: tuple[int, int] | None = None

    def __enter__(self) -> "FileLock":
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                flags |= getattr(os, "O_NOFOLLOW", 0)
                self._fd = os.open(self.path, flags, 0o600)
                info = os.fstat(self._fd)
                self._identity = (info.st_dev, info.st_ino)
                os.write(self._fd, f"{os.getpid()}\n".encode("ascii"))
                return self
            except (FileExistsError, PermissionError) as exc:
                # Imported here rather than at module scope, because this
                # module is pulled in by the package `__init__`: the edit
                # hook, which runs after every editing tool call and never
                # takes a lock, paid about four milliseconds for `random`
                # hundreds of times a session. This retry is its only caller.
                import random

                if time.monotonic() >= deadline:
                    raise HarnessError(
                        f"timed out waiting for lock: {self.path}"
                    ) from exc
                # PermissionError as well as FileExistsError: on Windows,
                # creating the lock while its previous holder is unlinking it
                # raises access-denied rather than exists, and that escaped
                # the retry entirely. `journal.append` then caught it as an
                # OSError and dropped the record, which is how eight
                # concurrent writers lost lines there and nowhere else.
                #
                # Jittered, because a fixed interval starves a waiter. Every
                # contender slept exactly 50ms and so woke together to race
                # for the same create, and nothing made the loser more likely
                # to win next time: with eight processes appending to one
                # journal on Windows, one of them lost often enough to
                # exhaust the timeout and drop a record. The record was then
                # discarded silently, because an observation layer that
                # raises would cost more than the observation is worth.
                time.sleep(0.01 + random.random() * 0.06)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._fd is not None:
            os.close(self._fd)
        try:
            current = self.path.lstat()
            identity = (current.st_dev, current.st_ino)
            if self._identity == identity and stat.S_ISREG(current.st_mode):
                self.path.unlink()
        except FileNotFoundError:
            pass


class GraphStore:
    """Load and mutate a graph without exposing partial writes."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def load(self) -> Graph:
        """Read the graph, refusing anything that is not a bounded regular file.

        Through the same primitive every other reader of an untrusted file
        uses. This one had none of its guards: a `task-graph.json` that was a
        FIFO has a size of zero, passed the byte limit, and then blocked
        `validate`, `status`, `doctor`, `completion-check` and the installed
        Stop hook for ever — verified by a reviewer against the path
        `claude_hook.main` takes. A symlink was refused only because the two
        callers happened to resolve through `workspace_path` first, which is
        their choice rather than this function's contract.
        """

        if not self.path.exists() and not is_link_like(self.path):
            raise HarnessError(
                f"graph file not found: {self.path}; run the command from the "
                "directory that holds the graph, or create one with "
                "'graphctl init'"
            )
        try:
            descriptor = open_bounded_regular_file(self.path, MAX_GRAPH_BYTES)
        except FileRefusal as exc:
            if exc.reason == REFUSAL_TOO_LARGE:
                raise HarnessError(
                    f"graph file exceeds {MAX_GRAPH_BYTES} bytes"
                ) from exc
            raise HarnessError(
                f"graph file cannot be read ({exc.reason}): {self.path}"
            ) from exc
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                document = json.load(handle)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise HarnessError(f"invalid JSON in {self.path}: {exc}") from exc
        except (OSError, UnicodeDecodeError) as exc:
            raise HarnessError(f"graph file cannot be read: {self.path}") from exc
        if not isinstance(document, dict):
            raise HarnessError("graph document must be a JSON object")
        return Graph(document)

    def save(self, graph: Graph) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(graph.to_dict(), indent=2, sort_keys=False) + "\n"
        if len(rendered.encode("utf-8")) > MAX_GRAPH_BYTES:
            raise HarnessError(f"graph file exceeds {MAX_GRAPH_BYTES} bytes")
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    @contextmanager
    def transaction(self) -> Iterator[Graph]:
        with FileLock(self.lock_path):
            graph = self.load()
            yield graph
            graph.validate()
            self.save(graph)

    def mutate(self, operation: Callable[[Graph], Any]) -> Any:
        with self.transaction() as graph:
            return operation(graph)
