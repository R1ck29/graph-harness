# Plan: observe conformance from a signal that cannot mistake a pull for work

Objective: judge a session on direct evidence that it changed code, and report
what the harness itself catches.

Design: `docs/superpowers/specs/2026-08-31-conformance-redesign-design.md`.
Step plan: `docs/superpowers/plans/2026-08-31-conformance-redesign.md`.
Executable plan: `task-graph.json` (7 nodes), which is the authority for status.

- [x] `edit-events` — record an editing tool call, never a path or its contents.
- [x] `installer-hook` — manage the edit hook on both clients.
- [x] `escape-hatches` — `grant-attempt` and `supersede`, so a person's decision
      is recorded rather than hand-edited.
- [ ] `bypass-signal` — judge on edit events and dirty-tree differences.
      **Failed three reviews; budget exhausted at 3 of 3.**
- [ ] `effectiveness` — count what independent review caught, what rework cost,
      and how often the protocol was followed.
- [ ] `docs` — describe the signal the code actually uses, blind spot included.
- [ ] `gate` — the full local verification gate.

## Where `bypass-signal` stands

Three attempts, three independent FAILs, budget exhausted again at 3 of 3 after
the user granted a third. Six review rounds in total on this problem, counting
the superseded graph.

**Every single failure has been in the working-tree term. The edit-event term
has never failed a review.** Attempt 3's reviewer states it directly: adding an
`Edit` event to the failing scenario makes it report correctly, so only the
shell half is broken.

Attempt 3 did close what it set out to close. `REBASE_HEAD` is gone from the
operation markers, which now hold only markers git removes when an operation
ends, and a cross-git test drives a real conflicted rebase to completion under
the second git on the machine; re-adding `REBASE_HEAD` fails three tests
including that one, with "silenced under /usr/bin/git". The suite can now see a
class of defect it previously could not.

It failed on two new holes in the same term:

- **A shrinking path set cancels authored work.** A boundary whose dirty-path
  set is a strict subset of the first satisfies neither the new-path test nor
  the same-paths-different-content test. Confirmed here directly: a repository
  opening with two dirty files, where the session writes 900 lines into one
  through the shell and commits the other, gives `edited() False` and
  `session_size() (0, 889)` with `M a.py` on disk. The size term contradicts
  the detection term inside one module.
- **The truncation fallback re-admits attempt 2's defect verbatim.** Above
  `MAX_SNAPSHOT_PATHS` the per-path rule is abandoned for the whole session and
  the net-count comparison that already failed is used instead, so the fix is
  gated on how dirty the repository happens to be.

## The pattern, after six rounds

| Round | What the tree term was caught doing |
| --- | --- |
| 1 | buried candidates, unclaimed reports, concurrent double-reporting, timestamp collisions, forged heads |
| 2 | a concurrent claim invisible behind a truncated history |
| 3 | `git pull` and `git checkout` reported as bypasses |
| 4 | conflicted pull, `stash pop`, submodule update reported; discards classified `bypass` |
| 5 | a stale `REBASE_HEAD` silencing every session; net counts hiding real authoring |
| 6 | a shrinking path set hiding real authoring; the truncation fallback restoring round 5's defect |

Each fix has been correct for the case it addressed and has exposed the next
one. That is what an underdetermined signal looks like: the working tree
records *that state changed*, never *who changed it*, and every round has been
an attempt to recover the missing half by inference.

The remaining recommendation from review is a further increment of the same
kind — per-path digest samples accumulated across consecutive boundary pairs.
It would likely pass the two named scenarios and is not obviously the last one.

## Recommendation

Retire this node and re-scope, rather than grant a fourth attempt.

Report bypasses from **edit events only**, which are direct causal evidence
that this session's agent wrote a file and have passed every review. Demote the
working-tree comparison to context in `graphctl conformance` — a low-confidence
hint, never a warning trigger. That removes the entire false-positive family
and both remaining holes at once, because none of `git pull`, `stash pop`,
`apply`, a discard, a truncated tree or a concurrent writer produces an edit
event.

