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

A session changed something when `HEAD` moved **or** the working-tree digest
changed. Both terms are required: committing returns the tree to the state the
digest already recorded, so the digest alone misses the most deliberate kind of
change, while `HEAD` alone misses uncommitted work.

The digest covers the porcelain status, the staged and unstaged diffs, and the
contents of untracked files up to `MAX_UNTRACKED_BYTES`. Status is read in its
NUL-separated form, because git's default `core.quotepath` escapes any path
holding non-ASCII bytes and a parser reading the newline form resolves an
escape string that names no file — which made edits to files with Japanese
names invisible.

This is deliberately independent of how the edit was made. Counting a client's
editing tool calls would miss every change made through the shell, which is
exactly how a session that ignores the protocol is likely to work.

## Repository identity

Records about one session are written by three separate processes. They are
matched by a single string, so every producer derives it the same way: git's
top level for the directory, then resolved. The top level matters because the
hooks start from the project directory while `graphctl` starts from wherever it
was run; resolution matters because a project reached through a symlink — the
normal case under `/tmp` and `/var` on macOS — otherwise yields two names for
one directory.

## Verdicts

`graphctl conformance` folds the records into one verdict per session.

| Verdict | Condition |
| --- | --- |
| `conformant` | changed, and a `graphctl` run fell inside its window |
| `bypass` | changed, and none did |
| `read_only` | neither `HEAD` nor digest moved; excluded from the rate |
| `incomplete` | opened, never ended |
| `unobserved` | no opening record, or an observation that degraded |
| `bypass_suspected` | a Codex session, whose working tree cannot be observed |

Each verdict carries `confidence`, `contested` when another session in the same
repository overlapped it, and `changed_files` / `changed_lines`. The rate counts
only `conformant` and `bypass`: a session that changed nothing says nothing
about whether the protocol was followed.

Sizes are differences between the two snapshots. A tree that was already dirty
when a session started is not that session's doing. Committed work is sized
from `git diff --shortstat` between the two recorded commits, which is what
keeps a pull, a checkout, or a one-line commit below the threshold.

A recorded commit name is refused unless it is forty hexadecimal characters.
The journal is forgeable, and an option-shaped value would otherwise reach git
as an argument.

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

- Only changes of at least `WARN_MIN_FILES` files or `WARN_MIN_LINES` lines are
  mentioned. Everything is recorded; a harness that objects to a one-line fix
  teaches people to ignore it.
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

It says nothing when the observation itself failed. An accusation drawn from a
degraded snapshot is worse than silence, so a degraded endpoint is reported as
`unobserved` rather than guessed at.

## Checking the mechanism itself

`graphctl doctor` reports the journal's size and retained months, the last
observation per client, and the interpreter behind the installed hooks.

Liveness is read from recorded sessions, not simulated, and only session
boundaries count. A `graph_transition` proves `graphctl` ran, which a person can
do by hand with no hook installed, so counting it would report a client as
observed on exactly the evidence that says nothing about its hooks.
