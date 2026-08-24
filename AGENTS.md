# Graph Engineering harness

This repository records structured objectives in a verified task graph. Before
non-trivial work, read `core/protocol.md`. If `task-graph.json` is missing, use
`graphctl init --objective "..." --criterion "..."` or copy an appropriate
example; otherwise validate it with `graphctl validate`.

Use the installed `graphctl` entry point. In an uninstalled source checkout,
replace `graphctl` with `python scripts/graphctl.py`.

- Run `graphctl` from the directory that holds `task-graph.json`. The workspace
  is the current directory, so a graph in a parent directory is rejected as
  outside the workspace even when named by an absolute path.
- Use only `graphctl` for graph state changes.
- Execute ready nodes; do not rework verified ancestors.
- Do not call a node complete without criterion-linked evidence and an
  independent PASS review with explicit reviewer criteria and evidence.
- Keep credentials, customer data, and unredacted client material out of graph
  evidence; use repository-relative artifact references or redacted summaries.
- Use `roles/` and the relevant on-demand skill for detailed procedures.
- Run `python -m unittest discover -v` and `graphctl completion-check` before
  declaring the objective complete.
