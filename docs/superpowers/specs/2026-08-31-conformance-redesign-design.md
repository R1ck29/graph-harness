# Conformance observation, redesigned — and a measure of the harness itself

Date: 2026-08-31
Status: approved for implementation
Supersedes: `2026-08-30-session-conformance-design.md` (signal choice only; its
journal, snapshot, CLI-transition, Codex-reader, doctor and installer decisions
stand and their code is reused)

## Why this exists

Two questions, one of which the harness has never been able to answer at all.

1. **Was the protocol followed?** The 2026-08-30 design set out to answer this
   and its answer proved unreliable in a specific, repeatable way.
2. **Is the harness worth its cost?** Nothing measures this. The graph records
   what happened to each node and then that record is never read back.

## What went wrong with the first answer

The 2026-08-30 signal is a git working-tree delta bracketed by turn boundaries:
`HEAD changed OR working-tree digest changed`. The node that decides and reports
a bypass from that signal was reviewed three times and failed three times.

| Round | Reviewer finding |
| --- | --- |
| 1 | A buried bypass is lost forever; a report is printed without checking that the record which makes it happen once was written; concurrent starts both report; the winner is chosen by string timestamp; a forged `HEAD` reaches git unvalidated |
| 2 | The claim is made under a lock but decided from a truncated history, so a concurrent winner's claim is invisible to the loser |
| 3 | `git pull` and `git checkout` are reported as bypasses |

The first two rounds are ordinary bugs and they were fixed; a reviewer that
wrote its own twelve-process racer confirmed round 2 closed, and showed both the
positional cut and the file lock are load-bearing by removing each in turn.

Round 3 is not an ordinary bug. `committed_size` runs
`git diff --shortstat <before> <after>`, which measures a pulled diff exactly as
it measures authored work. Confirmed directly on a real clone: a `git pull
--ff-only` of two upstream files of forty lines each, with the session doing
nothing else, yields `change_size (0, 0)`, `committed_size (2, 80)` and
`worth_reporting (True, 2, 80)`. The next session start accuses someone who ran
one `git pull`.

The root cause is the signal, not the function. **A `HEAD` move says something
changed; it cannot say who changed it or why.** Authoring, pulling, checking
out, rebasing and a colleague's commit in another terminal are the same
observation. Each round has been one more false-positive source patched —
buried candidates, concurrent claims, timestamp collisions, forged heads,
pre-existing dirt, now pulls. Patching the sixth is not progress towards a
correct answer.

The old design named this risk and dismissed it: it rejected tool-event
counting because a session can edit through the shell. That is true, and it
argues for keeping the tree comparison as a second term. It does not argue for
making an ambiguous signal the only one.

## Measured facts this design rests on

Measured 2026-08-31 on this machine, not assumed.

| Fact | Evidence |
| --- | --- |
| `PostToolUse` with `matcher: "Edit|Write"` runs in practice here | `~/.claude/settings.json` already carries a working third-party entry on that matcher |
| A hook process costs 23.6 ms for a bare interpreter and 40.4 ms importing `agent_harness.journal` | Ten runs each, `venv/bin/python` 3.13 |
| A session-boundary hook that also snapshots costs about 205 ms | Five runs of `graphctl-session-end` |
| `git pull --ff-only` of 2 files / 80 lines gives `change_size (0, 0)`, `committed_size (2, 80)`, `worth_reporting (True, 2, 80)` | Real upstream and real clone, run directly against the library |
| No test in the 247-test suite performs a `git pull` or a branch `checkout` | The test named `test_a_pull_sized_head_move_is_not_reported` makes a local one-line commit |
| The stray `tmp*` directories in the repository root are crash residue, not a per-run leak | Five clean runs of `tests.test_quickstart_contract` left none |
| The graph archives already hold 44 failure records and 6 archived attempts across three objectives | Counted from `task-graph*.json` |

## Non-goals

Unchanged from the superseded design, and restated because they still bind:

- Not an audit trail. Anyone who can write the user's home can forge or delete a
  record. No document may describe it as proof.
- No blocking. A deviating session is recorded and reported, never stopped.
- No reviewer-independence enforcement.
- No live model evaluation.

Added here:

- No reading of user content. The edit hook records no file contents and no file
  paths — see below.

## Part 1 — The signal

### The new event

A `PostToolUse` hook matched on `Edit|Write|MultiEdit|NotebookEdit` appends one
`edit` record:

```json
{"schema_version": 2, "ts": "...", "event": "edit", "client": "claude",
 "session_id": "...", "repo": "/abs/path", "tool": "Edit", "path_id": "a1b2c3d4e5f6"}
```

`path_id` is the first twelve hex characters of the SHA-256 of the repository-
relative path. It exists so distinct edited files can be counted. **The path
itself is never recorded.** `docs/security.md` forbids recording user content,
and a path is content enough: `clients/acme/contract.md` names a customer in the
filename. A hash counts what needs counting and leaks nothing.

