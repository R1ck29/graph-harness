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
# The hook events this installer manages, in the order it writes them.
MANAGED_HOOK_EVENTS = ("SessionStart", "Stop", "SessionEnd", "PostToolUse")

# Events whose entry carries a matcher. Without one the edit hook would run
# after every tool call, paying its cost on reads and searches, and the
# producer would record whatever the client happened to be configured with.
# The producer refuses a tool outside this list as well, so the matcher is a
# cost control rather than the only filter.
MANAGED_HOOK_MATCHERS = {"PostToolUse": "Edit|Write|MultiEdit|NotebookEdit"}
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
    """Parse one client's hook document, naming it if it cannot be merged.

    Two clients are merged now, so an error that names only one sends the
    reader to the wrong file.
    """

    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot safely merge {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InstallError(f"{path} must contain a JSON object")
    return cast("dict[str, Any]", value)


def _without_managed_hooks(
    settings: dict[str, Any], managed_commands: set[str]
) -> dict[str, Any]:
    """Remove only this harness's own hook entries, from every event it uses.

    Everything else in the document is copied through untouched, including
    other events, other entries under the same event, and matcher shapes this
    installer does not produce. An event left with no entries is removed
    rather than left as an empty list, so an install followed by an uninstall
    returns the file to what it was.
    """

    result = cast("dict[str, Any]", json.loads(json.dumps(settings)))
    hooks = result.get("hooks")
    if not isinstance(hooks, dict):
        return result
    for event in MANAGED_HOOK_EVENTS:
        configured = hooks.get(event)
        if not isinstance(configured, list):
            continue
        retained: list[Any] = []
        for matcher in configured:
            if not isinstance(matcher, dict):
                retained.append(matcher)
                continue
            entries = matcher.get("hooks")
            if not isinstance(entries, list):
                retained.append(matcher)
                continue
            filtered = [
                entry
                for entry in entries
                if not _is_managed_hook(entry, managed_commands)
            ]
            if filtered:
                copy = dict(matcher)
                copy["hooks"] = filtered
                retained.append(copy)
        if retained:
            hooks[event] = retained
        else:
            hooks.pop(event, None)
    if not hooks:
        result.pop("hooks", None)
    return result


def _is_managed_hook(entry: Any, managed_commands: set[str]) -> bool:
    """Recognize our own hook entry by the command it runs, and nothing else.

    The command is an absolute path into the runtime this installer built, so
    it identifies the entry on its own. Matching on the timeout and the
    argument list as well was stricter and worse: a person who raised the
    timeout, or added an argument, made our entry unrecognisable, so the next
    install appended a second one and the hook ran twice for every edit.
    Recognising the entry means a repeated install *replaces* it, which is
    also what makes the drift check able to see a hand edit at all.

    Nothing here loosens the boundary around other people's entries. A
    command equal to one of ours is one of ours: it names a path only this
    installer writes.
    """

    if not isinstance(entry, dict):
        return False
    return entry.get("type") == "command" and entry.get("command") in managed_commands


def _managed_entries(
    settings: dict[str, Any], managed_commands: set[str]
) -> dict[tuple[str, str], dict[str, Any]]:
    """Return the managed hook entries already in a settings document.

    Keyed by event and command, so an entry can be compared against the one
    about to replace it. Only entries this installer recognises as its own
    are returned; everything else in the file is none of its business.
    """

    found: dict[tuple[str, str], dict[str, Any]] = {}
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return found
    for event in MANAGED_HOOK_EVENTS:
        configured = hooks.get(event)
        if not isinstance(configured, list):
            continue
        for matcher in configured:
            if not isinstance(matcher, dict):
                continue
            entries = matcher.get("hooks")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not _is_managed_hook(entry, managed_commands):
                    continue
                command = entry.get("command")
                if isinstance(command, str):
                    carried = dict(entry)
                    carried["matcher"] = matcher.get("matcher")
                    found[(event, command)] = carried
    return found


def _managed_shape(
    settings: dict[str, Any], managed_commands: set[str]
) -> dict[str, list[dict[str, Any]]]:
    """Return the managed entries per event, in order, and nothing else.

    This is what the installer owns and therefore all it may call drift.
    Comparing whole documents made an unowned entry's *position* decide the
    answer: a third-party hook sitting after ours changed the rebuilt order,
    because a rebuild always appends ours last, so it read as drift, while
    the identical hook placed before ours read as clean. Position is not a
    difference anyone made on purpose, and a check that goes permanently red
    when another tool appends a hook is a check nobody reads — the same
    failure as comparing serialised bytes, one layer in.

    Entries are kept as a list rather than a set so a duplicated managed
    entry still differs from a single one: two of ours would run the hook
    twice per event, which is the thing the whole managed-entry design
    exists to prevent.
    """

    shape: dict[str, list[dict[str, Any]]] = {}
    hooks = settings.get("hooks")
    for event in MANAGED_HOOK_EVENTS:
        configured = hooks.get(event) if isinstance(hooks, dict) else None
        ours: list[dict[str, Any]] = []
        if isinstance(configured, list):
            for matcher in configured:
                if not isinstance(matcher, dict):
                    continue
                entries = matcher.get("hooks")
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if _is_managed_hook(entry, managed_commands):
                        carried = dict(entry)
                        carried["matcher"] = matcher.get("matcher")
                        ours.append(carried)
        shape[event] = ours
    return shape


def _replacements(
    existing: dict[str, Any], wanted: dict[str, Any], managed_commands: set[str]
) -> list[str]:
    """Name every managed entry that is about to be overwritten with different content.

    A repeated install replaces our entries rather than appending to them,
    which is what keeps the hook from running twice per edit. The cost is
    that a person who raised a timeout or added an argument loses that edit
    silently, and then cannot tell why their change stopped taking effect.
    Saying so at install time is the whole remedy: the edit is still
    reverted, but it is reverted out loud.
    """

    before = _managed_entries(existing, managed_commands)
    after = _managed_entries(wanted, managed_commands)
    changed: list[str] = []
    for key, entry in sorted(before.items()):
        replacement = after.get(key)
        if replacement is not None and replacement != entry:
            event, command = key
            changed.append(f"{event} -> {command}")
    return changed


def _with_managed_hooks(
    settings: dict[str, Any],
    commands: dict[str, Path],
    previous_commands: set[str],
    path: Path,
) -> dict[str, Any]:
    """Install one managed entry per event, replacing any earlier ones.

    Removing before appending is what makes a repeated install idempotent and
    what retires an entry left by a runtime that has since moved.
    """

    wanted = {str(path) for path in commands.values()}
    result = _without_managed_hooks(settings, previous_commands | wanted)
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError(f"{path} 'hooks' must be a JSON object")
    for event, command in commands.items():
        configured = hooks.setdefault(event, [])
        if not isinstance(configured, list):
            raise InstallError(f"{path} 'hooks.{event}' must be a JSON array")
        entry: dict[str, Any] = {
            "hooks": [
                {
                    "type": "command",
                    "command": str(command),
                    "args": [],
                    "timeout": 10,
                }
            ]
        }
        matcher = MANAGED_HOOK_MATCHERS.get(event)
        if matcher is not None:
            entry["matcher"] = matcher
        configured.append(entry)
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


def _hook_commands(bin_directory: Path) -> dict[str, Path]:
    """Map each managed hook event to the executable that serves it.

    ``Stop`` fires once per assistant turn and ``SessionEnd`` once per
    session, so the two are different events rather than two names for the
    same one. Both clients deliver the same payload on standard input, which
    is why one set of executables serves both.
    """

    suffix = ".exe" if os.name == "nt" else ""
    return {
        "SessionStart": bin_directory / f"graphctl-session-start{suffix}",
        "Stop": bin_directory / f"graphctl-claude-stop{suffix}",
        "SessionEnd": bin_directory / f"graphctl-session-end{suffix}",
        "PostToolUse": bin_directory / f"graphctl-edit{suffix}",
    }


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
        graphctl, _ = _runtime_executables(runtime_bin)
        targets[home / ".local/bin/graphctl"] = graphctl
        for command in _hook_commands(runtime_bin).values():
            targets[home / ".local/bin" / command.name] = command
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
        home / ".codex/hooks.json",
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


def _target_matches(target: Path, source: Path, *, platform: str = os.name) -> bool:
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
        (_requires_copy(target) or platform == "nt")
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
) -> tuple[dict[Path, Path], str, dict[str, Any], dict[str, Any]]:
    graphctl, _ = _runtime_executables(runtime_bin)
    commands = _hook_commands(runtime_bin)
    if not all(
        executable.is_file() and os.access(executable, os.X_OK)
        for executable in (graphctl, *commands.values())
    ):
        raise InstallError(f"runtime executables are missing from {runtime_bin}")
    template = (ROOT / "adapters/global-instructions.md").read_text(encoding="utf-8")
    block = template.format(graphctl=graphctl)
    previous_commands: set[str] = set()
    manifest = _load_manifest(install_root / "manifest.json")
    previous_runtime = manifest.get("runtime_bin")
    if isinstance(previous_runtime, str):
        previous_commands.update(
            str(path) for path in _hook_commands(Path(previous_runtime)).values()
        )
    claude_path = home / ".claude/settings.json"
    codex_path = home / ".codex/hooks.json"
    settings = _with_managed_hooks(
        _load_settings(claude_path), commands, previous_commands, claude_path
    )
    codex_hooks = _with_managed_hooks(
        _load_settings(codex_path), commands, previous_commands, codex_path
    )
    return (
        _targets(home, install_root / "payload", runtime_bin),
        block,
        settings,
        codex_hooks,
    )


