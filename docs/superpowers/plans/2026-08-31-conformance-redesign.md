# Conformance Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **This repository additionally runs its own protocol.** Each task below is one
> node in `task-graph.json`. Do not mark a task done without criterion-linked
> evidence and an independent PASS recorded with `graphctl verify`.

**Goal:** Replace an ambiguous conformance signal with a direct one, and make the harness report whether it is worth its own cost.

**Architecture:** A `PostToolUse` hook records that an editing tool ran, giving direct causal evidence a session changed code. Working-tree digests taken at turn boundaries stay as a second term covering shell edits. `HEAD` movement is demoted from evidence to context, which removes the pull/checkout/rebase false-positive class outright. A new read-only `graphctl effectiveness` folds the graph's own failure history into counts, and two new state transitions let a human's decisions be recorded instead of hand-edited.

**Tech Stack:** Python 3.10+ (`.venv/bin/python` is 3.12.12 here), standard library only, `unittest`, `black`, strict `mypy`.

**Spec:** `docs/superpowers/specs/2026-08-31-conformance-redesign-design.md`

## Global Constraints

- Python floor is **3.10**. The `python3` on PATH is **3.8.5** and must not be used; run everything with `.venv/bin/python`.
- Standard library only. No new dependencies.
- Every journal producer **must never raise into its caller**. `journal.append` returns `False` on `OSError`; callers check the return value and degrade to doing nothing.
- Journal lines are capped at `MAX_JOURNAL_LINE_BYTES = 4096`; over-long records shed variable-length fields rather than fail.
- **No file paths and no file contents may be written to the journal.** Only `path_id`, the first 12 hex characters of the SHA-256 of the repository-relative path.
- `WARN_MIN_FILES = 2`, `WARN_MIN_LINES = 20` are unchanged.
- `schema_version` becomes `2`; readers accept `1` and `2`.
- Nothing blocks a session. Reports are printed at the next `SessionStart` with exit 2; no other exit code changes.
- Run `graphctl` and `scripts/sync_adapters.py` from the repository root.
- Commit messages: `<type>: <description>`. No attribution trailers.

---

## File Structure

| File | Responsibility | Task |
| --- | --- | --- |
| `agent_harness/journal.py` | Record shape; `SCHEMA_VERSION` 2 | 1 |
| `agent_harness/session_hooks.py` | Edit producer, detection rule, sizing, reporting | 1, 2 |
| `agent_harness/worktree.py` | Snapshots; digest comparison only | 2 |
| `agent_harness/conformance.py` | Per-session verdicts, sharing task 2's rule | 2 |
| `agent_harness/effectiveness.py` | **New.** Harness outcome counts | 5 |
| `agent_harness/graph.py` | `grant_attempt`, `supersede` transitions | 4 |
| `agent_harness/cli.py` | `effectiveness`, `grant-attempt`, `supersede` commands | 4, 5 |
| `scripts/install_pc.py` | Manage the `PostToolUse` entry | 3 |
| `pyproject.toml` | `graphctl-edit` console entry point | 1 |
| `tests/test_session_hook_contract.py` | Producer and detection contracts | 1, 2 |
| `tests/test_conformance_report_contract.py` | Report contracts | 2 |
| `tests/test_effectiveness_contract.py` | **New.** Outcome-count contracts | 5 |
| `tests/test_graph_contract.py` | Transition contracts | 4 |
| `tests/test_pc_install_contract.py` | Installer contracts | 3 |
| `docs/conformance.md`, `docs/how-it-works.md`, `docs/security.md`, `docs/platform-compatibility.md`, `core/protocol.md`, `core/state-machine.md`, `roles/` | Documentation | 6 |

---

### Task 1: The edit producer

**Node:** `edit-events`

**Files:**
- Modify: `agent_harness/journal.py:26` (`SCHEMA_VERSION = 1` → `2`)
- Modify: `agent_harness/session_hooks.py` (add `path_id`, `record_edit`, `edit_entrypoint`)
- Modify: `pyproject.toml` (`graphctl-edit` console script)
- Test: `tests/test_session_hook_contract.py`

**Interfaces:**
- Consumes: `journal.record`, `journal.append`, `session_hooks.read_payload`, `session_hooks.client_name`, `session_hooks.repository`
- Produces:
  - `session_hooks.path_id(repo: Path, path: str) -> str` — 12 lowercase hex chars
  - `session_hooks.record_edit() -> int` — always returns 0
  - `session_hooks.edit_entrypoint() -> int` — console entry point, enforces the interpreter floor first

- [ ] **Step 1: Write the failing tests**