The hook is the cheapest producer in the system: read stdin, hash one string,
append one line, exit 0. It takes no snapshot and calls no git. It never raises
into its caller, like every other producer.

### The detection rule

A session did engineering work when either term holds:

```
edited = (at least one `edit` record for this session)
      OR (the working-tree digest differs between two consecutive
          turn-boundary snapshots of this session)
```

The first term is direct causal evidence: this session's agent invoked an
editing tool. The second term covers edits made through the shell — `sed`, a
heredoc, a script — which is the case the old design was right to protect.
Turn-boundary snapshots already exist; the `Stop` hook takes one every turn.

**`HEAD` is never evidence of work.** It stays in the snapshot as context and is
reported, but no verdict depends on it.

That one sentence removes the round-3 defect at its source. No ancestry check,
no committer matching, no time window and no `HEAD` validation is needed,
because no recorded `HEAD` is ever passed to git again.

**It is not sufficient on its own, and the first draft of this section said it
was.** That draft claimed a pull, a checkout, a rebase, a reset, a colleague's
commit and a submodule update all leave the tree undirtied and are therefore
silent. A review disproved it: a conflicted pull, rebase, cherry-pick or revert
fills the tree with content nobody authored; a submodule update dirties the
gitlink; `git stash pop` materialises real content; and discarding pre-existing
dirt with `git checkout -- .`, `git clean` or a hard reset moves the digest
while adding nothing. Three further rules are therefore required:

- A snapshot taken while git has a merge, rebase, cherry-pick, revert or bisect
  open is **not judgeable**, exactly as a degraded one is not. Aborting the
  operation afterwards does not repair the record, because the size charged is
  a maximum across boundaries, so one boundary taken mid-conflict would accuse
  a session whose net effect was nothing.
- Submodule differences are excluded at the source, with
  `--ignore-submodules=all` on the status and diff calls.
- A digest difference alone is not work. The session must also have **added**
  something: an edit record, or a boundary showing more changed files or lines
  than its first boundary did. Throwing dirt away is not authoring.

`git stash pop` remains indistinguishable from authoring and is documented as
the one known false positive rather than papered over.

### Sizing

Reported size is a magnitude, never the trigger.

- `changed_files` — the number of distinct `path_id` values for the session,
  or, when only the shell term fired, `max(files_i) - files_0` over the
  session's snapshots, floored at zero.
- `changed_lines` — `max(lines_i) - lines_0` over the session's snapshots,
  floored at zero.

Taking the maximum across turn boundaries rather than comparing the two ends is
what keeps a session that edits and then commits from measuring as zero: the
commit returns the tree to clean, but an intermediate turn boundary saw the
work.

The warning thresholds are unchanged: `WARN_MIN_FILES = 2`, `WARN_MIN_LINES = 20`.

### The known blind spot

A session that writes a file through the shell **and** commits it **within a
single turn** leaves every snapshot clean and produces no edit event. It is not
detected.

This is a failure towards silence, which is the direction this module errs in
everywhere else, and it is narrow: it requires the shell, a commit, and no turn
boundary in between. It is pinned by a test asserting silence, so it cannot
change unnoticed, and it is stated in `docs/conformance.md`. It is not
presented as a limitation of the idea; it is the price of refusing to accuse
anyone on a `HEAD` move.

### Codex

Codex parses its hook configuration and does not execute it — four probes across
two client versions. Codex sessions are therefore recovered from
`~/.codex/state_5.sqlite` and reported as `unobserved` with the reason attached.
No verdict is inferred from a session whose working tree was never observed.
This is unchanged, and it is stated rather than papered over.

### What is deleted

- `worktree.committed_size` and the `COMMIT_NAME` guard that exists only to keep
  a forged `HEAD` out of a git argument list.
- The `HEAD` term in `worktree.changed`, which becomes a digest comparison and
  is renamed to say so.
- `session_hooks.worth_reporting`'s call into commit sizing.
- The claims in `docs/conformance.md:84-86` and the `committed_size` docstring,
  which assert a property the code never had.

## Part 2 — `graphctl effectiveness`

The compliance report answers "was the procedure followed". This answers "was
following it worth anything". It reads and writes nothing.

### What it reports

| Metric | Computed from |
| --- | --- |
| **Defects caught by review** | `verify --fail` verdicts. Of those, the ones whose submitted evidence contained a `kind: test` item — the executor had test coverage and review found a defect anyway |
| First-pass rate | Verified nodes with `attempts == 1` over all verified nodes |
| Rework | Distribution of `attempts`; size of each invalidation cascade |
| Escalations | Nodes that reached `max_attempts` |
| Conformance | The per-session verdicts from Part 1 |

