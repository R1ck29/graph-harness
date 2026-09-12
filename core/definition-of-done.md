# Definition of done

A node is done only after all of these are true:

- every dependency is `verified` or `superseded`;
- the current attempt has evidence for every acceptance criterion;
- a reviewer identity differs from the executor identity;
- the reviewer explicitly checks every criterion, supplies independent review
  evidence, and records a PASS;
- the graph remains schema-valid after the transition.

The objective is done only when every node is `verified` or `superseded` and
`graphctl completion-check` exits successfully. Agent prose or a different ID
alone is not proof of independent verification; use a separate review context
and concrete, redacted artifact or test evidence.
