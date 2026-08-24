# Codex adapter

Codex reads the root `AGENTS.md`, discovers generated skills under
`.agents/skills/`, and loads the project-scoped read-only reviewer from
`.codex/agents/reviewer.toml` on versions that support custom subagents.

Ask Codex to delegate review to `graph_reviewer`. If subagents are unavailable,
start a fresh isolated Codex session in read-only sandbox mode and give it only
the review packet and relevant evidence. The local CLI does not expose a native
worktree flag; use the app's agent isolation when available or create an
external Git worktree explicitly.

Recommended permissions are `workspace-write` for an executor and `read-only`
for a reviewer. Never bypass approval or sandbox protections merely to run the
harness.