```python
    def test_an_edit_records_a_hashed_path_and_never_the_path(self) -> None:
        payload = json.loads(self._payload(session_id="E1"))
        payload["tool_name"] = "Edit"
        payload["tool_input"] = {"file_path": str(Path(self.repo) / "secret/customer.md")}

        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            self.assertEqual(0, session_hooks.record_edit())

        records = [e for e in journal.read(self.home) if e.get("event") == "edit"]
        self.assertEqual(1, len(records))
        self.assertEqual("Edit", records[0]["tool"])
        self.assertEqual("E1", records[0]["session_id"])
        self.assertRegex(records[0]["path_id"], r"^[0-9a-f]{12}$")
        raw = (journal.journal_directory(self.home)).read_text() if False else "".join(
            json.dumps(e) for e in journal.read(self.home)
        )
        self.assertNotIn("customer.md", raw)
        self.assertNotIn("secret", raw)

    def test_the_same_file_hashes_the_same_and_a_different_one_does_not(self) -> None:
        first = session_hooks.path_id(Path(self.repo), str(Path(self.repo) / "a.py"))
        again = session_hooks.path_id(Path(self.repo), str(Path(self.repo) / "a.py"))
        other = session_hooks.path_id(Path(self.repo), str(Path(self.repo) / "b.py"))

        self.assertEqual(first, again)
        self.assertNotEqual(first, other)

    def test_a_path_outside_the_repository_is_still_recorded_without_the_path(
        self,
    ) -> None:
        payload = json.loads(self._payload(session_id="E2"))
        payload["tool_name"] = "Write"
        payload["tool_input"] = {"file_path": "/etc/hosts"}

        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            self.assertEqual(0, session_hooks.record_edit())

        records = [e for e in journal.read(self.home) if e.get("event") == "edit"]
        self.assertEqual(1, len(records))
        self.assertRegex(records[0]["path_id"], r"^[0-9a-f]{12}$")

    def test_an_edit_that_cannot_be_recorded_still_exits_zero(self) -> None:
        payload = json.loads(self._payload(session_id="E3"))
        payload["tool_name"] = "Edit"
        payload["tool_input"] = {"file_path": str(Path(self.repo) / "a.py")}

        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            with mock.patch.object(journal, "append", return_value=False):
                self.assertEqual(0, session_hooks.record_edit())

    def test_an_unusable_edit_payload_records_nothing_and_exits_zero(self) -> None:
        for raw in ("", "not json", "[]", "{}"):
            with mock.patch("sys.stdin", io.StringIO(raw)):
                self.assertEqual(0, session_hooks.record_edit())

        self.assertEqual(
            [], [e for e in journal.read(self.home) if e.get("event") == "edit"]
        )
```

- [ ] **Step 2: Run them and verify they fail**

Run: `.venv/bin/python -m unittest tests.test_session_hook_contract -k edit`
Expected: FAIL with `AttributeError: module 'agent_harness.session_hooks' has no attribute 'record_edit'`

- [ ] **Step 3: Implement**

In `agent_harness/journal.py`, change `SCHEMA_VERSION = 1` to `SCHEMA_VERSION = 2`.

In `agent_harness/session_hooks.py`:

```python
import hashlib

# Only the hash of a path is recorded. A path is content enough: a filename
# can name a customer, and docs/security.md forbids recording user content.
# Twelve hex characters distinguish the files one session touches without
# carrying anything back that could identify them.
PATH_ID_CHARACTERS = 12


def path_id(repo: Path, path: str) -> str:
    """Return a stable, non-reversible identifier for one edited path."""

    try:
        relative = str(Path(path).resolve().relative_to(Path(repo).resolve()))
    except (OSError, ValueError):
        # A path outside the repository still counts as one distinct file;
        # it is hashed whole rather than dropped.
        relative = str(path)
    digest = hashlib.sha256(relative.encode("utf-8", "replace")).hexdigest()
    return digest[:PATH_ID_CHARACTERS]


def record_edit() -> int:
    """Record that an editing tool ran. Always returns 0.

    This is the cheapest producer in the system: read stdin, hash one string,
    append one line. It takes no snapshot and calls no git, because it runs
    after every edit rather than once per turn.
    """

    payload = read_payload(sys.stdin)
    if payload is None:
        return 0
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or not tool:
        return 0
    target = payload.get("tool_input")
    named = target.get("file_path") if isinstance(target, dict) else None
    if not isinstance(named, str) or not named:
        return 0
    try:
        root = repository(payload)
        journal.append(
            journal.record(
                "edit",
                client=client_name(),
                session_id=payload.get("session_id"),
                repo=str(root),
                tool=tool,
                path_id=path_id(root, named),
            )
        )
    except Exception:  # noqa: BLE001 - a lost record must not break an edit
        return 0
    return 0


def edit_entrypoint() -> int:
    """Console entry point for the post-edit hook."""

    from .claude_hook import ensure_supported_python

    ensure_supported_python(sys.version_info[:3])
    return record_edit()
```

In `pyproject.toml`, beside the existing console scripts:

