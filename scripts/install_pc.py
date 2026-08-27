#!/usr/bin/env python3
"""Install the harness once for user-scoped Codex and Claude Code discovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence, cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_harness.paths import is_link_like  # noqa: E402

SKILL_NAMES = (
    "graph-planning",
    "graph-execution",
    "independent-verification",
    "selective-recovery",
)
MANAGED_BEGIN = "<!-- BEGIN graph-engineering-agent-harness -->"
MANAGED_END = "<!-- END graph-engineering-agent-harness -->"
HOOK_MARKER = "graphctl-claude-stop"
INSTALL_DIRECTORY = Path(".local/share/graph-engineering-agent-harness")
MANAGED_PATTERN = re.compile(
    rf"(?:\n)?{re.escape(MANAGED_BEGIN)}\n.*?{re.escape(MANAGED_END)}(?:\n)?",
    re.DOTALL,
)


class InstallError(RuntimeError):
    """A safe precondition or installed-state check failed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_text(path: Path, value: str) -> None:
    _atomic_write(path, value.encode("utf-8"))


def _managed_instructions(existing: str, block: str) -> str:
    base = MANAGED_PATTERN.sub("", existing)
    separator = "" if not base else ("\n" if base.endswith("\n") else "\n\n")
    return f"{base}{separator}{MANAGED_BEGIN}\n{block.rstrip()}\n{MANAGED_END}\n"


def _without_managed_instructions(existing: str) -> str:
    return MANAGED_PATTERN.sub("", existing)


def _load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot safely merge Claude settings: {exc}") from exc
    if not isinstance(value, dict):
        raise InstallError("Claude settings must contain a JSON object")
    return cast("dict[str, Any]", value)


def _without_managed_hooks(
    settings: dict[str, Any], managed_commands: set[str]
) -> dict[str, Any]:
    result = cast("dict[str, Any]", json.loads(json.dumps(settings)))
    hooks = result.get("hooks")
    if not isinstance(hooks, dict):
        return result
    stop = hooks.get("Stop")
    if not isinstance(stop, list):
        return result
    retained: list[Any] = []
    for matcher in stop:
        if not isinstance(matcher, dict):
            retained.append(matcher)
            continue
        entries = matcher.get("hooks")
        if not isinstance(entries, list):
            retained.append(matcher)
            continue
        filtered = [
            entry for entry in entries if not _is_managed_hook(entry, managed_commands)
        ]
        if filtered:
            copy = dict(matcher)
            copy["hooks"] = filtered
            retained.append(copy)
    if retained:
        hooks["Stop"] = retained
    else:
        hooks.pop("Stop", None)
    if not hooks:
        result.pop("hooks", None)
    return result


def _is_managed_hook(entry: Any, managed_commands: set[str]) -> bool:
    """Recognize only the exact exec-form hook or its legacy shell-form shape."""

    if not isinstance(entry, dict):
        return False
    if entry.get("type") != "command" or entry.get("command") not in managed_commands:
        return False
    if entry.get("timeout") != 10:
        return False
    return "args" not in entry or entry.get("args") == []


def _with_managed_hook(
    settings: dict[str, Any], command: str, previous_commands: set[str]
) -> dict[str, Any]:
    result = _without_managed_hooks(settings, previous_commands | {command})
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError("Claude settings 'hooks' must be a JSON object")
    stop = hooks.setdefault("Stop", [])
    if not isinstance(stop, list):
        raise InstallError("Claude settings 'hooks.Stop' must be a JSON array")
    stop.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "args": [],
                    "timeout": 10,
                }
            ]
        }
    )
    return result


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError):
        return False
    return True


def _assert_safe_user_path(home: Path, path: Path) -> None:
    """Reject link-like parents before reading or replacing user config."""

    try:
        relative = path.relative_to(home)
    except ValueError as exc:
        raise InstallError(f"managed path escapes the selected home: {path}") from exc
    current = home
    for part in relative.parts:
        current = current / part
        if is_link_like(current):
            raise InstallError(f"managed path contains a link-like entry: {current}")


def _runtime_executables(bin_directory: Path) -> tuple[Path, Path]:
    suffix = ".exe" if os.name == "nt" else ""
    return (
        bin_directory / f"graphctl{suffix}",
        bin_directory / f"graphctl-claude-stop{suffix}",
    )


