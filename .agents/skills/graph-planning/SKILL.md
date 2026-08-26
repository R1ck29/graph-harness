---
name: graph-planning
description: Convert a non-trivial coding objective into a validated dependency graph before implementation.
---

# Graph planning

Canonical source: `skills/graph-planning/SKILL.md`. Copies under client and
adapter directories are generated; do not edit them directly.

1. Read `core/protocol.md` and `roles/planner.md`.
2. Split the objective into observable outcomes, not activity lists.
3. Create the first node with `graphctl init --objective "..." --criterion
   "..."`, then add each further node with `graphctl add-node NODE
   --description "..." --criterion "..." --depends-on EXISTING`. Add a node
   only after the nodes it depends on. Never hand-edit the graph file.
4. Add dependencies only when an output is truly required downstream.
5. Give every node acceptance criteria and `max_attempts` of two by default.
6. New nodes start `blocked`; validation promotes eligible roots to `ready`.
7. Run `python scripts/graphctl.py validate task-graph.json` and then `ready`.

Do not start implementation until the graph is valid.