```toml
graphctl-edit = "agent_harness.session_hooks:edit_entrypoint"
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `.venv/bin/python -m unittest tests.test_session_hook_contract`
Expected: PASS, no regressions in the other 44 tests.

- [ ] **Step 5: Measure the cost and record it as evidence**

Run: `time (for i in $(seq 1 10); do echo '{}' | .venv/bin/python -c "from agent_harness.session_hooks import record_edit; record_edit()"; done)`
Expected: under 60 ms per invocation. Record the number.

- [ ] **Step 6: Submit, review, verify**

```bash
graphctl submit edit-events --actor-id <you> \
  --relevant-file agent_harness/session_hooks.py \
  --relevant-file agent_harness/journal.py \
  --relevant-file pyproject.toml \
  --evidence '{"criterion":"...","kind":"test","summary":"..."}'
graphctl review-packet edit-events   # hand to graph-reviewer
graphctl verify edit-events --pass --reviewer-id <reviewer> --criterion ... --evidence ...
```

- [ ] **Step 7: Commit**

```bash
git add agent_harness/session_hooks.py agent_harness/journal.py pyproject.toml tests/test_session_hook_contract.py
git commit -m "feat: record editing tool calls without recording paths"
```

---

### Task 2: The detection rule

**Node:** `bypass-signal`

**Files:**
- Modify: `agent_harness/worktree.py` (delete `committed_size` and `COMMIT_NAME`; `changed` becomes digest-only)
- Modify: `agent_harness/session_hooks.py` (`edited`, `session_size`, `worth_reporting`, `previous_bypass`)
- Modify: `agent_harness/conformance.py` (`_judge` shares the same rule)
- Test: `tests/test_session_hook_contract.py`, `tests/test_conformance_report_contract.py`

**Interfaces:**
- Consumes: `session_hooks.path_id` and the `edit` event from Task 1
- Produces:
  - `worktree.dirty_changed(before: Any, after: Any) -> bool` — digest comparison only; replaces `changed`
  - `session_hooks.edited(records: list[dict]) -> bool`
  - `session_hooks.session_size(records: list[dict]) -> tuple[int, int]`

- [ ] **Step 1: Write the failing tests**

```python
    def _clone_with_upstream(self) -> tuple[Path, Path]:
        base = Path(tempfile.mkdtemp(dir=self._directory.name))
        upstream, clone = base / "upstream", base / "clone"
        upstream.mkdir()
        self._git(upstream, "init", "-q")
        self._git(upstream, "config", "user.email", "up@example.com")
        self._git(upstream, "config", "user.name", "Up")
        (upstream / "seed.txt").write_text("seed\n")
        self._git(upstream, "add", "-A")
        self._git(upstream, "commit", "-qm", "seed")
        subprocess.run(
            ["git", "clone", "-q", str(upstream), str(clone)],
            check=True,
            capture_output=True,
        )
        self._git(clone, "config", "user.email", "me@example.com")
        self._git(clone, "config", "user.name", "Me")
        return upstream, clone

    def test_a_pull_is_never_reported(self) -> None:
        upstream, clone = self._clone_with_upstream()
        self._open_session("puller", clone)
        for name in ("a.txt", "b.txt"):
            (upstream / name).write_text("".join(f"line {i}\n" for i in range(40)))
        self._git(upstream, "add", "-A")
        self._git(upstream, "commit", "-qm", "upstream work")
        self._git(clone, "pull", "-q", "--ff-only")
        self._close_session("puller", clone)

        self.assertIsNone(session_hooks.previous_bypass(repository_key(clone)))

    def test_a_checkout_is_never_reported(self) -> None:
        _, clone = self._clone_with_upstream()
        self._git(clone, "checkout", "-q", "-b", "feature")
        for name in ("a.txt", "b.txt"):
            (clone / name).write_text("".join(f"line {i}\n" for i in range(40)))
        self._git(clone, "add", "-A")
        self._git(clone, "commit", "-qm", "feature work")
        self._git(clone, "checkout", "-q", "master")
        self._open_session("checker", clone)
        self._git(clone, "checkout", "-q", "feature")
        self._close_session("checker", clone)

        self.assertIsNone(session_hooks.previous_bypass(repository_key(clone)))

    def test_a_hard_reset_is_never_reported(self) -> None:
        _, clone = self._clone_with_upstream()
        for name in ("a.txt", "b.txt"):
            (clone / name).write_text("".join(f"line {i}\n" for i in range(40)))
        self._git(clone, "add", "-A")
        self._git(clone, "commit", "-qm", "local work")
        self._open_session("resetter", clone)
        self._git(clone, "reset", "-q", "--hard", "HEAD~1")
        self._close_session("resetter", clone)

        self.assertIsNone(session_hooks.previous_bypass(repository_key(clone)))

    def test_a_commit_by_someone_else_mid_session_is_never_reported(self) -> None:
        _, clone = self._clone_with_upstream()
        self._open_session("bystander", clone)
        self._git(clone, "config", "user.email", "other@example.com")
        for name in ("a.txt", "b.txt"):
            (clone / name).write_text("".join(f"line {i}\n" for i in range(40)))
        self._git(clone, "add", "-A")
        self._git(clone, "commit", "-qm", "someone else")
        self._close_session("bystander", clone)

        self.assertIsNone(session_hooks.previous_bypass(repository_key(clone)))

    def test_an_edit_tool_call_is_reported(self) -> None:
        _, clone = self._clone_with_upstream()
        self._open_session("editor", clone)
        self._record_edits("editor", clone, "a.py", "b.py")
        self._close_session("editor", clone)

        found = session_hooks.previous_bypass(repository_key(clone))

        self.assertIsNotNone(found)
        self.assertEqual("editor", found["session_id"])
        self.assertEqual(2, found["changed_files"])

    def test_a_shell_edit_across_two_turns_is_reported(self) -> None:
        _, clone = self._clone_with_upstream()
        self._open_session("sheller", clone)
        for name in ("a.txt", "b.txt"):
            (clone / name).write_text("".join(f"line {i}\n" for i in range(40)))
        self._turn_end("sheller", clone)
        self._close_session("sheller", clone)

        self.assertIsNotNone(session_hooks.previous_bypass(repository_key(clone)))

    def test_a_shell_edit_committed_inside_one_turn_is_the_known_blind_spot(
        self,
    ) -> None:
        # Documented in docs/conformance.md. Pinned so it cannot change
        # unnoticed: this errs towards silence, never towards accusation.
        _, clone = self._clone_with_upstream()
        self._open_session("hidden", clone)
        for name in ("a.txt", "b.txt"):
            (clone / name).write_text("".join(f"line {i}\n" for i in range(40)))
        self._git(clone, "add", "-A")
        self._git(clone, "commit", "-qm", "shell work")
        self._close_session("hidden", clone)

        self.assertIsNone(session_hooks.previous_bypass(repository_key(clone)))

    def test_committed_size_is_gone(self) -> None:
        self.assertFalse(hasattr(worktree, "committed_size"))
