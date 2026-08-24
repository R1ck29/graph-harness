@AGENTS.md

# Claude Code adapter

Use `.claude/agents/reviewer.md` (`graph-reviewer`) for independent review. It
runs in plan mode, so it reports the verdict, checked criteria, and reviewer
evidence; record them verbatim with `graphctl verify --reviewer-id` from the
main session.

Run `graphctl` and `scripts/sync_adapters.py` from the repository root, not from
a package subdirectory. Project skills are generated under `.claude/skills/`;
run `python scripts/sync_adapters.py --check` before completion. Confirm the
configured hook command resolves to the supported project Python on the current
operating system. See `adapters/claude/README.md` for worktree and hook usage.
