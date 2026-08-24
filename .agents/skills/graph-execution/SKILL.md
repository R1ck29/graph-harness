---
name: graph-execution
description: Execute one ready task-graph node and submit criterion-linked evidence without self-review.
---

# Graph execution

Canonical source: `skills/graph-execution/SKILL.md`. Copies under client and
adapter directories are generated; do not edit them directly.

1. Select only a node printed by `graphctl ready`.
2. Start it with a stable executor identity.
3. Read its objective, dependencies, acceptance criteria, and relevant failures.
4. Implement the smallest complete change and run focused checks.
5. Submit evidence for each criterion using `graphctl submit`.
6. Stop implementation and hand the review packet to a separate reviewer.
7. If the result is UNCERTAIN, withdraw as the original executor, strengthen
   the evidence, and submit again without opening a new attempt.

Never write graph state by hand or mark your own node verified.