```

- [ ] **Step 2: Run them and verify they fail**

Run: `.venv/bin/python -m unittest tests.test_session_hook_contract -k "pull or checkout or reset or blind or edit_tool"`
Expected: the pull, checkout, reset and bystander tests FAIL by reporting a bypass; `test_committed_size_is_gone` FAILs.

- [ ] **Step 3: Implement**

In `agent_harness/worktree.py`, delete `committed_size` and `COMMIT_NAME`, and replace `changed`:

```python
def dirty_changed(before: Any, after: Any) -> bool:
    """Report whether the working tree itself moved between two snapshots.

    ``HEAD`` is deliberately not compared. A head move says something changed
    but never who changed it or why: authoring, pulling, checking out,
    rebasing and a colleague's commit in another terminal are one observation.
    Judging on it accused anyone who ran ``git pull``. Work the session did is
    seen instead through an edit event or through a dirty tree at some turn
    boundary.
    """

    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    if not before.get("git") or not after.get("git"):
        return False
    return bool(before.get("digest") != after.get("digest"))
```

In `agent_harness/session_hooks.py`:

```python
def edited(records: list[dict[str, Any]]) -> bool:
    """Report whether one session's records show it changed code.

    Two terms, both required for coverage. An edit event is direct causal
    evidence that this session's agent wrote a file. A dirty tree that differs
    between two consecutive turn boundaries covers edits made through the
    shell, which is how a session that ignores the protocol is likely to edit.
    """

    if any(record.get("event") == "edit" for record in records):
        return True
    snapshots = [
        record.get("snapshot")
        for record in records
        if record.get("event") in {"session_open", "turn_end", "session_close"}
    ]
    return any(
        worktree.dirty_changed(before, after)
        for before, after in zip(snapshots, snapshots[1:])
    )


def session_size(records: list[dict[str, Any]]) -> tuple[int, int]:
    """Return how much one session changed, as a magnitude only.

    The maximum across turn boundaries rather than the difference between the
    two ends: a session that edits and then commits returns the tree to clean,
    and comparing only the ends would size that work at zero.
    """

    distinct = {
        record.get("path_id")
        for record in records
        if record.get("event") == "edit" and isinstance(record.get("path_id"), str)
    }
    snapshots = [
        record.get("snapshot")
        for record in records
        if isinstance(record.get("snapshot"), dict)
    ]
    if not snapshots:
        return (len(distinct), 0)
    first = snapshots[0]
    files = max(
        (_count(shot.get("files")) - _count(first.get("files")) for shot in snapshots),
        default=0,
    )
    lines = max(
        (_count(shot.get("lines")) - _count(first.get("lines")) for shot in snapshots),
        default=0,
    )
    return (max(len(distinct), max(files, 0)), max(lines, 0))
