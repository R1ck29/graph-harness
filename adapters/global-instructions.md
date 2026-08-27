## Graph Engineering harness

For non-trivial implementation work (three or more steps or an architectural
decision), load `graph-planning` before editing and keep the executable plan in
`task-graph.json`. Use `{graphctl}` as the only graph-state writer. Execute only
ready nodes, attach criterion-linked evidence, and delegate each submitted node
to the installed graph reviewer (`graph_reviewer` in Codex, `graph-reviewer` in
Claude Code). A task is complete only after independent PASS records and
`{graphctl} completion-check` succeed. Use `selective-recovery` after a FAIL;
do not rerun verified, unaffected work.
