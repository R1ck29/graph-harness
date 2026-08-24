#!/usr/bin/env python3
"""Generate platform skill trees from the canonical `skills/` directory."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

TARGETS = (
    Path(".agents/skills"),
    Path(".claude/skills"),
    Path("adapters/codex/skills"),
    Path("adapters/claude/skills"),
)


class SyncError(ValueError):
    """Raised when adapter generation encounters an unsafe tree."""


def _assert_safe(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise SyncError(f"path escapes adapter root: {path}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise SyncError(f"symlink is not allowed in adapter trees: {current}")


def _tree_files(root: Path, tree: Path) -> list[Path]:
    _assert_safe(root, tree)
    if not tree.exists():
        return []
    files: list[Path] = []
    for path in tree.rglob("*"):
        _assert_safe(root, path)
        if path.is_symlink():
            raise SyncError(f"symlink is not allowed in adapter trees: {path}")
        if path.is_file():
            files.append(path)
        elif not path.is_dir():
            raise SyncError(f"special filesystem entry is not allowed: {path}")
    return sorted(files)


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with source.open("rb") as input_handle, os.fdopen(descriptor, "wb") as output:
            output.write(input_handle.read())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def source_files(root: Path) -> list[Path]:
    return _tree_files(root, root / "skills")


def sync(root: Path, check: bool) -> list[str]:
    sources = source_files(root)
    drift: list[str] = []
    source_root = root / "skills"
    expected = {source.relative_to(source_root) for source in sources}
    for target_relative in TARGETS:
        target_root = root / target_relative
        actual = {
            path.relative_to(target_root) for path in _tree_files(root, target_root)
        }
        for stale in sorted(actual - expected):
            target = target_root / stale
            drift.append(str(target_relative / stale))
            if not check:
                target.unlink()
        for source in sources:
            relative = source.relative_to(source_root)
            target = target_root / relative
            if not target.exists() or target.read_bytes() != source.read_bytes():
                drift.append(str(target_relative / relative))
                if not check:
                    _assert_safe(root, target)
                    _atomic_copy(source, target)
    return drift


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        drift = sync(args.root.resolve(), args.check)
    except (OSError, SyncError) as exc:
        print(f"adapter sync refused unsafe input: {exc}", file=sys.stderr)
        return 2
    if args.check and drift:
        print(
            "adapter drift detected:\n" + "\n".join(f"- {item}" for item in drift),
            file=sys.stderr,
        )
        return 1
    if drift:
        print("generated:\n" + "\n".join(f"- {item}" for item in drift))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
