# How this works

This explains the whole repository in plain language. No programming knowledge
is assumed. If you only read one section, read the first two.

## The problem it solves

When you ask an AI assistant to change some code, you are trusting it twice.
Once that it did the work, and once that it checked its own work. The second
kind of trust is the weaker one. An assistant that has made a mistake is
usually the last thing that will notice.

This repository is a way of not having to take either on faith. It does three
things:

1. It makes the assistant write down what it is going to do, in small pieces,
   before it starts.
2. It refuses to call a piece finished until somebody other than the assistant
   that did it has checked it and said so.
3. It notices when a session skipped all of that.

Think of it as the difference between a colleague saying "it's done" and a
colleague showing you a checklist with someone else's initials on each line.

## The two halves

### The plan: a task graph

Before doing anything non-trivial, the assistant breaks the job into small
tasks and writes them into one file, `task-graph.json`. Each task carries:

- a description of what will be true when it is finished,
- a list of the specific things that must be checked, called its acceptance
  criteria,
- a list of the other tasks it depends on.

Because tasks depend on each other, they form a shape — hence "graph". A task
cannot be started until everything it depends on has been finished and
checked. This is the same discipline as not painting a wall before the plaster
has dried.

Nothing edits that file by hand. All changes go through one program,
`graphctl`, which refuses anything that would break the rules. That is the
point: the assistant cannot mark its own work finished by editing a file,
because the program will not let it.

### The check: independent review

When a task is done, the assistant submits evidence — which test proved which
criterion, and what it observed. A **separate** reviewer, which does not share
the first one's reasoning or its assumptions, is given only that evidence and
the files involved, and has to reach its own conclusion. It reports PASS, FAIL,
or UNCERTAIN, in its own words, with its own evidence.

Only then does `graphctl` record the task as verified — and only if the
reviewer's name is different from the worker's, and only if every criterion has
evidence attached. If the reviewer says FAIL, the task and everything that
depended on it are reopened, and the reason is kept.

This is not a formality. During the work that produced this very document, the
reviewers found and forced the repair of, among others: a bug that made changes
to files with Japanese names invisible; a bug where the tool that was supposed
to only *read* a database quietly *created* one; and a bug where a forged
record could make the system run a command that wrote a file anywhere on disk.
None of those were found by the assistant that wrote the code.

## The third part: noticing when nobody used any of this

All of the above only helps if it is actually used. An assistant that ignores
the whole system leaves no trace in it — which is exactly the problem. The
absence of a record looks identical to a quiet day.

So the harness also watches from outside. Every time an assistant session
starts, ends, or finishes a turn, a small program records four things:

- which assistant it was, and which session,
- which project it was working in,
- a fingerprint of the project's files at that moment,
- nothing else. No code, no conversation, no file contents.

There is also a record every time the assistant uses an editing tool: which
session, and a *hash* of which file — never the filename itself, because a
filename can name a customer.

That last record is the one that matters, and it took six rounds of review to
learn why. Comparing fingerprints tells you a project changed; it cannot tell
you *who* changed it. Every attempt to guess the missing half accused somebody
who had merely run `git pull`, or `git stash pop`, or updated a submodule. So
the judgement now rests only on the record of the assistant actually writing a
file, and the fingerprints are kept as context for a person to read.

The honest cost: if the assistant edits by running shell commands instead of
using its editing tools, nothing records it, and the session is reported as
`unattributed` — "nobody can say" — rather than as a clean one.

### What you can ask it

```
graphctl conformance
```

This prints one line of judgement per session:

| Verdict | What it means |
| --- | --- |
| `conformant` | The session changed code and used the process. |
| `bypass` | The session changed code and did not use the process. |
| `unattributed` | Nothing recorded says who changed what. A question, or shell-only work. |
| `incomplete` | The session was interrupted before it finished. |
| `unobserved` | Something prevented a reliable observation. |
| `bypass_suspected` | The session ran, but its work could not be observed. |

