# Core protocol

The harness converts one objective into a directed acyclic task graph. A planner
defines small nodes and acceptance criteria. Executors work only on `ready`
nodes. A separate reviewer receives a compact review packet and records
`PASS`, `FAIL`, or `UNCERTAIN`. Only the deterministic CLI changes state.

## Lifecycle

1. Validate the graph before execution.
2. Start a `ready` node with an executor identity.
3. Submit current-attempt evidence mapped to acceptance criteria.
4. Give `graphctl review-packet NODE` to an independent reviewer.
5. Record the verdict with `graphctl verify`, including explicit reviewer
   criteria and evidence for PASS or FAIL.
6. On UNCERTAIN, let the original executor withdraw the submission, strengthen
   its evidence, and submit again without opening a new attempt.
7. On failure, retry the faulty node and only its invalidated descendants.
8. Treat the objective as complete only when `completion-check` succeeds.

Agents must not edit status, attempts, verification, or failure history directly.
Canonical graph files are JSON. JSON is a YAML 1.2 subset, but v1 intentionally
does not include a general YAML parser.
