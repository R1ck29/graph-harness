---
name: graph-planning
description: Convert a non-trivial coding objective into a validated dependency graph before implementation.
---

# Graph planning

1. Read `core/protocol.md` and `roles/planner.md`.
2. Split the objective into observable outcomes, not activity lists.
3. Add dependencies only when an output is truly required downstream.
4. Give every node acceptance criteria and `max_attempts` of two by default.
5. Start root nodes as `blocked`; validation promotes eligible roots to `ready`.
6. Run `python scripts/graphctl.py validate task-graph.json` and then `ready`.

Do not start implementation until the graph is valid.