def install(home: Path, install_root: Path, runtime_bin: Path, dry_run: bool) -> None:
    manifest_path = install_root / "manifest.json"
    _load_manifest(manifest_path)
    targets, block, settings, codex_hooks = _expected_state(
        home, install_root, runtime_bin
    )
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
    codex_hooks_path = home / ".codex/hooks.json"
    instruction_updates: dict[Path, str] = {}
    for path in instruction_paths:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        updated = _managed_instructions(existing, block)
        if updated != existing:
            instruction_updates[path] = updated
    managed_commands = {str(path) for path in _hook_commands(runtime_bin).values()}
    for path, existing_document, wanted_document in (
        (settings_path, _load_settings(settings_path), settings),
        (codex_hooks_path, _load_settings(codex_hooks_path), codex_hooks),
    ):
        for replaced in _replacements(
            existing_document, wanted_document, managed_commands
        ):
            print(f"replacing edited harness hook in {path}: {replaced}")
    rendered_settings = _json_bytes(settings)
    settings_changed = rendered_settings != (
        settings_path.read_bytes() if settings_path.exists() else b""
    )
    # Codex documents the same events and the same payload, and its hook file
    # is written even though the clients measured here parse it without
    # running it: the entries cost nothing while that holds, and doctor
    # reports from journal records whether they have ever fired.
    rendered_codex = _json_bytes(codex_hooks)
    codex_changed = rendered_codex != (
        codex_hooks_path.read_bytes() if codex_hooks_path.exists() else b""
    )
    changed_configs = [*instruction_updates]
    if settings_changed:
        changed_configs.append(settings_path)
    if codex_changed:
        changed_configs.append(codex_hooks_path)
    if changed_configs:
        backup_root = install_root / "backups" / str(time.time_ns())
        for path in changed_configs:
            _backup(path, home, backup_root)
    for path, updated in instruction_updates.items():
        _write_text(path, updated)
    if settings_changed:
        _atomic_write(settings_path, rendered_settings)
    if codex_changed:
        _atomic_write(codex_hooks_path, rendered_codex)
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


