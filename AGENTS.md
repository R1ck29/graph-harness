# Graph Engineering harness

This repository turns coding objectives into a verified task graph. Before
non-trivial work, read `core/protocol.md` and validate `task-graph.json` with
`python scripts/graphctl.py validate`.

- Use only `graphctl` for graph state changes.
- Execute ready nodes; do not rework verified ancestors.
- Do not call a node complete without criterion-linked evidence and an
  independent PASS review.
- Use `roles/` and the relevant on-demand skill for detailed procedures.
- Run `python -m unittest discover -v` and `graphctl completion-check` before
  declaring the objective complete.
