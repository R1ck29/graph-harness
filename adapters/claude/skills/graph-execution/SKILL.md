---
name: graph-execution
description: Execute one ready task-graph node and submit criterion-linked evidence without self-review.
---

# Graph execution

1. Select only a node printed by `graphctl ready`.
2. Start it with a stable executor identity.
3. Read its objective, dependencies, acceptance criteria, and relevant failures.
4. Implement the smallest complete change and run focused checks.
5. Submit evidence for each criterion using `graphctl submit`.
6. Stop implementation and hand the review packet to a separate reviewer.

Never write graph state by hand or mark your own node verified.
