# Client and operating-system compatibility

Verified on 2026-08-27 against official documentation and the installed CLIs.
The deterministic core has no platform imports, API calls, or network need.

## Operating systems

The package and installed `graphctl` entry point are tested in GitHub Actions on
Ubuntu, macOS, and Windows with Python 3.10 and 3.13. The public quick start has
separate Bash and PowerShell setup commands. Filesystem confinement rejects
symbolic links on every platform and link-like Windows reparse points,
including NTFS directory junctions, while allowing non-redirecting cloud
placeholder tags.

GitHub-hosted runners are the tested CI environment. The pinned v7 releases of
`actions/checkout` and `actions/setup-python` use the Node 24 action runtime. An
organization using GitHub Enterprise Server or self-hosted runners must confirm
that its runner version supports those action releases before reusing this
workflow.

## Codex

Local versions: login-shell `codex-cli 0.146.1`; desktop-bundled
`codex-cli 0.150.0-alpha.8`.

- Codex loads root-to-current `AGENTS.md` instructions and gives
  `AGENTS.override.md` precedence within each directory.
- Repository skills are discovered under `.agents/skills/**/SKILL.md` and load
  progressively.
- User skills are discovered under `~/.agents/skills/`, and user custom agents
  under `~/.codex/agents/`. The PC installer links both clients to one skill
  payload but keeps the Codex reviewer profile as a regular file for
  compatibility with the desktop-bundled build tested here.
- Current local releases enable multi-agent workflows; project custom agents
  live in `.codex/agents/*.toml`. The reviewer adapter is read-only.
- The CLI exposes `read-only`, `workspace-write`, and `danger-full-access`
  sandboxes plus separate approval policies. The harness does not weaken them.
- This installed CLI has no `--worktree` flag. Use client-provided agent
  isolation when available or an explicitly created Git worktree.
- A full review round trip was exercised on 2026-08-26: `codex exec --sandbox
  read-only` in a self-contained checkout read `AGENTS.md` and the generated
  skill, took the packet from `graphctl review-packet`, checked the criterion
  and the packet's `upstream_verified_files` independently, and reported a
  verdict with its own evidence and reviewer identity without running
  `graphctl verify` or writing any file. The main session then recorded that
  verdict verbatim.
- A user-scoped round trip was exercised from an unrelated temporary Git
  repository on 2026-08-27. Codex loaded the global skills, delegated the
  awaiting node to the installed `graph_reviewer`, recorded its evidence in the
  main session, and reached `{"complete": true}`. A separate read-only probe
  confirmed the desktop-bundled 0.150 build can also delegate to the regular
  global reviewer profile.

- Hooks are documented for Codex with the same event set and the same standard
  input payload as Claude Code, and `~/.codex/hooks.json` is parsed: an entry
  with an out-of-range timeout draws a clamping warning. **They did not
  execute.** Four probes on 2026-08-30 and 2026-08-31, against login-shell
  `codex-cli` 0.146.1 and the desktop-bundled 0.151.0-alpha.7.1, defined a hook
  on every documented event whose command appended a line to a file, and no
  command ever ran, under both `read-only` and writable sandboxes.

  Reproduction: write a `hooks.json` under a temporary `CODEX_HOME` giving each
  event a command such as `printf "%s\n" "&lt;event&gt;" >> /tmp/probe.log`, run
  `codex exec --skip-git-repo-check "Say OK"` with `CODEX_HOME` pointing at it,
  and read the file. The configuration is parsed and the run completes; the
  file stays empty.

  The harness installs the entries regardless, since they cost nothing while
  this holds and become live if it changes. Because a Codex session's working
  tree is therefore never observed, `graphctl conformance` recovers Codex
  sessions from `~/.codex/state_*.sqlite` — metadata only, no message content —
  and reports them as `bypass_suspected`. `graphctl doctor` reports Codex as
  never observed until a client actually runs the hooks.

Sources: [AGENTS.md](https://developers.openai.com/codex/guides/agents-md),
[skills](https://developers.openai.com/codex/skills),
[subagents](https://developers.openai.com/codex/multi-agent), and
[configuration reference](https://developers.openai.com/codex/config-file/config-reference).

## Claude Code

Local version: `2.1.239 (Claude Code)`.

- Claude Code reads `CLAUDE.md`, not `AGENTS.md`; the adapter uses the official
  `@AGENTS.md` import pattern.
- Project skills and agents live in `.claude/skills/` and `.claude/agents/`.
- User skills and agents live in `~/.claude/skills/` and `~/.claude/agents/`.
  The PC installer shares the skill payload with Codex and installs a regular
  `graph-reviewer` profile.
- Project hooks/settings live in `.claude/settings.json`. The adapter includes
  an opt-in Stop hook example that runs a deterministic local completion check
  and performs no writes.
- Custom agents support tool restrictions and `isolation: worktree`; the CLI
  also exposes `--worktree`. A worktree is edit isolation, not a security
  boundary. `task-graph.json` is untracked, so it does not appear in a new
  worktree; keep graph commands in the primary checkout.
- Agent frontmatter honors `tools`, `permissionMode`, and `disallowedTools`,
  but this release documents that `disallowedTools` is ignored while `tools` is
  set, so the reviewer relies on its `tools` list.
- Plan mode refuses the `Write` and `Edit` tools but does not stop a shell
  redirect. A probe of the project reviewer on 2026-08-25 wrote a file through
  `Bash` with no permission prompt, on a machine whose settings allow `Bash`
  broadly. No subagent tool restriction here is a security boundary; the
  caller records the verdict by convention, not because the client blocks the
  reviewer from recording it.
- Plugin packaging is supported, but v1 uses project-scoped standalone config
  to stay small. The canonical `skills/` tree can later be put in a plugin
  without changing core semantics.
- A user-scoped round trip was exercised from an unrelated temporary Git
  repository on 2026-08-27. Claude Code loaded `independent-verification`,
  delegated to the installed `graph-reviewer`, independently checked the
  one-line artifact at byte level, recorded the returned PASS in the main
  session, and reached `{"complete": true}`. The installed Stop hook then
  exited 0; the pre-existing `SessionStart` and `PostToolUse` hooks remained
  present and unchanged in count.

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
