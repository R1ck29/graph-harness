---
name: independent-verification
description: Independently verify an awaiting task node from a compact evidence packet and return PASS, FAIL, or UNCERTAIN.
---

# Independent verification

1. Read `core/verification.md` and `roles/reviewer.md`.
2. Use a fresh agent context distinct from the executor.
3. Inspect the original objective, node criteria, relevant files, diff, tests,
   and current-attempt evidence only.
4. Return PASS only when every criterion is supported.
5. On FAIL, identify the observed criterion, evidence, faulty node, affected
   descendants, and minimal retry recommendation. If the faulty node is an
   ancestor, also map the failure to that node's acceptance criterion.
6. Record the verdict through `graphctl verify`; do not edit implementation.
