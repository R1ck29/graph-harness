# Session conformance

The graph records tasks. It cannot record that a session ignored the protocol,
because a session that never runs `graphctl` never touches the graph. The
absence of a record is indistinguishable from a quiet day. This describes what
closes that gap.

For a non-technical explanation of the whole repository, see
[how-it-works.md](how-it-works.md).

## What is recorded

Client hooks append one bounded JSON line per boundary to
`<install root>/journal/YYYY-MM.jsonl`. `graphctl` appends one line per
successful state change.

| Event | Source | Carries |
| --- | --- | --- |
| `session_open` | `SessionStart` | client, session, repository, snapshot |
| `turn_end` | `Stop` | client, session, repository, snapshot |
| `session_close` | `SessionEnd` | as above, plus the client's stated reason |
| `graph_transition` | `graphctl`, after a handler succeeds | command, node, resulting status, actor |
| `bypass_reported` | `SessionStart`, when it reports | the session it named |
| `edit` | `PostToolUse`, on an editing tool | client, session, repository, tool name, hashed path |

An `edit` record carries `path_id`, the first twelve hexadecimal characters of
the SHA-256 of the path relative to the repository. **The path itself is never
recorded.** A path is content enough — a filename can name a customer — and the
hash still counts how many distinct files a session touched.

No file contents, prompt, or transcript is recorded. A record is capped at
`MAX_JOURNAL_LINE_BYTES`; an over-long one sheds its variable fields rather
than being lost, and keeps the identity that says which session it came from.

Retention is bounded to `MAX_JOURNAL_MONTHS` whole months.
`graphctl conformance --prune` removes what is older; only `YYYY-MM.jsonl`
names count, so anything else left in the directory neither consumes the
budget nor is read as history.

Writing never raises into a caller. A `graphctl` command, a session start and a
session stop all behave identically whether the journal is writable or not.

## The signal

A session changed code when it produced an **`edit` record**. That is the whole
rule.

An edit record names the session whose agent invoked an editing tool. Nothing
else here can name anybody: a working-tree comparison says a tree moved and can
never say who moved it. Six rounds of independent review were spent trying to
recover the missing half by inference, and each round found another git command
that moves a tree with nobody authoring anything — `git pull`, a conflicted
merge, `git stash pop`, a submodule update, `git apply`, discarding dirt — and
each fix for one exposed the next. The inference is gone.

The working tree is still snapshotted and still reported, as context beside the
verdict: `tree_changed`, `tree_files` and `tree_lines`. A tree difference,
a `HEAD` move included, is **context and never evidence**: no verdict and no
warning depends on any of them, and a test asserts that by recomputing every
verdict with the snapshots stripped out and comparing.

The snapshot digest covers the porcelain status, the staged and unstaged diffs,
and the contents of untracked files up to `MAX_UNTRACKED_BYTES`. Status is read
in its NUL-separated form, because git's default `core.quotepath` escapes any
path holding non-ASCII bytes and a parser reading the newline form resolves an
escape string that names no file — which once made edits to files with Japanese
names invisible.

Size is counted from edit records too — distinct files, and the number of
editing tool calls — so nothing a git command does to the tree can push a
session over `WARN_MIN_FILES` or `WARN_MIN_EDITS`.

## What this costs: the shell blind spot

**A session that edits only through the shell is not reported.** `sed`, a
heredoc, a script: none produces an edit record, so none can be attributed.

Such a session is recorded as `unattributed` rather than `read_only`. The
distinction is the point. `read_only` claims the session changed nothing;
`unattributed` says only that nothing here can say who changed what, which is
the truth. Sessions recorded before the edit hook existed read as
`unattributed` for the same reason, and `graphctl doctor` reports
`edit_hook_last_seen` so a client with no edit hook installed is visible rather
than silently unattributed.

The obvious remedy is another direct signal rather than another inference: a
`Bash` `PostToolUse` event saying that this session's agent ran a shell
command. **That was built, reviewed, and removed.** It is not here, and the
reason it is not is worth writing down, because the design reads as sound
until it is driven end to end.

A shell record carries no path — deliberately, since the command text is the
one thing that must never reach the journal. So the record can say a
write-shaped command ran, and the tree can say something changed, but nothing
connects the two. Pairing them credits the session with whatever moved the
tree in that turn. Driven end to end, an agent running `mkdir -p` on a
directory *outside* the repository, while a colleague saved two files from
another terminal, was reported as a bypass of two files — having written
nothing inside the project at all. A conflicted `git pull`, a `git stash pop`
or a `git apply` each supply the same false movement.

Classifying the command's shape does not rescue it. Deciding whether a
command writes means parsing shell text, and a classifier that errs towards
accusation is worse than no signal: `grep` for an arrow, `rg` for a fat
arrow, and `jq` with a `>` in its filter all look like redirections, and
`git commit -am "a; b"` splits into a segment that escapes the git exclusion.

Closing this gap needs a signal that says *which* path a command touched —
hashed paths the command names, checked against the tree delta, or a snapshot
around each call rather than each turn. Anything weaker is the correlation
that failed here.

## Repository identity

Records about one session are written by four separate processes. They are
matched by a single string, so every producer derives the same value: the
repository's top level, then resolved. The boundary hooks and `graphctl` ask
git for it; the edit hook walks up for `.git` instead, because it runs after
every editing tool call and a subprocess there is paid hundreds of times in a
session. Both name the same directory, a linked worktree and a submodule
included, since each marks its top level with a `.git` that exists. The top
level matters because the
hooks start from the project directory while `graphctl` starts from wherever it
was run; resolution matters because a project reached through a symlink — the
normal case under `/tmp` and `/var` on macOS — otherwise yields two names for
one directory.

