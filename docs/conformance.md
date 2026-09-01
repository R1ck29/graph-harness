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

A session changed something when it produced an `edit` record, **or** some
boundary holds a dirty path its first boundary did not, **or** the same paths
hold different content.

Moving the tree is not the same as adding to it. `git checkout -- .`,
`git clean` and a hard reset over dirt that was already there all change the
digest while authoring nothing.

The comparison is per path, not by counting. Netting the counts made a session
that removes more than it adds look like it authored nothing: ten junk files
deleted and one real feature written leaves *fewer* dirty files than it started
with. Paths are recorded as hashes, capped at `MAX_SNAPSHOT_PATHS`; beyond that
the snapshot says it was truncated and the coarser count comparison is used.

The content clause has a deliberate guard: it applies only when the path set
did not shrink. Discarding dirt also changes the digest, and without the guard
that would be work again.

`HEAD` is recorded and reported as context. **It is never evidence.** A head
move says something changed and cannot say who changed it or why: authoring,
pulling, checking out, rebasing, resetting and a colleague's commit in another
terminal are one observation. Judging on it reported anyone who ran `git pull`
as having bypassed the protocol.

Work the session committed itself is still seen, because the comparison runs
over every boundary rather than over the outer pair, and one of those
boundaries saw the tree dirty.

The digest covers the porcelain status, the staged and unstaged diffs, and the
contents of untracked files up to `MAX_UNTRACKED_BYTES`. Submodules are
excluded with `--ignore-submodules=all`: a submodule update moves a pointer
git maintains, and nobody authored anything in this repository. Lines in
created files are counted while their bytes are read for the digest, because
untracked content never reaches `git diff` and a session that wrote ten
thousand new lines was otherwise announced as changing none. Status is read in its
NUL-separated form, because git's default `core.quotepath` escapes any path
holding non-ASCII bytes and a parser reading the newline form resolves an
escape string that names no file — which made edits to files with Japanese
names invisible.

The two terms cover each other. An edit event names the session whose agent did
the writing, which no comparison of a shared working tree can do. The digest
covers changes made through the shell — `sed`, a heredoc, a script — which is
exactly how a session that ignores the protocol is likely to work, and which no
count of a client's tool calls would see.

## Not judgeable

A snapshot taken while git has a merge, rebase, cherry-pick, revert or bisect
open is not evidence, exactly as a degraded one is not: the tree is full of
content git put there. The session is reported `unobserved`.

Detection uses only markers git **removes when the operation ends** — the
`rebase-merge` and `rebase-apply` directories, `MERGE_HEAD`,
`CHERRY_PICK_HEAD`, `REVERT_HEAD` and `BISECT_LOG`. `REBASE_HEAD` is
deliberately not among them: git 2.50.1 leaves it behind after a *completed*
rebase, where it survives later commits, a checkout, a merge and a `gc` and is
cleared only by the next rebase. Treating it as an open operation silenced
every session in any repository where a rebase conflict had ever been
resolved — including sessions whose edits were recorded by tool events, which
no stale file makes unreliable. Git 2.21 removes the file, so a suite running
only that git could not see the difference; there is now a test that runs the
whole cycle under a second git when the machine has one.

Aborting the operation does not repair it, and the session is still not judged.
The size charged is a maximum across boundaries, so a single boundary taken
during a conflict would otherwise accuse a session that pulled, hit a conflict,
ran `git merge --abort`, and left the repository byte-for-byte as it found it.

## What this cannot see

Three gaps, stated because a reader who believes there are none will trust a
number that has not earned it. Each is pinned by a test.

**Work committed with no turn boundary in between.** A shell edit committed
inside a single turn leaves every snapshot clean and produces no edit event.
This is not narrow: committing before *every* turn boundary hides a whole
session's work the same way, and `graphctl conformance` then classifies those
sessions `read_only` and drops them from the rate's denominator entirely.

**Edits under an ignored path.** Git never reports them, so no snapshot sees
them.

**Content that appears without being written.** `git stash pop`, `git apply`,
`cherry-pick -n`, `git checkout BRANCH -- PATH`, `git worktree add` inside the
repository, and build output that is not gitignored all put content in the tree
that nobody typed. None leaves a marker git removes, and no observation of the
tree alone distinguishes them from authoring, so all of them **are** reported.
This is the residual false-positive family, and it is the price of keeping the
shell term at all.

Everything else here fails towards silence. Silence is the direction this
module errs in, because a warning that fires on `git pull` is a warning people
learn to ignore.

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
| `conformant` | changed, and a `graphctl` run fell inside its window |
| `bypass` | changed, and none did |
| `read_only` | no edit event, and nothing added to the tree; excluded from the rate |
| `incomplete` | opened, never ended |
| `unobserved` | no opening record, an observation that degraded, or a boundary taken while git had an operation open |
| `bypass_suspected` | a Codex session, whose working tree cannot be observed |

Each verdict carries `confidence`, `contested` when another session in the same
repository overlapped it, and `changed_files` / `changed_lines`. The rate counts
only `conformant` and `bypass`: a session that changed nothing says nothing
about whether the protocol was followed.

Size is a magnitude, never the reason for reporting. It is the number of
distinct `path_id` values a session recorded, and the largest difference in
changed files and lines between any boundary and the session's first one. The
first boundary is the baseline because a tree that was already dirty when a
session started is not that session's doing, and the maximum is taken because a
session that commits its work returns the tree to clean before it ends.

A session whose work was committed with no turn boundary in between is reported
with a line count of zero: the file count comes from its edit records, and no
snapshot ever saw the tree dirty.

A tree that someone outside the harness dirties while a session is open is
charged to that session. `contested` marks only overlaps between sessions this
harness observed, so a person editing in another window is invisible to it.

No recorded `HEAD` is passed to git as an argument any more, because no
recorded `HEAD` is used at all. The journal is forgeable by anyone who can
write the user's home, and not consuming a forgeable value is a stronger
guarantee than validating it.

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
