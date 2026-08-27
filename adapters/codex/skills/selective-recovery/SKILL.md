---
name: selective-recovery
description: Recover from a failed task-graph node by consulting compact failure memory and retrying only affected nodes.
---

# Selective recovery

Canonical source: `skills/selective-recovery/SKILL.md`. Copies under client and
adapter directories are generated; do not edit them directly.

1. Query only failures relevant to the faulty node or failure type.
2. Confirm ancestors and unrelated branches remain verified.
3. Retry the faulty node; do not reset attempt counters.
4. After it is verified, retry only invalidated descendants whose dependencies
   are verified.
5. Escalate when `max_attempts` is reached.