def _create_runtime(install_root: Path, python: Path) -> Path:
    runtime = install_root / "venv"
    if not runtime.exists():
        subprocess.run([str(python), "-m", "venv", str(runtime)], check=True)
    bin_directory = runtime / ("Scripts" if os.name == "nt" else "bin")
    pip = bin_directory / ("pip.exe" if os.name == "nt" else "pip")
    with tempfile.TemporaryDirectory(prefix="graph-harness-build-") as temporary:
        staged_source = Path(temporary) / "source"
        staged_source.mkdir()
        shutil.copy2(ROOT / "pyproject.toml", staged_source / "pyproject.toml")
        shutil.copytree(
            ROOT / "agent_harness",
            staged_source / "agent_harness",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        subprocess.run(
            [
                str(pip),
                "install",
                "--disable-pip-version-check",
                "--force-reinstall",
                "--no-deps",
                str(staged_source),
            ],
            check=True,
        )
    return bin_directory


def _asset_sources() -> dict[Path, Path]:
    sources: dict[Path, Path] = {}
    for name in SKILL_NAMES:
        sources[Path("skills") / name / "SKILL.md"] = (
            ROOT / "skills" / name / "SKILL.md"
        )
    sources[Path("agents/codex/graph_reviewer.toml")] = (
        ROOT / ".codex/agents/reviewer.toml"
    )
    sources[Path("agents/claude/graph-reviewer.md")] = (
        ROOT / ".claude/agents/reviewer.md"
    )
    return sources


def _targets(
    home: Path, payload: Path, runtime_bin: Path | None = None
) -> dict[Path, Path]:
    targets: dict[Path, Path] = {}
    for name in SKILL_NAMES:
        shared = payload / "skills" / name
        targets[home / ".agents/skills" / name] = shared
        targets[home / ".claude/skills" / name] = shared
    targets[home / ".codex/agents/graph_reviewer.toml"] = (
        payload / "agents/codex/graph_reviewer.toml"
    )
    targets[home / ".claude/agents/graph-reviewer.md"] = (
        payload / "agents/claude/graph-reviewer.md"
    )
    if runtime_bin is not None:
        graphctl, guard = _runtime_executables(runtime_bin)
        targets[home / ".local/bin/graphctl"] = graphctl
        targets[home / ".local/bin/graphctl-claude-stop"] = guard
    return targets


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot read install manifest: {exc}") from exc
    return value if isinstance(value, dict) else {}


def _preflight_targets(targets: dict[Path, Path]) -> None:
    conflicts: list[str] = []
    for target, expected in targets.items():
        if not target.exists() and not is_link_like(target):
            continue
        if _target_matches(target, expected):
            continue
        conflicts.append(str(target))
    if conflicts:
        raise InstallError(
            "unmanaged target conflict; no changes made: " + ", ".join(conflicts)
        )


def _preflight_user_paths(home: Path, targets: dict[Path, Path]) -> None:
    for path in (
        home / ".codex/AGENTS.md",
        home / ".claude/CLAUDE.md",
        home / ".claude/settings.json",
    ):
        _assert_safe_user_path(home, path)
    for target in targets:
        _assert_safe_user_path(home, target.parent)
    _load_settings(home / ".claude/settings.json")


def _requires_copy(target: Path) -> bool:
    """Return whether client discovery requires a regular adapter file."""

    return target.parent.name == "agents" and target.suffix in {".md", ".toml"}


def _target_matches(target: Path, source: Path) -> bool:
    if is_link_like(target):
        return target.is_symlink() and target.resolve(strict=False) == source.resolve(
            strict=False
        )
    if target.is_dir() and source.is_dir():
        target_files = {
            path.relative_to(target): _sha256(path)
            for path in target.rglob("*")
            if path.is_file()
        }
        source_files = {
            path.relative_to(source): _sha256(path)
            for path in source.rglob("*")
            if path.is_file()
        }
        return target_files == source_files
    return (
        _requires_copy(target)
        and target.is_file()
        and source.is_file()
        and _sha256(target) == _sha256(source)
    )


def _install_link(target: Path, source: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if _requires_copy(target):
        if not is_link_like(target) and _target_matches(target, source):
            return
        _atomic_write(target, source.read_bytes())
        return
    if _target_matches(target, source):
        return
    if os.name == "nt":
        if source.is_dir():
            if target.is_dir() and all(
                (target / child.name).is_file()
                and _sha256(target / child.name) == _sha256(child)
                for child in source.iterdir()
                if child.is_file()
            ):
                return
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)
        else:
            _atomic_write(target, source.read_bytes())
        return
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(source, target_is_directory=source.is_dir())
    os.replace(temporary, target)


def _backup(path: Path, home: Path, backup_root: Path) -> None:
    if not path.exists() or path.is_symlink():
        return
    destination = backup_root / path.relative_to(home)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)


