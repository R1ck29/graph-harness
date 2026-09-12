# Session conformance observation — design

Date: 2026-08-30
Status: approved for implementation

## Problem

The harness cannot answer whether its protocol was followed in a given session.
Enforcement today is four uneven layers:

| Layer | Mechanism | Guarantee |
| --- | --- | --- |
| Instruction files | Prose in `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md` | None. A model may ignore it. |
| `graphctl` state machine | `graph.py` rejects dependent reviewers, missing evidence, cycles | Strong, but only for work routed through the CLI. |
| Claude Stop hook | `graphctl-claude-stop` runs `completion_check` | Partial. |
| `install_pc.py --check` | Install drift | Manual, and not run by CI. |

Four gaps follow.

1. **Silent bypass.** `claude_hook.py` returns `0` when `task-graph.json` is
   absent. A session that rewrites code without ever creating a graph leaves no
   trace and triggers no warning. The most likely deviation is the one the
   harness cannot see.
2. **No session record.** The graph stores nodes, not sessions. "How many of my
   last twenty sessions followed the protocol?" has no data behind it.
3. **No Codex enforcement.** `install_pc.py` writes no Codex hook, and measured
   runs show Codex hooks do not execute at all on this machine.
4. **Nominal reviewer independence.** `_same_actor` compares strings. Out of
   scope here; recorded so it is not mistaken for solved.

This design closes gaps 1-3 by observation, not by blocking.

## Non-goals

- This is **not an audit trail**. `docs/security.md` already states that graph
  state is not proof that a separate agent performed a review. The journal
  inherits that limit: anyone who can write the user's home directory can forge
  or delete a record. Documentation must never describe it as proof.
- No blocking. A deviating session is recorded and reported, never stopped.
- No reviewer-independence enforcement (gap 4).
- No live model evaluation.

## Measured facts this design rests on

Every claim below was measured on 2026-08-30, not assumed.

| Fact | Evidence |
| --- | --- |
| Claude and Codex hooks share one stdin contract: `session_id`, `cwd`, `hook_event_name`, `transcript_path`, `stop_hook_active` | Strings extracted from both client binaries |
| `Stop` fires once per assistant turn; `SessionEnd` fires once per session | Claude Code hooks reference |
| `SessionEnd` has a 1.5 s shared budget and its exit code is ignored | Claude Code hooks reference |
| `SessionEnd` does not run on `SIGKILL` | Claude Code hooks reference |
| `SessionStart` exit 2 shows stderr without blocking | Claude Code hooks reference |
| Hooks union across settings scopes; a project file cannot remove a user hook | Claude Code settings reference |
| `CLAUDE_CODE_SESSION_ID` is present in Bash-tool subprocess environments | `env` in a live session |
| Codex hooks are documented with the same event set, but did not execute in four probes on `codex-cli` 0.146.1 and 0.151.0-alpha.7.1 | `hooks.json` parsed (timeout-clamp warning emitted) yet no hook command ran |
| Codex records every session in `~/.codex/state_5.sqlite:threads` with `id`, `cwd`, `created_at`, `updated_at`, `source`, `rollout_path`, covering exec, TUI, desktop, and subagent runs | Live probe added one row; 137 rows over 7 months and 10 client versions |
| A working-tree digest alone misses a session that commits its work; adding `HEAD` catches it | Four-case probe; digest returned to the clean baseline value after commit while `HEAD` moved |
| Concurrent `O_APPEND` writes of sub-4096-byte lines do not interleave | 16 processes x 200 lines: 3200 lines, 0 corrupt |
| A snapshot costs about 0.13 s | Timed on this repository |
| The system `python3` here is 3.8.5, below the 3.10 floor | `python3 -V` |

## Signal choice

The conformance signal is a **git working-tree delta bracketed by turn
boundaries**, not a count of editing tool calls.

Rejected alternatives:

- *Tool-event counting.* Blind to edits made through `bash` (`sed`, heredocs,
  scripts), which is precisely how a session that ignores the protocol is
  likely to edit. Also unavailable on Codex.
- *Transcript parsing.* Formats are undocumented and unstable, and reading
  transcripts means reading user content, which `docs/security.md` forbids
  recording.

The delta is `HEAD changed OR working-tree digest changed`. Both terms are
required: the digest alone misses a session that commits, and `HEAD` alone
misses uncommitted work.

Attribution comes from snapshotting on **every** `Stop` (once per turn) rather
than from a separate `PostToolUse` hook. Changes that appear between two
consecutive turn snapshots are bracketed by agent activity. This yields the same
attribution benefit as tool events with no additional managed hook and no
process spawn per edit.

## Data model

Location: `<install_root>/journal/YYYY-MM.jsonl`, where `install_root` defaults
to `~/.local/share/graph-engineering-agent-harness` — the same root the
installer already owns. `INSTALL_DIRECTORY` moves from `scripts/install_pc.py`
into `agent_harness/paths.py`; the installer imports it (the dependency already
runs installer to library and cannot be reversed).

`paths.user_data_path()` confines journal paths beneath the selected home and
rejects link-like components, mirroring `install_pc._assert_safe_user_path`.
`GRAPH_HARNESS_HOME` overrides the home for tests.

