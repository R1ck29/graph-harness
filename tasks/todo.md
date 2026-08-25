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

## Closing the open concerns (2026-08-25)

### Plan

- [x] Enforce the static checks the review notes claimed, instead of only
      running them by hand.
- [x] Stop the Claude hook from certifying completion on an untested runtime.
- [x] Exercise the Stop hook through Claude Code itself, not a piped stdin.
- [x] Cover the Codex reviewer config, which had no test at all.
- [x] Independent subagent review of the change and of `agent_harness/graph.py`.

### Review

- CI gained a `static` job running Black and mypy. Neither ran in CI before, and
  no development dependency was declared, so a type or formatting regression
  reached `main` unnoticed. `pyproject.toml` now declares a `dev` extra and pins
  the Black grammar to `py310` and mypy to `strict`, so local and CI agree.
- That job immediately earned itself: mypy rejected the first version of the new
  hook guard, because `sys.version_info` is `tuple[int, int, int, str, int]`
  rather than `tuple[int, ...]`. The guard now passes `sys.version_info[:3]`.
- The Stop hook refuses to run below Python 3.10 and names the interpreter it
  found and the command to fix. Previously the example's bare `python` silently
  ran 3.8.5 on this machine, which the project neither declares nor tests. The
  check sits under `__main__` so it precedes the harness imports when run as a
  hook while leaving the module importable for tests.
- The hook was verified through Claude Code end to end for the first time. A
  real `claude -p` session in a disposable checkout, with the hook wired into
  `.claude/settings.json`, was blocked from stopping, surfaced the guard's
  message, and asked whether to work the node or pause; `stop_hook_active`
  ended the run cleanly. Earlier sessions had only piped JSON to the script.
- That run also showed the guard fires on any stop attempt while a node is
  unverified, whatever the turn was about. Recorded in the adapter README as the
  reason the hook stays opt-in.
- `.codex/agents/reviewer.toml` had no coverage, so a rename or typo in the
  read-only contract would have passed CI. Two tests now assert the sandbox mode
  and the no-self-verdict instruction; they skip on Python 3.10, which has no
  `tomllib`.
- 73 tests pass on Python 3.11 and on the 3.8.5 interpreter this machine's
  `python3` resolves to, plus Black, mypy, adapter drift, compileall, and five
  offline eval cases. Every new test was mutation-checked.

### Independent review findings (2026-08-25)

A subagent reviewed the change and audited `agent_harness/graph.py`, which had
only been exercised behaviorally. Both severe findings were reproduced
independently before any fix.

- **Reviewer independence was defeated by one trailing space.** Identity checks
  compared raw strings, so executor `"agent-1 "` and reviewer `"agent-1"` were
  different actors. A single agent ran start, submit and PASS and reached
  `{"complete": true}`; a case-only difference worked the same way. All six
  comparison sites now go through `_same_actor`, which strips and casefolds.
  This defeated the one invariant the harness exists to enforce.
- **Upstream failure permanently stranded innocent descendants.** Invalidation
  never refunded the discarded attempt, so every upstream FAIL spent one attempt
  from every descendant. With the schema minimum `max_attempts: 1`, one upstream
  failure left a node that never failed a review at `1/1`, where both `retry`
  and `start` refuse it and no CLI command can recover it. Confirmed the
  objective became uncompletable. `_invalidate_descendants` now refunds the
  attempt for the states that actually hold discarded work, and only once per
  invalidation. A node's own review failures still exhaust the budget.
- The version guard's wiring was untested: deleting the `__main__` call left all
  tests green. Two tests now execute the hook as `__main__` with a faked
  interpreter version, pinning `CLAUDE_PROJECT_DIR` and `PYTHONPATH` so the
  result does not depend on whether the package is installed.
- `test_guard_accepts_...` never touched its own boundary, because `(3, 10, 0)`
  already sorts above the 2-tuple `(3, 10)`; tightening `>=` to `>` kept the
  suite green. `(3, 10)` is now in the accept list.
- Static checks were narrower than the trees CI runs: `evals/` was checked by
  neither tool and `tests/` was not type-checked. Both are now covered, which
  required fixing two real mypy errors in the new test code. Tool versions are
  pinned exactly so an unrelated release cannot red the job.
- 83 tests pass on Python 3.11 and on this machine's 3.8.5, plus Black, mypy
  over 18 files, adapter drift, compileall, and five offline eval cases. Every
  fix was mutation-checked: reverting either severe fix fails the new tests.

Not done: no Codex session was run. `codex doctor` reports no warnings and the
reviewer config's field names appear in the Codex binary, but that is evidence
the configuration is well formed, not proof a review round-trips.

### Remaining review findings closed (2026-08-25)

The previous commit fixed four of seven findings. The rest are now closed; each
was reproduced first.

- A verdict recorded straight over an UNCERTAIN review destroyed it. `withdraw`
  archived the doubt into `review_history`, but `verify` overwrote it, so a node
  read as a clean single-review PASS with the reviewer's stated uncertainty gone
  and unrecoverable. All three verdict paths now go through
  `_record_verification`, which archives a superseded UNCERTAIN using the same
  record shape. It runs only after every check passes, so a rejected verdict
  still archives nothing.
- `submit` wrote the new evidence before validating `actor_id`, so a rejected
  call left the attacker's evidence on a still-`running` node. The CLI was safe
  because it never saves a failed transaction, but `Graph` is public API and a
  rejected operation must be a no-op. Identity and context checks now precede
  every mutation.
- Reopening a verified ancestor left its PASS attached, so a `failed` node
  carried a passing verdict until someone called `retry`. The reopened ancestor
  now loses `verification`, `reviewer_id`, and `verified_at`. A node that faults
  itself keeps its FAIL record.
- Three CLI tests only passed when run from the repository root, because the CLI
  confines paths to the current directory. CI always runs from the root, which
  hid it. `GraphCtlAtomicityTests` now pins the working directory, and the suite
  passes from an unrelated directory.
- 90 tests pass on Python 3.11 and 3.8.5, from the repository root and from an
  unrelated directory, plus Black, mypy over 18 files, adapter drift,
  compileall, five offline eval cases, and a clean-install quick start. Each of
  the three fixes was mutation-checked.