```

`worth_reporting` becomes a threshold check over `session_size`, and
`previous_bypass` gathers each candidate's records rather than only its two
boundary records. `conformance._judge` calls the same `edited` and
`session_size`, so the report and the hook cannot disagree.

- [ ] **Step 4: Run the tests and verify they pass**

Run: `.venv/bin/python -m unittest discover`
Expected: all tests pass. The old `test_a_pull_sized_head_move_is_not_reported` and `test_a_forged_head_is_never_passed_to_git` are deleted along with the code they covered.

- [ ] **Step 5: Reproduce the original defect and confirm it is gone**

Run the round-3 reproduction from the spec (real upstream, real clone, `git pull --ff-only` of 2 files / 80 lines) and confirm the next session start exits 0 and prints nothing.

- [ ] **Step 6: Submit, review, verify** — as Task 1.

- [ ] **Step 7: Commit**

```bash
git add agent_harness/worktree.py agent_harness/session_hooks.py agent_harness/conformance.py tests/
git commit -m "fix: stop treating a head move as evidence of work"
```

---

### Task 3: The installer manages the edit hook

**Node:** `installer-hook`

**Files:**
- Modify: `scripts/install_pc.py:35` (`MANAGED_HOOK_EVENTS`), `_expected_state`
- Modify: `agent_harness/cli.py` (doctor reports edit-hook liveness)
- Test: `tests/test_pc_install_contract.py`, `tests/test_conformance_contract.py`

**Interfaces:**
- Consumes: `session_hooks.edit_entrypoint` from Task 1, exposed as `graphctl-edit`
- Produces: a `PostToolUse` entry with `matcher: "Edit|Write|MultiEdit|NotebookEdit"`

- [ ] **Step 1: Write the failing tests**

```python
    def test_the_edit_hook_is_installed_with_its_matcher(self) -> None:
        install_pc.main(["--home", str(self.home)])

        settings = json.loads((self.home / ".claude/settings.json").read_text())
        entries = settings["hooks"]["PostToolUse"]
        managed = [e for e in entries if "graphctl-edit" in json.dumps(e)]
        self.assertEqual(1, len(managed))
        self.assertEqual("Edit|Write|MultiEdit|NotebookEdit", managed[0]["matcher"])

    def test_an_unrelated_post_tool_use_entry_is_left_alone(self) -> None:
        path = self.home / ".claude/settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"hooks": {"PostToolUse": [
                    {"matcher": "Edit|Write",
                     "hooks": [{"type": "command", "command": "other-tool"}]}
                ]}}
            )
        )

        install_pc.main(["--home", str(self.home)])

        entries = json.loads(path.read_text())["hooks"]["PostToolUse"]
        self.assertIn("other-tool", json.dumps(entries))

    def test_installing_twice_reports_no_drift(self) -> None:
        install_pc.main(["--home", str(self.home)])
        install_pc.main(["--home", str(self.home)])

        self.assertEqual(0, install_pc.main(["--home", str(self.home), "--check"]))
```

- [ ] **Step 2: Run them and verify they fail**

Run: `.venv/bin/python -m unittest tests.test_pc_install_contract -k edit`
Expected: FAIL with `KeyError: 'PostToolUse'`

- [ ] **Step 3: Implement**

```python
MANAGED_HOOK_EVENTS = ("SessionStart", "Stop", "SessionEnd", "PostToolUse")

