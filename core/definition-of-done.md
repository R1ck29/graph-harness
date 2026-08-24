# Definition of done

A node is done only after all of these are true:

- every dependency is verified;
- the current attempt has evidence for every acceptance criterion;
- a reviewer identity differs from the executor identity;
- the reviewer checks every criterion and records a PASS;
- the graph remains schema-valid after the transition.

The objective is done only when every node is `verified` and
`graphctl completion-check` exits successfully. Agent prose is not evidence.
