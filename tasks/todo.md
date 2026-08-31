# Plan: make session conformance observable

The previous plan, installing the harness for Codex and Claude Code on this PC,
is complete. Its record is in git history and in
`task-graph.completed-2026-08-27.json`.

Objective: make harness protocol conformance observable across every Claude Code
and Codex session, without blocking work.

Design: `docs/superpowers/specs/2026-08-30-session-conformance-design.md`.
Executable plan: `task-graph.json` (10 nodes), which is the authority for status.

## Plan

- [x] Measure what the harness can and cannot observe today; record the gaps.
- [x] Verify the client hook contracts, the Codex session store, and the signal
      choice by measurement rather than assumption.
- [x] Write the design and build the task graph.
- [x] `journal-core` — bounded append-only records under a home-confined path.
- [x] `snapshot` — working-tree comparison that survives every edit shape.
- [x] `cli-transitions` — record a transition per successful graphctl command.
- [x] `codex-reader` — enumerate Codex sessions from local metadata only.
- [x] `doctor-ext` — journal and hook liveness without breaking the contract.
- [x] `session-records` — session and turn boundaries, one repository key.
- [x] `installer` — manage the new hook entries on both clients.
- [ ] `bypass-report` — decide and report a bypass. **Attempt budget exhausted.**
- [ ] `conformance-cmd` — written and tested; blocked on `bypass-report`.
- [ ] `docs` — written; blocked on `conformance-cmd`.
- [ ] `gate` — blocked.

The `hooks` node was split after it exhausted its budget: both failures were in
deciding and reporting a bypass, while recording boundaries passed its criteria
in both rounds. `session-records` owns the recording and is verified;
`bypass-report` owns the judgement and has now exhausted its own budget in
turn. The graph was rebuilt once for that split, carrying every recorded
verdict across verbatim; it has not been rebuilt again, because resetting an
attempt budget by rebuilding would defeat the thing the budget is for.

## What the harness could not answer before this work

- A session that edits code without ever creating a graph leaves no trace:
  `claude_hook.py` returns 0 when `task-graph.json` is absent, so the most
  likely deviation was the one nothing could see.
- The graph records nodes, not sessions, so conformance had no denominator.
- The installer wrote no Codex hook, and Codex hooks do not execute here at all.

## Measurements this work rests on

Recorded because each one changed a decision.

- `Stop` fires once per turn, not once per session; `SessionEnd` is the session
  boundary.
- `SessionEnd` has a 1.5 s budget and its exit code is ignored, so it cannot
  carry a warning. The warning moves to the next `SessionStart`, which shows
  stderr on exit 2 without blocking.
- `SessionEnd` does not run on `SIGKILL`, so an unclosed session is expected.
- Hooks union across settings scopes; a project file cannot remove a user hook.
- `CLAUDE_CODE_SESSION_ID` is present in Bash-tool subprocesses, so `graphctl`
  can stamp its own transitions with the session that ran them.
- Codex hooks are documented with the same event set but did not execute in four
  probes across `codex-cli` 0.146.1 and 0.151.0-alpha.7.1. Codex observation
  therefore reads `~/.codex/state_*.sqlite`, which records every session across
  exec, TUI, desktop, and subagent runs.
- A working-tree digest alone misses a session that commits its work; adding
  `HEAD` catches it. Both terms are required.
- Concurrent `O_APPEND` writes below 4096 bytes do not interleave.
- The system `python3` here is 3.8.5, below the supported floor, so the
  interpreter behind a hook command is worth reporting.

## Review

Pending completion.