MANAGED_HOOK_MATCHERS = {"PostToolUse": "Edit|Write|MultiEdit|NotebookEdit"}
```

`_with_managed_hooks` writes `matcher` from `MANAGED_HOOK_MATCHERS` when the
event has one, and `_without_managed_hooks` keeps removing only entries whose
command is one of ours, so a third-party `PostToolUse` entry survives.

`doctor` gains `edit_hook_last_seen` next to the existing per-client
observation keys, read from `edit` events, and reports the interpreter behind
the configured command.

- [ ] **Step 4: Run the tests and verify they pass**

Run: `.venv/bin/python -m unittest tests.test_pc_install_contract tests.test_conformance_contract`
Expected: PASS.

- [ ] **Step 5: Verify against the real machine, read-only**

Run: `.venv/bin/python scripts/install_pc.py --check`
Expected: it reports the missing `PostToolUse` entry as drift and changes nothing.

- [ ] **Step 6: Submit, review, verify** — as Task 1.

- [ ] **Step 7: Commit**

```bash
git add scripts/install_pc.py agent_harness/cli.py tests/
git commit -m "feat: install and diagnose the edit hook on both clients"
```

---

### Task 4: The two escape hatches

**Node:** `escape-hatches`

**Files:**
- Modify: `agent_harness/graph.py` (`grant_attempt`, `supersede`)
- Modify: `agent_harness/cli.py` (`grant-attempt`, `supersede` subcommands)
- Test: `tests/test_graph_contract.py`, `tests/test_storage_and_cli_contract.py`

**Interfaces:**
- Produces:
  - `Graph.grant_attempt(node_id: str, granted_by: str, reason: str) -> int` — returns the new `max_attempts`
  - `Graph.supersede(node_id: str, reason: str) -> list[str]` — returns the ids released

- [ ] **Step 1: Write the failing tests**

```python
    def test_a_granted_attempt_raises_the_ceiling_and_keeps_the_failures(self) -> None:
        task_graph = Graph.from_dict(graph(node("fix", max_attempts=2)))
        self._fail_twice(task_graph, "fix")

        with self.assertRaisesRegex(HarnessError, "max_attempts"):
            task_graph.retry("fix")

        ceiling = task_graph.grant_attempt("fix", granted_by="rick", reason="design")

        self.assertEqual(3, ceiling)
        self.assertEqual(2, task_graph.node("fix")["attempts"])
        self.assertEqual(2, len(task_graph.node("fix")["failure_history"]))
        self.assertEqual("ready", task_graph.retry("fix"))
        granted = task_graph.node("fix")["granted_attempts"]
        self.assertEqual([{"granted_by": "rick", "reason": "design", "at": mock.ANY}], granted)

    def test_a_grant_is_refused_on_a_node_that_has_not_failed(self) -> None:
        task_graph = Graph.from_dict(graph(node("fix")))

        with self.assertRaisesRegex(HarnessError, "only a failed node"):
            task_graph.grant_attempt("fix", granted_by="rick", reason="because")

    def test_a_grant_requires_a_grantor_and_a_reason(self) -> None:
        task_graph = Graph.from_dict(graph(node("fix", max_attempts=1)))
        self._fail_once(task_graph, "fix")

        for grantor, reason in (("", "r"), ("rick", "")):
            with self.assertRaises(HarnessError):
                task_graph.grant_attempt("fix", granted_by=grantor, reason=reason)

    def test_superseding_releases_descendants_and_keeps_the_history(self) -> None:
        task_graph = Graph.from_dict(
            graph(node("old"), node("next", depends_on=["old"]))
        )
        self._fail_once(task_graph, "old")

        released = task_graph.supersede("old", reason="approach abandoned")

        self.assertEqual(["next"], released)
        self.assertEqual("superseded", task_graph.node("old")["status"])
        self.assertEqual(1, len(task_graph.node("old")["failure_history"]))
        self.assertEqual(["next"], task_graph.ready())

    def test_a_superseded_node_does_not_block_completion(self) -> None:
        task_graph = Graph.from_dict(
            graph(node("old"), node("next", depends_on=["old"]))
        )
        self._fail_once(task_graph, "old")
        task_graph.supersede("old", reason="approach abandoned")
        self._verify(task_graph, "next")

        self.assertTrue(task_graph.completion_check()["complete"])
```

- [ ] **Step 2: Run them and verify they fail**

Run: `.venv/bin/python -m unittest tests.test_graph_contract -k "grant or supersede"`
Expected: FAIL with `AttributeError: 'Graph' object has no attribute 'grant_attempt'`

- [ ] **Step 3: Implement**

```python
    def grant_attempt(self, node_id: str, granted_by: str, reason: str) -> int:
        """Record that a person granted one more attempt, and raise the ceiling.

        The protocol says to stop and escalate when the budget runs out. When
        the answer comes back "take one more", there has to be a way to say so
        that is not editing the graph by hand. `attempts` is never touched:
        the failures that spent the budget stay on the record and stay
        counted, and the grant sits beside them with the name of whoever gave
        it.

        An agent must not call this to unblock itself. It carries a grantor
        because the grantor is the point.
        """

        node = self.node(node_id)
        self._require_non_empty(granted_by, "granted_by")
        self._require_non_empty(reason, "reason")
        if node["status"] != "failed":
            raise HarnessError(f"only a failed node can be granted an attempt: {node_id}")
        if node["max_attempts"] >= MAX_ATTEMPTS:
            raise HarnessError(f"node {node_id} is at the absolute attempt ceiling")
        node["max_attempts"] += 1
        node.setdefault("granted_attempts", []).append(
            {"granted_by": granted_by, "reason": reason, "at": utc_now()}
        )
        self.validate()
        return cast(int, node["max_attempts"])

    def supersede(self, node_id: str, reason: str) -> list[str]:
        """Retire a node whose approach was abandoned, releasing its dependents.

        Without this, changing approach means rebuilding the whole graph, and
        a rebuild silently resets every attempt budget in it — the exact
        laundering the budget exists to prevent. Retiring one node keeps every
        other verdict and every failure record where it is.
        """

        node = self.node(node_id)
        if node["status"] == "verified":
            raise HarnessError(f"cannot supersede a verified node: {node_id}")
        self._require_non_empty(reason, "reason")
        node["status"] = "superseded"
        node["superseded_reason"] = reason
        node["superseded_at"] = utc_now()
        released = self._release_dependents(node_id)
        self.validate()
        return released