Writing: one `os.write` on a descriptor opened `O_WRONLY | O_APPEND | O_CREAT`,
mode `0o600`. Lines are capped at `MAX_JOURNAL_LINE_BYTES = 4096`, the bound
under which concurrent appends were measured not to interleave. Over-long
records drop their variable-length fields rather than fail.

**The writer never raises into its caller.** `journal.append` returns `False` on
`OSError`. An observation layer that breaks `graphctl` would be worse than no
observation layer.

Records share `schema_version`, `ts` (existing `graph.utc_now()`), `event`,
`client`, `session_id`, `repo`.

| `event` | Source | Extra fields |
| --- | --- | --- |
| `session_open` | `SessionStart` hook | `source`, `snapshot` |
| `turn_end` | existing `Stop` hook | `snapshot` |
| `session_close` | `SessionEnd` hook | `reason`, `snapshot` |
| `graph_transition` | `cli.py`, after a handler succeeds | `command`, `node`, `status`, `actor` |

`snapshot` is `{git, head, digest, files, lines}`. `git: false` distinguishes a
non-repository directory from a clean one — without it the two produce the same
digest.

`session_id` comes from `CLAUDE_CODE_SESSION_ID` when present; otherwise records
are correlated by `(repo, time window)`.

Retention: monthly files, `MAX_JOURNAL_MONTHS` retained,
`conformance --prune` deleting older months, and a `doctor` warning above a
byte ceiling. This follows the repository's existing `MAX_*` discipline, under
which every unbounded resource carries a stated bound.

Absolute repository paths are recorded. This matches `manifest.json`, which
already stores the absolute home and target paths in the same directory. The
`docs/security.md` rule requiring repository-relative references governs graph
evidence, which is shared and committed; the journal is neither.

## Verdicts

Folded per `session_id`:

| Verdict | Condition |
| --- | --- |
| `unobserved` | no `session_open` |
| `incomplete` | opened, never closed — expected, since `SessionEnd` does not survive `SIGKILL` |
| `read_only` | neither `HEAD` nor digest changed; excluded from the denominator |
| `conformant` | changed, and at least one `graph_transition` |
| `bypass` | changed, and no `graph_transition` |

Each verdict carries `confidence` (`high` when the change falls inside turn
boundaries, `medium` for a git-only delta, `low` outside git or without a
baseline), `contested` when another session overlapped in the same repository,
and `changed_files` / `changed_lines`.

Size is reported, never judged. The protocol governs non-trivial work; a machine
cannot decide triviality, so `conformance --min-files` lets a human filter.

Warnings are separated from records. Everything is recorded; the next
`SessionStart` reports a prior bypass on stderr and exits 2 (shown, not
blocking) only above two changed files or twenty changed lines. `SessionEnd`
cannot carry the warning: its exit code is ignored and its budget is 1.5 s.

## Codex

Working path today: read `~/.codex/state_*.sqlite` (highest generation), table
`threads`, `mode=ro`, extracting only `id`, `cwd`, `created_at`, `updated_at`,
`source`, `cli_version`. No message content is read. Required columns are
asserted and their absence fails loudly, because the schema is undocumented and
its filename carries a generation number.

Codex sessions have no working-tree baseline, so they yield
`bypass_suspected` at `confidence: low`, corroborated by `git log` within the
session window when commits exist.

Future path: the installer writes the same three hook entries into
`~/.codex/hooks.json` under the existing managed-entry discipline. `doctor`
reports whether those hooks have ever fired, judged from journal data rather
than simulation. Today it will report "configured, never observed".

## `doctor`

`doctor` already exists at `cli.py:368`. It is extended, not replaced; existing
keys (`graph_valid`, `lock_present`, `lock_path`, `lock_pid`, `warnings`,
`healthy`) stay, because tests assert them. Added: journal path and size, last
observation per client, hook liveness, and the interpreter version behind the
configured hook command — a real failure mode, since the system `python3` on
this machine is below the supported floor.

`doctor` stays library-side and does not import `scripts/`; it names
`install_pc.py --check` for install drift rather than performing it.

## Documentation changes

- `docs/how-it-works.md` — new, explains the whole repository for a
  non-engineer.
- `docs/conformance.md` — new, the mechanism reference.
- `docs/security.md` — three statements become false when the hook writes and
  must be corrected: that the hook performs no write operations, that it
  returns immediately when no graph exists, and that state is confined to the
  workspace. Adds journal retention and an explicit statement that the journal
  is forgeable and is not an audit trail.
- `docs/platform-compatibility.md` — records that Codex hooks did not execute in
  four probes across two client versions, with the reproduction.
- `README.md` — one pointer.

## Test plan

`unittest` only, matching the repository. Temporary homes via
`GRAPH_HARNESS_HOME`; CLI exercised through `subprocess` for exit codes and
in-process for injected failures; Windows skips follow the four existing forms.

Cases: append atomicity under concurrency; line-cap truncation; writer failure
leaving `graphctl` unaffected; the four snapshot shapes (tracked re-edit,
untracked re-edit, commit-to-clean, non-repository); every verdict including
`contested` and `incomplete`; `prune` bounds; Codex reader against a synthetic
SQLite fixture and against a schema missing a required column; `doctor` keys
preserved; read-only commands mutating nothing.

Full local gate before completion: `python -m unittest discover -v`, `black
--check`, `mypy` strict, `sync_adapters.py --check`, `evals/runner/run.py`,
`compileall`, `install_pc.py --check`, and `graphctl completion-check`.