def _expected_state(
    home: Path, install_root: Path, runtime_bin: Path
) -> tuple[dict[Path, Path], str, dict[str, Any]]:
    graphctl, guard = _runtime_executables(runtime_bin)
    if not all(
        executable.is_file() and os.access(executable, os.X_OK)
        for executable in (graphctl, guard)
    ):
        raise InstallError(f"runtime executables are missing from {runtime_bin}")
    template = (ROOT / "adapters/global-instructions.md").read_text(encoding="utf-8")
    block = template.format(graphctl=graphctl)
    previous_commands: set[str] = set()
    manifest = _load_manifest(install_root / "manifest.json")
    previous_runtime = manifest.get("runtime_bin")
    if isinstance(previous_runtime, str):
        _, previous_guard = _runtime_executables(Path(previous_runtime))
        previous_commands.add(str(previous_guard))
    settings = _with_managed_hook(
        _load_settings(home / ".claude/settings.json"), str(guard), previous_commands
    )
    return _targets(home, install_root / "payload", runtime_bin), block, settings


def install(home: Path, install_root: Path, runtime_bin: Path, dry_run: bool) -> None:
    manifest_path = install_root / "manifest.json"
    _load_manifest(manifest_path)
    targets, block, settings = _expected_state(home, install_root, runtime_bin)
    _preflight_user_paths(home, targets)
    _preflight_targets(targets)
    if dry_run:
        print("dry-run: installation preflight passed; no files written")
        return
    payload = install_root / "payload"
    for relative, source in _asset_sources().items():
        destination = payload / relative
        source_bytes = source.read_bytes()
        if not destination.exists() or destination.read_bytes() != source_bytes:
            _atomic_write(destination, source_bytes)
    instruction_paths = (home / ".codex/AGENTS.md", home / ".claude/CLAUDE.md")
    settings_path = home / ".claude/settings.json"
    instruction_updates: dict[Path, str] = {}
    for path in instruction_paths:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        updated = _managed_instructions(existing, block)
        if updated != existing:
            instruction_updates[path] = updated
    rendered_settings = _json_bytes(settings)
    settings_changed = rendered_settings != (
        settings_path.read_bytes() if settings_path.exists() else b""
    )
    changed_configs = [*instruction_updates]
    if settings_changed:
        changed_configs.append(settings_path)
    if changed_configs:
        backup_root = install_root / "backups" / str(time.time_ns())
        for path in changed_configs:
            _backup(path, home, backup_root)
    for path, updated in instruction_updates.items():
        _write_text(path, updated)
    if settings_changed:
        _atomic_write(settings_path, rendered_settings)
    for target, source in targets.items():
        _install_link(target, source)
    receipt = {
        "version": 1,
        "home": str(home),
        "runtime_bin": str(runtime_bin),
        "targets": [str(path) for path in targets],
        "source_hashes": {
            str(relative): _sha256(source)
            for relative, source in _asset_sources().items()
        },
    }
    _atomic_write(manifest_path, _json_bytes(receipt))
    print(f"installed Codex and Claude Code harness for {home}")