```

`STATUSES` gains `superseded`. Readiness treats a `superseded` dependency as
satisfied; `completion_check` ignores superseded nodes and names them in its
output so they cannot be mistaken for verified.

The CLI adds both subcommands, each requiring its flags, and both are recorded
as `graph_transition` events like every other mutating command.

- [ ] **Step 4: Run the tests and verify they pass**

Run: `.venv/bin/python -m unittest discover`
Expected: PASS.

- [ ] **Step 5: Submit, review, verify** — as Task 1.

- [ ] **Step 6: Commit**

```bash
git add agent_harness/graph.py agent_harness/cli.py tests/
git commit -m "feat: record granted attempts and superseded nodes"
```

---

### Task 5: `graphctl effectiveness`

**Node:** `effectiveness`

**Files:**
- Create: `agent_harness/effectiveness.py`
- Create: `tests/test_effectiveness_contract.py`
- Modify: `agent_harness/cli.py` (the `effectiveness` subcommand)

**Interfaces:**
- Consumes: `conformance.report` from Task 2, `journal.read`, `Graph.from_dict`
- Produces: `effectiveness.report(graphs: list[Path], home=None) -> dict[str, Any]` with keys `schema_version`, `reviews`, `nodes`, `escalations`, `conformance`

- [ ] **Step 1: Write the failing tests**

```python
    def test_a_review_failure_after_test_evidence_is_counted_separately(self) -> None:
        # The harness's whole claim is that an independent reviewer with
        # explicit criteria catches what a green suite does not. That number
        # is this one.
        document = graph(
            node("caught", status="failed", evidence=[{"kind": "test", "summary": "247 pass"}],
                 verification={"result": "fail", "reviewer_id": "r", "evidence": []}),
            node("plain", status="failed", evidence=[{"kind": "observation", "summary": "looked"}],
                 verification={"result": "fail", "reviewer_id": "r", "evidence": []}),
        )

        found = effectiveness.report_from_documents([document])

        self.assertEqual(2, found["reviews"]["failed"])
        self.assertEqual(1, found["reviews"]["failed_despite_test_evidence"])

    def test_the_first_pass_rate_counts_only_verified_nodes(self) -> None:
        document = graph(
            node("a", status="verified", attempts=1),
            node("b", status="verified", attempts=3),
            node("c", status="failed", attempts=2),
        )

        found = effectiveness.report_from_documents([document])

        self.assertEqual(2, found["nodes"]["verified"])
        self.assertEqual(1, found["nodes"]["verified_first_attempt"])
        self.assertEqual(0.5, found["nodes"]["first_pass_rate"])

    def test_an_exhausted_budget_is_reported_as_an_escalation(self) -> None:
        document = graph(node("stuck", status="failed", attempts=2, max_attempts=2))

        found = effectiveness.report_from_documents([document])

        self.assertEqual(1, found["escalations"]["budget_exhausted"])

    def test_the_same_node_in_two_archives_is_counted_once(self) -> None:
        early = graph(node("a", status="failed", attempts=1))
        late = graph(node("a", status="verified", attempts=2))

        found = effectiveness.report_from_documents([early, late])

        self.assertEqual(1, found["nodes"]["total"])
        self.assertEqual(1, found["nodes"]["verified"])

    def test_the_report_writes_nothing(self) -> None:
        before = self._tree_state()

        effectiveness.report([Path(self.graph_path)], home=self.home)

        self.assertEqual(before, self._tree_state())
```

- [ ] **Step 2: Run them and verify they fail**

Run: `.venv/bin/python -m unittest tests.test_effectiveness_contract`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_harness.effectiveness'`

- [ ] **Step 3: Implement**

`agent_harness/effectiveness.py` reads each graph document, keys every node by
`(objective, node_id)` and keeps the record with the highest `attempts` so an
archive and its successor are one node, then counts:

- `reviews.failed`, `reviews.passed`, and `reviews.failed_despite_test_evidence`
  — a FAIL whose submitted evidence held a `kind: test` item
- `nodes.total`, `nodes.verified`, `nodes.verified_first_attempt`,
  `nodes.first_pass_rate`, `nodes.attempt_histogram`
- `escalations.budget_exhausted`, `escalations.granted_attempts`,
  `escalations.superseded`
- `conformance` — `conformance.report(home)`'s counts and rate

Denominators are reported beside every rate. With three objectives on record
these are anecdotes, and a reader must be able to see that.

- [ ] **Step 4: Run the tests and verify they pass**

Run: `.venv/bin/python -m unittest discover`
Expected: PASS.

- [ ] **Step 5: Run it on this repository's real history**

Run: `graphctl effectiveness --graph task-graph.json --graph task-graph.superseded-2026-08-31.json --graph task-graph.completed-2026-08-27.json`
Expected: `reviews.failed_despite_test_evidence >= 1`, which is the 2026-08-31 pull defect. Record the output as evidence.

- [ ] **Step 6: Submit, review, verify** — as Task 1.

- [ ] **Step 7: Commit**

```bash
git add agent_harness/effectiveness.py agent_harness/cli.py tests/test_effectiveness_contract.py
git commit -m "feat: report what the harness itself caught"
```

