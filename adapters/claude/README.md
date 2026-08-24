# Claude Code adapter

Claude Code imports the root instructions through `CLAUDE.md`, discovers
generated skills under `.claude/skills/`, and exposes the project reviewer at
`.claude/agents/reviewer.md`. The reviewer denies write tools and uses plan
permission mode.

The optional Stop hook validates `task-graph.json` and blocks an unverified
completion once per stop attempt. It performs no writes. To enable it, review
`adapters/claude/settings.example.json` and merge its `hooks` entry into
`.claude/settings.json`. It is opt-in because project hooks execute local code.
Organizations with managed hooks should register the guard centrally instead.

For write-heavy parallel nodes, use Claude Code's native `--worktree` option or
agent `isolation: worktree`. Worktrees share Git metadata and saved approvals,
so they are edit isolation rather than a security boundary.