def check(home: Path, install_root: Path, runtime_bin: Path) -> None:
    if not _load_manifest(install_root / "manifest.json"):
        raise InstallError("install drift: manifest is missing")
    targets, block, settings = _expected_state(home, install_root, runtime_bin)
    problems: list[str] = []
    for relative, source in _asset_sources().items():
        installed = install_root / "payload" / relative
        if not installed.is_file() or _sha256(installed) != _sha256(source):
            problems.append(f"payload drift: {relative}")
    for target, expected in targets.items():
        if not _target_matches(target, expected):
            problems.append(f"target drift: {target}")
    for path in (home / ".codex/AGENTS.md", home / ".claude/CLAUDE.md"):
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if (
            existing.count(MANAGED_BEGIN) != 1
            or _managed_instructions(existing, block) != existing
        ):
            problems.append(f"instruction drift: {path}")
    settings_path = home / ".claude/settings.json"
    if (
        not settings_path.exists()
        or _json_bytes(settings) != settings_path.read_bytes()
    ):
        problems.append(f"hook drift: {settings_path}")
    if problems:
        raise InstallError("install drift detected:\n- " + "\n- ".join(problems))
    print("installed harness is in sync")


def uninstall(home: Path, install_root: Path) -> None:
    payload = install_root / "payload"
    manifest = _load_manifest(install_root / "manifest.json")
    runtime_value = manifest.get("runtime_bin")
    runtime_bin = Path(runtime_value) if isinstance(runtime_value, str) else None
    for path, expected in _targets(home, payload, runtime_bin).items():
        _assert_safe_user_path(home, path.parent)
        if path.is_symlink() and path.resolve(strict=False) == expected.resolve(
            strict=False
        ):
            path.unlink()
        elif _requires_copy(path) and _target_matches(path, expected):
            path.unlink()
        elif os.name == "nt" and path.is_file() and expected.is_file():
            if _sha256(path) == _sha256(expected):
                path.unlink()
        elif (
            os.name == "nt"
            and path.is_dir()
            and expected.is_dir()
            and _target_matches(path, expected)
        ):
            shutil.rmtree(path)
    for path in (home / ".codex/AGENTS.md", home / ".claude/CLAUDE.md"):
        _assert_safe_user_path(home, path)
        if path.exists() and not path.is_symlink():
            existing = path.read_text(encoding="utf-8")
            instruction_update = _without_managed_instructions(existing)
            if instruction_update != existing:
                _write_text(path, instruction_update)
    settings_path = home / ".claude/settings.json"
    _assert_safe_user_path(home, settings_path)
    if settings_path.exists() and not settings_path.is_symlink():
        settings = _load_settings(settings_path)
        managed_commands: set[str] = set()
        if runtime_bin is not None:
            _, guard = _runtime_executables(runtime_bin)
            managed_commands.add(str(guard))
        settings_update = _without_managed_hooks(settings, managed_commands)
        if settings_update != settings:
            _atomic_write(settings_path, _json_bytes(settings_update))
    print(
        "removed managed Codex and Claude Code integrations; "
        "runtime retained for rollback"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--install-root", type=Path)
    parser.add_argument("--runtime-bin-dir", type=Path)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true")
    action.add_argument("--dry-run", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        home = args.home.resolve(strict=True)
        install_root = (args.install_root or home / INSTALL_DIRECTORY).resolve(
            strict=False
        )
        if not _path_is_within(install_root, home):
            raise InstallError(
                "installation refused: install root must stay inside the selected home"
            )
        if args.uninstall:
            uninstall(home, install_root)
            return 0
        manifest = _load_manifest(install_root / "manifest.json")
        runtime_bin = args.runtime_bin_dir
        if runtime_bin is None:
            if args.check and isinstance(manifest.get("runtime_bin"), str):
                runtime_bin = Path(manifest["runtime_bin"])
            else:
                runtime_bin = (
                    install_root / "venv" / ("Scripts" if os.name == "nt" else "bin")
                )
        runtime_bin = runtime_bin.absolute()
        targets = _targets(home, install_root / "payload", runtime_bin)
        _preflight_user_paths(home, targets)
        _preflight_targets(targets)
        if args.check:
            check(home, install_root, runtime_bin)
            return 0
        if args.dry_run and not runtime_bin.exists():
            print(
                "dry-run: runtime would be created and client integrations "
                "installed; no files written"
            )
            return 0
        if args.runtime_bin_dir is None and not args.dry_run:
            runtime_bin = _create_runtime(install_root, args.python)
        install(home, install_root, runtime_bin, args.dry_run)
    except (InstallError, OSError, subprocess.CalledProcessError) as exc:
        print(f"installation failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
