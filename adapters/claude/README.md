# Claude Code adapter

Claude Code imports the root instructions through `CLAUDE.md`, discovers
generated skills under `.claude/skills/`, and exposes the project reviewer at
`.claude/agents/reviewer.md`.

The reviewer's `tools` list omits `Write`, `Edit`, and `NotebookEdit`, and plan
permission mode blocks file writes. It keeps `Bash` to inspect sources and run
tests, so it is a review posture rather than an enforced sandbox; the operating
environment supplies the real boundary. `disallowedTools` is kept as a fallback
for anyone who removes the `tools` list, but Claude Code ignores it while
`tools` is set.

Because plan mode refuses writes, the reviewer reports its verdict, criteria,
and evidence to the caller instead of writing the graph itself. The caller
records it verbatim with `graphctl verify` using the reviewer identity. Under a
client that lets the reviewer run its own writes, let the reviewer call
`graphctl verify` directly.

Run `graphctl` from the directory holding `task-graph.json`. The workspace is
the current directory, so a graph in a parent directory is rejected as outside
the workspace even when named by an absolute path.

The optional Stop hook validates `task-graph.json` and blocks an unverified
completion once per stop attempt. It performs no writes. To enable it, review
`adapters/claude/settings.example.json` and merge its `hooks` entry into
`.claude/settings.json`. It is opt-in because project hooks execute local code.
Organizations with managed hooks should register the guard centrally instead.

The settings example uses `python`. Before enabling it, run `python --version`
and confirm it resolves to Python 3.10 or newer. On Windows, replace `python`
with `py -3` when the Python launcher is the supported entry point. For the most
predictable setup, replace it with the absolute interpreter from the same
project virtual environment used to install the harness, such as
`.venv/bin/python` on macOS/Linux or `.venv\\Scripts\\python.exe` on Windows.
Run the resulting hook command manually from the repository after
`completion-check` succeeds; enable the setting only after it exits with code 0.

If the hook command cannot start, Claude Code reports the failure and still
stops, so treat the guard as a reminder and keep `completion-check` in CI.

For write-heavy parallel nodes, use Claude Code's native `--worktree` option or
agent `isolation: worktree`. Worktrees share Git metadata and saved approvals,
so they are edit isolation rather than a security boundary.

`task-graph.json` is ignored by Git, so a new worktree does not receive it.
Keep the graph in the primary checkout and run every `graphctl` command there;
use the worktree only for the file edits of one node. A worktree with no graph
makes the Stop hook exit successfully without checking anything.
