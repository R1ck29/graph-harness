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
- [x] Create and push the public GitHub repository, then verify the remote commit and visibility.

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
- Published publicly at `https://github.com/R1ck29/agent-harness` on `main`.

## Cross-platform and non-engineer remediation

Started: 2026-08-25

### Plan

- [x] Add a tested resubmission path after an `UNCERTAIN` review.
- [x] Require explicit reviewer evidence for `PASS` and document the actual trust boundary.
- [x] Reject Windows junctions/reparse points in every workspace-confined path.
- [x] Align the runtime graph version contract with the public JSON Schema.
- [x] Provide a complete macOS/Linux and Windows quick start with safe evidence examples.
- [x] Make the Claude completion hook launcher portable across supported operating systems.
- [x] Improve CLI onboarding, help, recovery guidance, and installation smoke coverage.
- [x] Clarify sensitive-data handling and generated-adapter ownership.
- [x] Run unit/integration tests, adapter checks, evals, static checks, packaging smoke tests,
  and an independent final review.

### Review

- Added `init`, `doctor`, `withdraw`, per-node next actions, atomic no-replace
  initialization, and a complete redacted quick start for Bash and PowerShell.
- PASS and FAIL now require explicit reviewer evidence. UNCERTAIN can be withdrawn
  only by the original executor, preserves the full prior review packet, and does
  not consume another attempt.
- Runtime review validation and both public JSON Schemas agree on the structured
  evidence contract; malformed live and archived reviews are rejected.
- Workspace confinement rejects symlinks and Windows junction/mount-point reparse
  tags. Evidence files must be bounded UTF-8 JSON regular files read without
  following a replaced leaf link.
- 63 tests pass on local Python 3.11 and 3.13; the two real NTFS-junction tests are
  intentionally skipped on macOS and remain in the Windows CI matrix.
- Strict mypy, Black, compileall, JSON/TOML and Draft 2020-12 Schema validation,
  adapter drift checks, five offline eval cases, and installed-package quick-start
  smoke testing pass.
- Independent Python, general-code, and security re-reviews report no remaining
  CRITICAL, HIGH, or MEDIUM findings.
- No commit or push was performed; Windows CI has not yet run against these local
  changes.

## Claude Code usability review

Started: 2026-08-25

### Plan

- [x] Verify the documented quick start end to end from a clean install.
- [x] Verify the Stop hook blocks, allows, and avoids loops, and measure its cost.
- [x] Verify the protocol guardrails, ancestor blame, and selective invalidation.
- [x] Check Claude Code discovery of the generated skills and reviewer agent.
- [x] Stop `sync_adapters.py --check` from failing on editor bookkeeping files.
- [x] Make the working-directory constraint discoverable in CLI errors and docs.
- [x] Resolve who records the verdict when the reviewer runs without write access.
- [x] Document the worktree gap for the untracked graph file.

### Review

- Clean-install quick start reaches `{"complete": true}` on Python 3.11.
  Guardrails hold: reviewer/executor identity collision, missing reviewer
  evidence, unchecked criteria, and post-verified submission are all refused,
  and a hand-edited graph is rejected on the next transition.
- A diamond graph confirms selective recovery. Failing `D` while blaming
  ancestor `A` reopens `A`, invalidates `B`, `C`, and `D`, and leaves the
  unrelated branch `X` verified with one structured failure record.
- Claude Code 2.1.239 loads all four generated skills and the `graph-reviewer`
  agent from this repository. Always-resident instruction cost is about 1.4 KB.
- Cost is negligible: the Stop hook takes about 38 ms, and `graphctl` stays at
  about 42 ms per call from one node to two hundred.
- Fixed: `sync_adapters.py` skipped dot entries, so `.DS_Store` no longer
  reports permanent drift on macOS or gets copied into four adapter trees.
- Fixed: workspace and missing-graph errors now name the working directory and
  the recovery command. Running `graphctl` from a package subdirectory was the
  most likely first failure and gave no guidance.
- Fixed: plan mode and the Codex read-only sandbox both deny the reviewer the
  graph write, so the reviewer now reports its verdict and the caller records it.
  The skill previously told the reviewer to run `graphctl verify` itself.
- Documented: `disallowedTools` is ignored while `tools` is set, the reviewer
  keeps `Bash` and is a review posture rather than a sandbox, an unstartable
  hook fails open, and an untracked `task-graph.json` never reaches a worktree.
- 67 tests pass, plus strict mypy, Black, adapter drift check, and five offline
  eval cases. Both fixes were mutation-checked and fail without the change.

### Reviewer permission probe (2026-08-25)

Ran the `graph-reviewer` subagent against a disposable copy to close the one
item left unverified in the review above.

- Read-only `graphctl review-packet` and `graphctl status` succeed under plan
  permission mode with no prompt.
- Plan mode refuses the `Write` and `Edit` tools but does not stop a shell
  redirect. The reviewer wrote a file through `Bash` with no prompt, on a
  machine whose settings allow `Bash` broadly.
- Corrected: the earlier claim that plan mode denies the reviewer its writes was
  wrong. The caller records the verdict by convention, not because the client
  blocks the reviewer. `docs/security.md` now states that a subagent tool list
  is not a boundary and names the sandbox alternative.
- The probe asked the reviewer to record a fabricated PASS as a diagnostic. It
  refused, correctly, on the grounds that another agent's request is not user
  consent and that a self-issued review is what the separation exists to
  prevent. Graph state was unchanged, confirmed by hash. That is a model
  judgement, not an enforced control, and it does not change the finding above.