---

### Task 6: Documentation

**Node:** `docs`

**Files:**
- Modify: `docs/conformance.md` (lines 84-86 are false and must go), `docs/how-it-works.md`, `docs/security.md`, `docs/platform-compatibility.md`
- Modify: `core/protocol.md`, `core/state-machine.md`, `roles/`
- Test: `tests/test_documentation_contract.py`

- [ ] **Step 1: Write the failing tests**

```python
    def test_no_document_claims_commit_sizing_distinguishes_a_pull(self) -> None:
        for path in Path("docs").rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("committed_size", text, path)
            self.assertNotIn("keeps a pull", text, path)

    def test_the_blind_spot_is_documented(self) -> None:
        text = Path("docs/conformance.md").read_text(encoding="utf-8")

        self.assertIn("single turn", text)
        self.assertIn("blind spot", text)

    def test_the_escape_hatches_state_who_may_use_them(self) -> None:
        text = Path("core/protocol.md").read_text(encoding="utf-8")

        self.assertIn("grant-attempt", text)
        self.assertIn("supersede", text)
        self.assertIn("explicit human instruction", text)
```

- [ ] **Step 2: Run them and verify they fail**

Run: `.venv/bin/python -m unittest tests.test_documentation_contract`
Expected: FAIL — `docs/conformance.md` still contains `committed_size`.

- [ ] **Step 3: Write the documentation**

`docs/conformance.md`: replace the commit-sizing section with the two-term
detection rule; add a "known blind spot" section naming the shell-edit-plus-
commit-inside-one-turn case and why silence is the chosen failure. State that
`HEAD` is recorded as context and never used as evidence.

`docs/how-it-works.md`: explain, without assuming engineering knowledge, what
the harness observes, what it cannot observe, and what `effectiveness` is for.

`docs/security.md`: state that the edit hook records a hash of a path and never
a path or its contents.

`docs/platform-compatibility.md`: record the `PostToolUse` measurement and the
40.4 ms cost, and keep the Codex hook result with its reproduction.

`core/protocol.md` and `core/state-machine.md`: add `superseded` to the state
machine, and state that `grant-attempt` and `supersede` require an explicit
human instruction and record who gave it.

- [ ] **Step 4: Run the tests and verify they pass**

Run: `.venv/bin/python -m unittest discover` and `.venv/bin/python scripts/sync_adapters.py --check`
Expected: PASS, no adapter drift.

- [ ] **Step 5: Submit, review, verify** — as Task 1.

- [ ] **Step 6: Commit**

```bash
git add docs/ core/ roles/ .claude/skills/ .agents/skills/ tests/test_documentation_contract.py
git commit -m "docs: describe the signal the code actually uses"
```

---

### Task 7: The gate

**Node:** `gate`

**Files:** none created; this task runs the full local verification.

- [ ] **Step 1: Run every check**

```bash
.venv/bin/python -m unittest discover -v
.venv/bin/python -m black --check agent_harness scripts tests
.venv/bin/python -m mypy --strict agent_harness
.venv/bin/python scripts/sync_adapters.py --check
.venv/bin/python -m compileall -q agent_harness scripts
.venv/bin/python scripts/install_pc.py --check
graphctl conformance
graphctl effectiveness --graph task-graph.json
graphctl completion-check
```

- [ ] **Step 2: Demonstrate the objective end to end**

Construct a bypass session in a scratch repository and confirm `graphctl
conformance` reports it; run a `git pull` session and confirm it is reported as
`read_only` rather than `bypass`; confirm no journal failure can make any
`graphctl` command fail.

- [ ] **Step 3: Submit, review, verify** — as Task 1.

- [ ] **Step 4: Commit**

```bash
git commit --allow-empty -m "chore: record the verification gate"
```

---

## Self-Review

**Spec coverage:** Part 1 → Tasks 1, 2, 3. Part 2 → Task 5. Part 3 → Task 4.
Data-model changes → Tasks 1, 3, 4. Testing requirements → every task's step 1;
the pull, checkout, reset, second-identity, `Edit`, shell-across-turns and
blind-spot cases are all in Task 2, and the edit-hook privacy cases in Task 1.
Migration → performed before Task 1 (archive `task-graph.json`, build the new
graph). Risks → the hook cost is measured in Task 1 step 5; the blind spot is
pinned in Task 2; Codex stays unobserved with no task, as designed.

**Placeholder scan:** none. Every code step carries the code; every test step
carries the test; every run step carries the command and the expected result.

**Type consistency:** `path_id` returns `str` and is used as `str` in Task 2's
`session_size`. `dirty_changed` replaces `changed` and is called only from
`edited` and `conformance._judge`. `grant_attempt` returns `int`, `supersede`
returns `list[str]`, and both are used that way in Task 5's counts.
`_count` already exists in `worktree.py` and is imported by `session_hooks` in
Task 2 rather than redefined.
