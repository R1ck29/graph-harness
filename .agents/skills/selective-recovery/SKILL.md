---
name: selective-recovery
description: Recover from a failed task-graph node by consulting compact failure memory and retrying only affected nodes.
---

# Selective recovery

1. Read `core/recovery.md`.
2. Query only failures relevant to the faulty node or failure type.
3. Confirm ancestors and unrelated branches remain verified.
4. Retry the faulty node; do not reset attempt counters.
5. After it is verified, retry only invalidated descendants whose dependencies
   are verified.
6. Escalate when `max_attempts` is reached.