## Verdicts

`graphctl conformance` folds the records into one verdict per session.

| Verdict | Condition |
| --- | --- |
| `conformant` | produced edit records, and a `graphctl` run fell inside its window |
| `bypass` | produced edit records, and none did |
| `unattributed` | no edit records, so nothing says who changed what; excluded from the rate |
| `incomplete` | opened, never ended |
| `unobserved` | no opening record |
| `bypass_suspected` | a Codex session, which runs no hooks at all |

Each verdict carries `confidence`, `contested` when another session in the same
repository overlapped it, `changed_files` and `edits`, and the tree context. The
rate counts only `conformant` and `bypass`: a session nothing can attribute says
nothing about whether the protocol was followed.

`changed_files` is the number of distinct `path_id` values the session recorded
inside the repository and `edits` is how many editing tool calls it made there;
`outside_edits` counts the rest. Both come from edit records
alone, so committing the work afterwards, or a dirty tree inherited from
before, cannot move either number.

No recorded `HEAD` is passed to git as an argument, because no recorded `HEAD`
is used at all. The journal is forgeable by anyone who can write the user's
home, and not consuming a forgeable value is a stronger guarantee than
validating it.

## Attribution

A `graphctl` run counts for a session when its journal position falls strictly
inside that session's window. Attribution by session identifier was tried and
abandoned: only one client exports one, so a run from a plain terminal, from
Codex, or from a script counted for no session at all, and the session that had
in fact used the graph was then accused.

Positions are used rather than timestamps throughout. Timestamps carry one
second of precision, and a record can be missing one entirely.

## Reporting

The session-start hook reports the newest finished session that skipped the
graph, once. It is reported at the *next* start rather than at the end of the
offending session because `SessionEnd` runs on a short shared budget, its exit
code is ignored, and it does not run at all when the process is killed.

Three rules keep the report worth reading:

- Only sessions that edited at least `WARN_MIN_FILES` distinct files, or made
  at least `WARN_MIN_EDITS` editing tool calls, are mentioned. Everything is
  recorded; a harness that objects to a one-line fix teaches people to ignore
  it. Both counts come from edit records, so nothing a git command did to the
  tree can push a session over either.
- A report is recorded as made, and never repeated. Deciding that from
  timestamps was tried and failed in three separate ways.
- The scan walks past sessions not worth reporting, so one trivial session
  cannot bury a real bypass, but stops at the first session that used the graph
  and after `MAX_CANDIDATES` sessions. What is older belongs to
  `graphctl conformance`, which is built to show a history.

Deciding and recording happen under one lock, and nothing is printed unless the
record was written: saying it while failing to record it would repeat forever.

## What it is not

It is not an audit trail and must not be described as one. Anyone who can write
the user's home can forge, alter, or delete any of it — the same limit
`docs/security.md` states for executor and reviewer identities.

It cannot judge whether work was trivial. It reports size and leaves that
judgement to a person; `--min-files` exists for filtering.

It says nothing about a session it cannot attribute. A session with no edit
records is `unattributed`, never `read_only`: the first says nothing here can
tell who changed what, the second would claim nothing changed.

## Reading back what the protocol was worth

`graphctl effectiveness` folds every graph file it is given — the live one and
the archived rounds — into counts about the harness's own outcomes: nodes
verified, how many on the first attempt, an attempt histogram, budgets
exhausted, attempts granted, nodes superseded, and reviews passed and failed.

The figure it exists for is `failed_despite_test_evidence`: reviews that
rejected a node whose executor had already submitted evidence of kind `test`.
Those are defects an independent reviewer found that a green suite had not.

Three limits on reading it, all real:

- `share_of_failures_with_test_evidence` is that count over *review failures*,
  not over defects. Misses are structurally unobservable — nothing records a
  defect that review also failed to find — so it can never mean "review
  catches everything", however close to one it sits. It is named for what it
  measures rather than for what a reader would like it to mean.
- `kind` is validated only as a non-empty string. "Test evidence" is the
  executor's own assertion, not a verified green run.
- The counts are only as complete as the graph files handed to them. A round
  abandoned without its archive kept, or an archive not passed on the command
  line, is simply absent; `graphs_read` says how many were folded in, and
  `unreadable` names every path that was passed and refused, each with the
  reason it was refused. The two together always account for every path
  given, so a thin report cannot be mistaken for a complete one, and the
  reason says whether to fix the file or point somewhere else.

  The reasons, which are every reason the loader can give:

  ```refusals
  symlink
  outside_workspace
  not_a_regular_file
  too_large
  unreadable
  malformed
  ```

It reads and writes nothing. Each node is counted once however many archives it
appears in, and **every archive's reviews are kept**: a node re-created by a
rebuild without its history would otherwise drop a recorded failure from the
count, which is the same laundering by rebuild that `core/protocol.md` says the
escape hatches exist to prevent. Where two archives disagree about a node, the
record that reached a finished status wins, because a round abandoned mid-flight
leaves nodes frozen at `running` with a higher attempt count than the successor
that verified them.

## Checking the mechanism itself

`graphctl doctor` reports the journal's size and retained months, the last
observation per client, and the interpreter behind the installed hooks.

Liveness is read from recorded sessions, not simulated, and only session
boundaries count. A `graph_transition` proves `graphctl` ran, which a person can
do by hand with no hook installed, so counting it would report a client as
observed on exactly the evidence that says nothing about its hooks.
