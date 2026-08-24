# Security boundary

The core is offline and executes no task commands. `graphctl` accepts JSON and
keeps state plus failure memory in one atomically replaced graph file. CLI and
evaluation-output paths are confined to the current workspace, and symlinked
paths are rejected.

Executor and reviewer IDs enforce logical separation, not cryptographic
identity. A user who can edit the graph can forge either ID or history. Use
separate platform sessions/agents, read-only reviewer permissions, repository
ACLs, and CI review when tamper resistance matters. Never describe graph state
as an authorization or audit-signature mechanism.

Generated adapter trees reject symlinks and must exactly match canonical
skills. Claude hooks are opt-in because enabling a project hook executes local
repository code. The example hook has no network calls or write operations.

The harness does not auto-approve destructive commands, bypass sandboxes, store
secrets, or require external services. Keep credentials and sensitive evidence
outside the graph; store paths or redacted summaries instead.

Workspace confinement prevents accidental or prompt-driven writes outside the
repository. It is not a defense against another process concurrently replacing
paths inside a writable workspace; use OS isolation for hostile multi-user
workspaces. A crashed writer can leave a `.lock` file. After confirming no
writer is active, remove only the lock beside the affected graph and retry.
