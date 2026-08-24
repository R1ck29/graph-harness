# Platform compatibility

Verified on 2026-08-25 against official documentation and the installed CLIs.
The deterministic core has no platform imports, API calls, or network need.

## Codex

Local version: `codex-cli 0.146.1`.

- Codex loads root-to-current `AGENTS.md` instructions and gives
  `AGENTS.override.md` precedence within each directory.
- Repository skills are discovered under `.agents/skills/**/SKILL.md` and load
  progressively.
- Current local releases enable multi-agent workflows; project custom agents
  live in `.codex/agents/*.toml`. The reviewer adapter is read-only.
- The CLI exposes `read-only`, `workspace-write`, and `danger-full-access`
  sandboxes plus separate approval policies. The harness does not weaken them.
- This installed CLI has no `--worktree` flag. Use client-provided agent
  isolation when available or an explicitly created Git worktree.

Sources: [AGENTS.md](https://developers.openai.com/codex/guides/agents-md),
[skills](https://developers.openai.com/codex/skills),
[subagents](https://developers.openai.com/codex/multi-agent), and
[configuration reference](https://developers.openai.com/codex/config-file/config-reference).

## Claude Code

Local version: `2.1.239 (Claude Code)`.

- Claude Code reads `CLAUDE.md`, not `AGENTS.md`; the adapter uses the official
  `@AGENTS.md` import pattern.
- Project skills and agents live in `.claude/skills/` and `.claude/agents/`.
- Project hooks/settings live in `.claude/settings.json`. The adapter includes
  an opt-in Stop hook example that runs a deterministic local completion check
  and performs no writes.
- Custom agents support tool restrictions and `isolation: worktree`; the CLI
  also exposes `--worktree`. A worktree is edit isolation, not a security
  boundary.
- Plugin packaging is supported, but v1 uses project-scoped standalone config
  to stay small. The canonical `skills/` tree can later be put in a plugin
  without changing core semantics.

Sources: [memory and CLAUDE.md](https://code.claude.com/docs/en/memory),
[extensions](https://code.claude.com/docs/en/features-overview),
[subagents](https://code.claude.com/docs/en/sub-agents),
[hooks](https://code.claude.com/docs/en/hooks-guide),
[permissions](https://code.claude.com/docs/en/permissions), and
[plugins](https://code.claude.com/docs/en/plugins).

## Behavioral fallback

| Capability | Preferred adapter | Fallback |
| --- | --- | --- |
| Independent review | Read-only custom subagent | Fresh read-only session with only the review packet |
| Parallel writes | Native worktree-isolated agent where available | Explicit Git worktree per ready node |
| Deterministic guard | Project hook | Run `graphctl validate` and `completion-check` in CI |
| Reusable skill | Generated repository skill | Read canonical `skills/*/SKILL.md` directly |

Local help takes precedence if a future installed version differs from these
documents. Unknown features are not generated speculatively.
