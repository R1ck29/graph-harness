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