The first row is the point. The harness's claim is that an independent reviewer
with explicit criteria catches things a green test suite does not. On
2026-08-31 that happened in this repository: 247 passing tests, and the reviewer
found that `git pull` triggers a false accusation. Until now that event was
recorded and never counted.

### Where the numbers come from

The journal's `graph_transition` records are the event stream: append-only, one
record per successful state change, so nothing is double counted. Detail that
the stream does not carry — failed criteria, reviewer identity, evidence kinds —
is read from the graph files by `(objective, node, attempt)`.

Graph archives predate the journal, so they are backfilled once, deduplicated on
the same key. A record already present in the journal wins.

## Part 3 — Two escape hatches the harness is missing

Both were hit in this session, and both forced a hand edit or a rebuild.

**`graphctl grant-attempt NODE --granted-by WHO --reason TEXT`.** The protocol
says to stop and escalate when `max_attempts` is reached. When the human answers
"take one more", there is no way to say so: `retry` refuses first, and raising
`max_attempts` means editing the graph by hand — the one thing agents are told
not to do. The command records the grant with its grantor and reason, raises the
ceiling by exactly one, and never touches `attempts`, so the failures stay on
the record. A grant is visible in `status` and counted by `effectiveness`.

**`graphctl supersede NODE --reason TEXT`.** There is no transition for a node
whose approach was abandoned. A failed node blocks its descendants forever, so
changing approach means rebuilding the whole graph — which silently resets every
attempt budget, exactly the laundering the budget exists to prevent. `supersede`
retires the node, releases descendants that no longer need it, and keeps its
failure history in place.

Neither command may be invoked by an agent to unblock itself. Both require an
explicit human instruction, both record who gave it, and `doctor` reports their
use. This is stated in `roles/` and in `core/protocol.md`.

## Data model changes

- `schema_version` goes to 2. Readers accept 1 and 2; version 1 records have no
  `edit` events, which reads correctly as "the shell term only".
- New event `edit` with `tool` and `path_id`.
- `MANAGED_HOOK_EVENTS` in `scripts/install_pc.py` gains `PostToolUse`, written
  with `matcher: "Edit|Write|MultiEdit|NotebookEdit"`. Hooks union across
  scopes, so the existing third-party `PostToolUse` entry is untouched.
- `doctor` reports edit-hook liveness alongside session-hook liveness, and names
  the interpreter behind each configured command.

## Testing requirements

The round-3 failure was a coverage failure as much as a design failure. These
are required, not suggested:

- A real `git pull --ff-only` from a real upstream clone, a real branch
  `checkout`, and a real `git reset --hard`, each moving more than
  `WARN_MIN_FILES` and `WARN_MIN_LINES`, each asserting **silence**.
- A commit made by a second identity in the same tree while the session runs,
  asserting silence.
- A session that edits through `Edit`, asserting a report.
- A session that edits only through a shell heredoc across two turns, asserting
  a report.
- The blind spot: shell edit plus commit inside one turn, asserting silence, so
  the gap is pinned rather than discovered later.
- The edit hook: no path appears anywhere in the journal; a payload naming a
  path outside the repository is recorded with a `path_id` and nothing else; a
  hook that cannot append still exits 0.
- `effectiveness` on a constructed graph and journal produces each metric, and
  writes nothing to either.
- `grant-attempt` raises the ceiling by one, leaves `attempts` alone, and is
  refused on a node that has not failed. `supersede` releases descendants and
  keeps failure history.

## Migration

1. `task-graph.json` is archived as `task-graph.superseded-2026-08-31.json`. The
   three `bypass-report` failures stay in it. This document is why it was
   abandoned.
2. The seven verified nodes' code is reused unchanged: `journal-core`,
   `snapshot`, `cli-transitions`, `codex-reader`, `doctor-ext`,
   `session-records`, `installer`. Their verdicts are not carried forward as
   verdicts — the new graph's nodes are different work — but their code is not
   rewritten.
3. A new graph is built for this design and executed under the normal protocol.

**Rebuilding resets attempt budgets.** That is stated plainly because it is the
move this session refused to make on its own three hours earlier. It is
happening now because the user directed a design change, not because a budget
ran out, and the abandoned approach's failures remain readable in the archived
graph and in `tasks/todo.md`.

## Risks

- **Hook cost.** 40 ms per edit, on a hook the client already runs others on. An
  edit-heavy turn of fifty edits adds about two seconds spread across the turn.
  If this proves noticeable, the hook can batch by writing to a per-session file
  and folding it in at `Stop`; not built now.
- **The blind spot above.** Narrow, silent, tested, documented.
- **Codex stays unobserved.** Unchanged, and honest. If Codex hooks begin to
  execute, the same `edit` producer serves it with no design change.
- **`effectiveness` on thin data.** With three objectives on record the numbers
  are anecdotes, not statistics. The command reports counts and denominators
  rather than a single score, so a reader can see how thin the base is.
