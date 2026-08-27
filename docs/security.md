# Security boundary

The core is offline and executes no task commands. `graphctl` accepts JSON and
keeps state plus failure memory in one atomically replaced graph file. CLI and
evaluation-output paths are confined to the current workspace. Symbolic links
and link-like Windows reparse points, including directory junctions, are
rejected; non-redirecting cloud placeholder tags remain usable.

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
project hook executes local repository code. The example hook has no network
calls or write operations.

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

The global Claude Stop hook runs the installed `graphctl-claude-stop` entry
point from a dedicated Python environment. It does not import code from the
repository being reviewed. It returns immediately when that repository has no
`task-graph.json`; when a graph exists it applies the same bounded, offline
completion check as the project adapter.

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

Workspace confinement prevents accidental or prompt-driven writes outside the
repository. It is not a defense against another process concurrently replacing
paths inside a writable workspace; use OS isolation for hostile multi-user
workspaces. A crashed writer can leave a `.lock` file. Run `graphctl doctor`
first to display the exact lock path and recorded process ID. After confirming
no writer is active, remove only that lock and retry; the harness intentionally
does not delete locks automatically.
