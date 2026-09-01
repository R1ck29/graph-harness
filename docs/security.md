# Security boundary

The core is offline and executes no task commands. `graphctl` accepts JSON and
keeps state plus failure memory in one atomically replaced graph file. CLI and
evaluation-output paths are confined to the current workspace. Symbolic links
and link-like Windows reparse points, including directory junctions, are
rejected; non-redirecting cloud placeholder tags remain usable.

One kind of state is deliberately outside the workspace. Session observations
are appended beneath the selected home, in the installer's own directory, under
the same rules the installer follows: paths are confined to that directory,
link-like components are rejected, and nothing else is written. They are kept
there because a session that never creates a graph has no workspace to record
into, which is exactly the case they exist to make visible.

Executor and reviewer IDs enforce logical separation, not cryptographic
identity. A user who can edit the graph can forge either ID or history. Use
separate platform sessions/agents, read-only reviewer permissions, repository
ACLs, and CI review when tamper resistance matters. Never describe graph state
as an authorization, audit-signature mechanism, or proof that a separate human
or agent actually performed the review. A PASS requires reviewer-supplied
criteria and evidence, but the surrounding platform must enforce the fresh
context and read-only reviewer boundary.

A subagent tool list is not that boundary. Removing `Write` and `Edit` from a
reviewer still leaves `Bash`, which can write through a shell redirect; a probe
of the Claude reviewer on 2026-08-25 did exactly that with no permission
prompt, under plan permission mode. The bundled reviewers keep `Bash` so they
can read sources and run tests, and they are instructed not to record their own
verdict. Where a forged self-review is part of the threat model, run the
reviewer under an OS-level or container sandbox, or a separate account, rather
than relying on agent configuration.

Generated adapter trees reject symlinks and link-like Windows reparse points and
must exactly match canonical skills. Claude hooks are opt-in because enabling a
project hook executes local repository code. The example project hook makes no
network calls and writes nothing.

The installed session hooks do write: each appends one bounded record to the
observation journal beneath the home. They write nothing else, make no network
call, and read no file in the repository beyond what `git` reports about it.
A failure to write degrades to writing nothing; it never changes a hook's exit
code or a command's result.

The PC installer only writes beneath the selected user home, rejects symlinked
configuration parents, refuses unmanaged same-name targets before any client
configuration change, and uses atomic replacement for files it owns. Existing
configuration is parsed before mutation and backed up when it changes. On
POSIX, skills link to one owned payload; client agent definitions are regular
files because the desktop-bundled Codex tested here failed to apply a linked
profile. Uninstall removes
an owned link only when it still resolves to the managed payload and removes a
regular reviewer only when its hash still matches, so a user replacement is not
silently deleted.

The global session hooks run installed entry points from a dedicated Python
environment. They do not import code from the repository being observed. The
Stop hook records the working tree at the turn boundary and then, when the
repository has a `task-graph.json`, applies the same bounded, offline
completion check as the project adapter; when there is no graph it records the
turn and returns success. Recording happens before the check and cannot change
its verdict.

The session-start hook additionally reports, on standard error, when the
previous session in that repository changed code without recording any
task-graph state. It never blocks a session, and it reports a given session at
most once. Sizes come from `git`, and a recorded commit name is refused unless
it is a forty-character hexadecimal sha, so a forged record cannot turn into a
git argument.

The harness does not auto-approve destructive commands, bypass sandboxes, or
require external services. It does persist submitted and reviewer evidence in
the graph, archives prior-attempt evidence during retry, and archives each
withdrawn UNCERTAIN submission with its evidence in `review_history` until the
bounds in `core/verification.md` discard the oldest records. Never put passwords,
API keys, customer names, contract terms, personal data, or unredacted client
material in `--evidence` or an evidence JSON file. Command arguments may also be
retained by shell history or visible to local process inspection. Store only
repository-relative artifact references or redacted summaries, and apply the
organization's retention and deletion policy to graph files.

The default `/task-graph.json` is ignored by Git. A graph saved under another
name or directory is not automatically ignored; check `git status` before every
commit and add the chosen graph path to `.gitignore` when it contains internal
operational records.

## What the edit hook records

The `PostToolUse` hook records that an editing tool ran: the client, the
session, the repository root, the tool's name, and `path_id` — the first twelve
hexadecimal characters of the SHA-256 of the edited path relative to the
repository.

**The path itself is never recorded, and neither is any file content.** A path
is content enough: a filename can name a customer, so `clients/acme/contract.md`
would leak one by existing in a log. The hash counts how many distinct files a
session touched and identifies none of them. Nothing in the payload a client
sends — the edit's old text, its new text, the file's contents — is read or
stored.

The same rule governs the working-tree snapshot beside it, which records a
digest, counts, and `HEAD`, and no path.

Session observations are not an audit trail and must never be described as
one. They live in ordinary files that any process running as the user can
append to, rewrite, or delete, exactly as the executor and reviewer identities
can be forged. They record repository paths, timestamps, digests and change
counts; they never record file contents, prompts, transcripts, or any part of
a conversation. Retention is bounded to `MAX_JOURNAL_MONTHS` whole months and
`graphctl conformance --prune` removes what is older. Apply the organization's
retention and deletion policy to them as to graph files.

Workspace confinement prevents accidental or prompt-driven writes outside the
repository. It is not a defense against another process concurrently replacing
paths inside a writable workspace; use OS isolation for hostile multi-user
workspaces. A crashed writer can leave a `.lock` file. Run `graphctl doctor`
first to display the exact lock path and recorded process ID. After confirming
no writer is active, remove only that lock and retry; the harness intentionally
does not delete locks automatically.
