# Integration guide

## Stable core

Treat `agent_harness/`, `core/`, and `scripts/graphctl.py` as the portable
contract. Do not add organization paths, credentials, databases, APIs, or
platform-specific feature names there. Extend behavior through documented
evidence kinds and adapter wrappers instead of editing state fields directly.

## Platform adapters

- Codex: `AGENTS.md`, `.codex/`, `.agents/skills/`, and `adapters/codex/`.
- Claude Code: `CLAUDE.md`, `.claude/`, and `adapters/claude/`.
- `skills/` is the single source of truth. Generated copies must pass
  `python scripts/sync_adapters.py --check`.
- Never edit a generated skill under `.agents/`, `.claude/`, or `adapters/`
  directly. Edit `skills/<name>/SKILL.md`, run the sync command, and review the
  generated diff. The adapter-local copies are retained so each adapter can be
  distributed independently without handwritten procedure forks.

## User-scoped PC integration

`scripts/install_pc.py` is the supported integration path when both clients
must use the harness outside this repository. It owns one payload and one
isolated Python runtime under the selected user's `.local/share` directory.
Both clients reference the same canonical skill payload on POSIX systems;
client-specific reviewer formats remain separate regular files.

The installer performs a complete collision preflight before changing client
configuration. It atomically upserts one marked block in Codex `AGENTS.md` and
Claude `CLAUDE.md`, merges one hook entry per managed event, writes a hash
receipt, and backs up only configuration files that actually change. The
managed events are `SessionStart`, `Stop`, `SessionEnd`, and `PostToolUse`;
only the last carries a matcher, so the edit hook runs after editing tools
and not after reads or searches. Re-running it updates the owned payload
without duplicating blocks, hooks, or backups. It never replaces an unmanaged
same-name skill or reviewer.

An entry is recognised as the installer's own by the command it runs, which
is an absolute path into the runtime the installer built. That has a
consequence worth knowing before you edit one: if you change the timeout or
the arguments of a managed entry by hand, the next install **replaces** it
and your edit is gone. Replacing rather than appending is deliberate — a
second entry would run the hook twice for every edit — and the install now
prints `replacing edited harness hook in <file>: <event> -> <command>` for
each one it overwrites, so the revert is not silent. `--check` reports the
same edit as `hook drift` and changes nothing. To keep a different timeout,
change it in the installer rather than in `settings.json`.

The global procedure is intentionally short and refers to the existing
organization workflow instead of restating planning, TDD, review, or security
rules. Organization-specific MCP servers, permissions, agents, and hooks stay
outside the managed block and remain untouched.

## Extension points

- Add a role procedure under `roles/`, keeping four active roles or fewer per
  task.
- Add a canonical skill under `skills/<name>/SKILL.md`, then run the sync tool.
- Add organization acceptance criteria in planner templates or a wrapper that
  validates evidence before invoking `graphctl verify`.
- Add read-only DB or MCP integrations only in platform adapters. Convert their
  output into bounded evidence objects; never store secrets in the graph.
- Add evaluators as new cases or runners under `evals/`; preserve the result
  fields so baseline and graph runs remain comparable.

## Organization-specific skills

Keep procedures vendor-neutral when possible. If a skill requires a platform
tool, keep the shared decision logic in canonical `skills/` and put only the
tool invocation in the relevant adapter. Never maintain two handwritten copies
of the same procedure.

## Minimal migration

1. Copy the stable core, roles, canonical skills, scripts, and tests.
2. Merge the short root instruction file with the target harness entrypoint.
3. Choose and merge only the adapters used by the organization.
4. Register organization skills and integrations at adapter scope.
5. Add policy acceptance criteria and evidence validators.
6. Run unit tests, sync drift check, adapter validation, and the five offline
   eval cases.
7. Pilot on non-sensitive repositories before enabling broader execution.

No migration requires a graph database, service, external API, or web UI.
Failure history intentionally remains inside the graph so integrations never
have to coordinate commits across two state files.
