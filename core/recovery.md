# Recovery and failure memory

A failed review stores one structured record in the task graph, which is the
single source of truth for state and failure memory. Retrieve only relevant
entries with:

```bash
python scripts/graphctl.py failures --node NODE --limit 5
```

Retry the faulty node first. A downstream review may reopen a verified ancestor
with `--faulty-node` when that ancestor caused the observed failure. Its
descendants stay invalidated until their
dependencies are verified and each descendant is explicitly retried. Attempts
are archived before retry, and `max_attempts` prevents infinite loops. If the
limit is reached, stop and escalate rather than resetting counters.