The cost is real and must be documented, not glossed: a session that edits only
through the shell stops producing a warning. Whether that gap is worth closing
later is a separate question with a separate answer — a `Bash` `PostToolUse`
event would give the same causal attribution for shell writes that `Edit` gives
for tool writes, and it belongs in its own node with its own review.

Retiring a node and granting an attempt both require an explicit instruction
from the user. Neither has been run for this node by this session.

## What each earlier attempt was caught doing

**Attempt 1** removed the `HEAD` term, and the dirty-tree term inherited the
same defect. A conflicted `git pull` was reported; so was the same session after
`git merge --abort`, with a byte-identical tree and an unmoved `HEAD`, because
the size charged is a maximum across boundaries. `git stash pop` and
`git submodule update --remote` were reported. `git checkout -- .`,
`git clean -fd` and a hard reset over pre-existing dirt were silent at the hook
but classified `bypass` by the report.

**Attempt 2** added three rules and both of them broke something new:

1. *Not judgeable while a git operation is open*, detected from marker files.
   `REBASE_HEAD` is not removed when a rebase finishes. Confirmed here directly:
   on `git 2.50.1` (`/usr/bin/git`, Apple's stock git) a completed rebase leaves
   `REBASE_HEAD` on disk with a clean status and no rebase in progress; on
   `git 2.21.0` (`/usr/local/bin/git`, which is first on `PATH` and is what the
   test suite runs) it does not. So in any repository where someone has ever
   resolved a rebase conflict, **every** session is silently `unobserved` —
   including edit-event sessions, which nothing about a stale file makes
   unreliable — until the next rebase. The suite could not see it.

2. *A digest difference is only work if the session added something*, measured
   as net scalar counts against the first boundary. Subtraction on counts rather
   than content hides real authoring after a dirty start: deleting ten junk
   files and writing a fifty-line feature, or committing eight WIP files and
   writing a hundred-line one, both come out `read_only` while the authored file
   sits visibly in the tree.

Both defects are mine, introduced by the fix for the previous round. The
reviewer's suggested direction: detect an operation actually in progress from
the `rebase-merge` and `rebase-apply` directories plus the four markers git does
remove on completion, dropping `REBASE_HEAD`; and take "added something" from
per-path content rather than from net counts. Both would need tests that run
under a git that behaves like `/usr/bin/git`, since the suite passed only
because of which git is first on `PATH`.

Sub-threshold findings recorded in the graph, not fixed: `git worktree add`,
`cherry-pick -n`, `checkout BRANCH -- PATH`, `git apply` and un-ignored build
artefacts all report as `bypass`; writing `.git/MERGE_HEAD` by hand silences a
real bypass in one command.

## The objective this replaces, and why

The record below is kept whole. It is the reason the design changed, and
deleting it would make the new graph look better than it earned.

The previous objective — make session conformance observable — reached 7 of 11
nodes verified. Its `bypass-report` node failed three independent reviews and
was retired at 3 of 3 attempts. Rounds 1 and 2 were ordinary bugs and were
fixed. Round 3 was not: `git pull` and `git checkout` were reported as protocol
bypasses, because the signal was a `HEAD` move, which cannot distinguish
authored work from work that arrived from somewhere else.

The graph for that objective is archived at
`task-graph.superseded-2026-08-31.json` with all three failures intact.
Rebuilding reset the attempt budgets. That is stated plainly because this
session refused to do exactly that three hours earlier when the motive would
have been to escape a budget; it happened here because the user directed a
design change, and the failures remain readable in the archive, in the reviews
under `tasks/artifacts/`, and in the section below.

The seven verified nodes' code is reused unchanged: `journal-core`, `snapshot`,
`cli-transitions`, `codex-reader`, `doctor-ext`, `session-records`, `installer`.

---

# Superseded plan: make session conformance observable

The plan before that one, installing the harness for Codex and Claude Code on
this PC, is complete. Its record is in git history and in
`task-graph.completed-2026-08-27.json`.

Objective: make harness protocol conformance observable across every Claude Code
and Codex session, without blocking work.

Design: `docs/superpowers/specs/2026-08-30-session-conformance-design.md`.
Executable plan: `task-graph.superseded-2026-08-31.json`.

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
- [ ] `bypass-report` — decide and report a bypass. Attempt 3 failed on one
      criterion of five. **Budget exhausted again at 3 of 3.**
- [ ] `conformance-cmd` — written and tested; blocked on `bypass-report`.
- [ ] `docs` — written; blocked on `conformance-cmd`.
- [ ] `gate` — blocked.

The `hooks` node was split after it exhausted its budget: both failures were in
deciding and reporting a bypass, while recording boundaries passed its criteria
in both rounds. `session-records` owns the recording and is verified;
`bypass-report` owns the judgement and exhausted its own budget in turn. The
graph was rebuilt once for that split, carrying every recorded verdict across
verbatim; it has not been rebuilt again, because resetting an attempt budget by
rebuilding would defeat the thing the budget is for.

## The budget grant, and the gap it exposed

`bypass-report` reached `max_attempts` and the protocol says to stop and
escalate rather than reset counters. The escalation was made and the user
answered it: give the node one more attempt and finish the verification by the
normal procedure. `max_attempts` was raised from 2 to 3 on that node alone.
`attempts` was not touched and stands at 3 after the retry, so the two recorded
failures are still on the record and still count.

That change had to be made by hand, because the harness has no supported way to
record the outcome of its own escalation: `graphctl` can refuse an attempt but
cannot be told that a human granted one, and `retry` refuses before any of this
can be expressed. A `graphctl` command that records a granted attempt with the
grantor and the reason — so the grant is as visible as the failures it follows —
is the missing piece. It is deliberately not built here; adding a way for the
agent to widen its own budget in the middle of being budget-limited is the one
change that should not be made by the agent that wants it.

## What attempt 3 found

The granted attempt was spent, and it bought a real answer rather than a
formality. The attempt-2 concurrency defect is closed and was confirmed closed
by a reviewer that wrote its own twelve-process racer and showed both the
positional cut and the file lock are load-bearing by removing each in turn. Four
of the five criteria passed on that evidence.

The fifth failed, on something no earlier round had looked at:

> committed work is sized from the commits themselves, so a pull, a checkout or
> a one-line commit is not reported

`committed_size` runs `git diff --shortstat` between the two recorded heads,
which measures a pulled or checked-out diff exactly as it measures work the
session authored. Confirmed here directly: in a real clone, a `git pull
--ff-only` of two upstream files of forty lines each, with the session doing
nothing else, gives `change_size (0, 0)`, `committed_size (2, 80)` and
`worth_reporting (True, 2, 80)`, so the next session start accuses someone who
only ran `git pull`. A branch checkout does the same. Of the three sources the
criterion names, only the one-line commit is prevented, and the pre-existing
size threshold is what prevents it.

The design intent was wrong, not just the implementation: sizing from commits
does not keep a pull below the threshold, because a pull's diff is exactly as
large as the work it brings. Nothing in the module discriminates the *origin* of
a `HEAD` move; there is no `merge-base`, `rev-list`, `is-ancestor` or committer
check anywhere in `agent_harness/`.

Two shipped statements assert the property the code does not have and are
therefore also false: `docs/conformance.md:84-86` and the `committed_size`
docstring.

No test in the 247-test suite performs a pull or a checkout. The test named in
the submitted evidence, `test_a_pull_sized_head_move_is_not_reported`, makes a
local one-line commit and no pull. The suite is green; the gap was in coverage.

The full review is in `tasks/artifacts/bypass-report-attempt-3-review.md`, the
mutation record in `tasks/artifacts/bypass-report-attempt-3-mutations.md`.

## What a fourth attempt would have to do

1. Discriminate the origin of a head move: refuse a move whose new head is not
   a descendant of the old, and count only commits whose committer matches the
   local identity and whose committer date falls inside the session window.
   Fall back to silence when the origin cannot be established, which is the
   direction this module errs in everywhere else.
2. Add tests that perform a real pull from a real upstream clone and a real
   branch checkout, both well above `WARN_MIN_FILES` and `WARN_MIN_LINES`.
3. Correct `docs/conformance.md:84-86` and the `committed_size` docstring.

That attempt is not open. The node is at 3 of 3 and the same escalation applies:
whether to grant a fourth is the user's decision, not this session's.

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
