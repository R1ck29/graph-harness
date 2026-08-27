---
name: independent-verification
description: Independently verify an awaiting task node from a compact evidence packet and return PASS, FAIL, or UNCERTAIN.
---

# Independent verification

Canonical source: `skills/independent-verification/SKILL.md`. Copies under
client and adapter directories are generated; do not edit them directly.

1. Use a fresh agent context distinct from the executor.
2. Inspect the original objective, node criteria, relevant files, diff, tests,
   and current-attempt evidence only.
3. When the node changed a file listed under `upstream_verified_files`,
   confirm that verified ancestor still holds; a PASS is a point-in-time
   record, not a lock on the files behind it.
4. Return PASS only when every criterion is supported, and supply explicit
   reviewer criteria and evidence to `graphctl verify`.
5. On FAIL, identify the observed criterion, evidence, faulty node, affected
   descendants, and minimal retry recommendation. If the faulty node is an
   ancestor, also map the failure to that node's acceptance criterion.
6. Record the verdict through `graphctl verify`; do not edit implementation.
   When the reviewer runs as a delegated review context, it reports the verdict,
   checked criteria, and reviewer evidence instead, and the caller records them
   verbatim under the reviewer identity. Apply this whenever the reviewer is a
   subagent, even if its tools would technically permit the write: a self-issued
   review record defeats the separation this step exists to create.
7. Run `graphctl` from the directory that holds `task-graph.json`; a graph in a
   parent directory is rejected as outside the workspace.
