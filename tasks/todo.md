# Agent Harness v1 implementation

Started: 2026-08-25

## Plan

- [x] Confirm the repository boundary and inspect current Codex and Claude Code documentation/CLI behavior.
- [x] Define the minimal vendor-neutral architecture and deterministic state machine.
- [x] Write tests for graph validation, transitions, evidence, retry, adapters, and eval loading.
- [x] Implement the standard-library graph core, atomic storage, failure memory, and `graphctl`.
- [x] Add concise roles, canonical skills, generated platform adapters, and compatibility notes.
- [x] Add five portable eval cases, offline comparison runner, README, and integration guide.
- [x] Run all tests, offline evals, adapter validation, formatting/static checks, and secret scans.
- [x] Obtain an independent code/security review and resolve all critical/high findings.
- [ ] Create and push the public GitHub repository, then verify the remote commit and visibility.

## Review

- 36 unit/integration tests pass on local Python 3.11.
- Strict mypy, Black, compileall, JSON/TOML parsing, adapter drift checks, and
  Claude/Codex adapter discovery pass.
- Five offline protocol cases preserve unrelated branches and avoid 5 of 17
  full-replay node reruns; model-dependent metrics remain explicitly unmeasured.
- Independent Python, security, and specification reviews report no remaining
  CRITICAL or HIGH findings.
- Failure memory is graph-resident so state and diagnosis commit atomically.
- Known operational limit: after confirming no writer is active, a crash-stale
  graph lock must be removed manually as documented in `docs/security.md`.