def _managed_hook_files(
    home: Path, settings: dict[str, Any], codex_hooks: dict[str, Any]
) -> list[tuple[Path, dict[str, Any]]]:
    """Pair each client's hook file with the content this installer expects."""

    return [
        (home / ".claude/settings.json", settings),
        (home / ".codex/hooks.json", codex_hooks),
    ]


def check(home: Path, install_root: Path, runtime_bin: Path) -> None:
    if not _load_manifest(install_root / "manifest.json"):
        raise InstallError("install drift: manifest is missing")
    targets, block, settings, codex_hooks = _expected_state(
        home, install_root, runtime_bin
    )
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
    managed_commands = {str(path) for path in _hook_commands(runtime_bin).values()}
    for hook_path, document in _managed_hook_files(home, settings, codex_hooks):
        # Compared as the managed entries alone, not as whole documents and
        # not as bytes. Each narrowing closed a false positive: bytes made a
        # client's reformatting look like drift, and whole documents made an
        # unowned hook's *position* look like drift. The installer owns one
        # entry per managed event and nothing else in these files, so that
        # is the only thing it is entitled to call drift.
        if not hook_path.exists():
            problems.append(f"hook drift: {hook_path}")
            continue
        on_disk = _managed_shape(_load_settings(hook_path), managed_commands)
        if on_disk != _managed_shape(document, managed_commands):
            problems.append(f"hook drift: {hook_path}")
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
    managed_commands: set[str] = set()
    if runtime_bin is not None:
        managed_commands.update(
            str(path) for path in _hook_commands(runtime_bin).values()
        )
    for configured in (home / ".claude/settings.json", home / ".codex/hooks.json"):
        _assert_safe_user_path(home, configured)
        if not configured.exists() or configured.is_symlink():
            continue
        existing_hooks = _load_settings(configured)
        hooks_update = _without_managed_hooks(existing_hooks, managed_commands)
        if hooks_update != existing_hooks:
            _atomic_write(configured, _json_bytes(hooks_update))
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
        if args.check:
            check(home, install_root, runtime_bin)
            return 0
        _preflight_targets(targets)
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