Plus a rate: of the sessions that changed something and could be judged, what
fraction used the process.

```
graphctl effectiveness
```

This answers the question the whole repository is an argument about: **is any
of this worth the trouble?** It reads back what happened to every task ever
recorded here and counts the outcomes — how many tasks passed on the first
attempt, how much rework there was, how often a budget ran out, and above all
how many times an independent reviewer rejected work whose author had already
run the tests and reported them green.

That last figure is the case for the whole arrangement, and this page
deliberately does not quote it. Run the command and read
`"failed_despite_test_evidence"` and `"nodes_with_such_a_failure"` off for
yourself: the count moves every time a review records a verdict, and a figure
printed here would be stale by the next one. It was already stale twice while
this paragraph was being written.

Two rejections it counts: a warning that fired at anyone who ran `git pull`, and
a stale file left by one version of git that silenced every session in a
repository. Neither was caught by a test suite that was green at the time.

Review also turns up things it does *not* reject a task for. A reviewer once
noticed that a *read* of a file was being recorded as a *write* — real, and
fixed — but it said so as a side note inside a review it passed, so it is not
one of the rejections counted above. The distinction is kept here because a
report that quietly widened "what review caught" to include everything a
reviewer ever mentioned would be doing the thing this whole repository exists
to prevent.

Every rate is printed next to the count it was taken over, because with three
projects on record these are anecdotes and a reader should be able to see that.
The rate on that figure is deliberately not called a catch rate: its
denominator is *rejections*, not *defects*, and a defect the reviewer also
missed is recorded nowhere, so it can never mean "review finds everything".

```
graphctl doctor
```

This answers a different question: is the watching itself working? It reports
when each assistant was last actually seen, read from real records rather than
from configuration. A tool that is set up but never runs will say so.

## What it deliberately does not claim

**It is not proof, and it is not an audit trail.** Every record it keeps lives
in an ordinary file in your home directory. Anyone who can edit that file can
add records, remove records, or change them. The harness is a way to notice
drift and mistakes, not a way to catch someone determined to hide something.
The documentation says this in several places on purpose, so that nobody builds
a stronger claim on top of it.

**It cannot judge whether work was important.** It can say a session changed
two files and forty lines. It cannot say whether that was a typo fix or a
redesign. So it reports the size and leaves the judgement to you, and it stays
quiet about small changes rather than complaining about every one.

**It says nothing when it is not sure.** If the observation failed — the
project was not under version control, or the check ran out of time — the
session is reported as unobserved rather than guessed at. An accusation based
on a failed measurement is worse than silence.

**It never blocks you.** The strongest thing it does is print one line the next
time you open a session. Work is never refused.

## Where things live

| Place | What is there |
| --- | --- |
| `task-graph.json` | The current plan and its status. Not shared; not committed. |
| `core/` | The rules, written down independently of any assistant. |
| `agent_harness/` | The program that enforces them. |
| `roles/`, `skills/` | Instructions for each job: planning, doing, reviewing. |
| `adapters/`, `.claude/`, `.codex/` | The same behaviour translated for each assistant. |
| `docs/` | This and the other explanations. |
| `~/.local/share/graph-engineering-agent-harness/` | The observation records. |

## Two assistants, one difference worth knowing

The harness supports Claude Code and Codex. It installs the same three watchers
into both, because both document the same way of accepting them.

On the machines this was tested on, **Codex accepts the configuration and then
never runs it.** Four separate attempts across two versions confirmed it. So
Codex sessions cannot be observed the way Claude Code sessions can. Rather than
guessing, the harness reads Codex's own record of which sessions ran and where,
and reports those as *suspected* — a session that happened, whose work could
not be seen.

`graphctl doctor` will keep saying that Codex has never been observed until
that changes. That is the honest answer, and it was worth building the machine
to be able to give it.

## If you are setting this up

`README.md` has the installation steps. `docs/security.md` describes exactly
what is recorded and what is not, and the limits above in more detail.
`docs/conformance.md` describes the watching in technical terms.
