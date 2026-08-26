# Plan: close the four reported gaps

## 1. No CLI path to author a multi-node graph
`graphctl init` creates exactly one node and refuses to overwrite, yet
`graph-planning` tells the planner to build a DAG and `AGENTS.md` forbids
editing the graph outside `graphctl`. The documented workflow was unreachable.

- [x] `Graph.add_node()` — build a candidate document, validate it, adopt it
      only when validation passes, so a rejected addition is a no-op.
- [x] `graphctl add-node NODE --description --criterion [--depends-on]
      [--role] [--max-attempts]`.
- [x] Update `roles/planner.md`, `skills/graph-planning/SKILL.md`, `README.md`,
      `AGENTS.md`; regenerate adapter copies.

## 2/3. `review_history` wedges the node at MAX_EVIDENCE
At 512 archived UNCERTAIN reviews every transition is refused: `withdraw`,
`submit`, `verify` (all three verdicts), and `retry`. The `verify` leg is a
regression introduced by `_record_verification` in beb1a2d.

- [x] One `_archive_review()` helper shared by `withdraw_submission` and
      `_record_verification` — removes the duplicated record construction and
      drops the oldest record at the cap instead of refusing the append.

## 4. Verification is point-in-time and nothing says so
A verified node keeps its PASS after its files are rewritten by later work.
The recovery mechanism already exists (`verify --fail --faulty-node ANCESTOR`),
but the reviewer is never given the ancestor's reviewed file list, so the
regression is invisible from the compact packet.

- [x] `Graph.ancestors()` and `upstream_verified_files` in `review_packet`.
- [x] State the point-in-time boundary and the reviewer's duty in
      `core/verification.md`, `core/protocol.md`,
      `skills/independent-verification/SKILL.md`.

## Verification
- [x] Contract tests for each change, including the 512-entry wedge repro.
- [x] `python -m unittest discover -v`, `black --check`, `mypy`,
      `python scripts/sync_adapters.py --check`, `graphctl completion-check`.
- [x] Independent subagent review.

## Review

All three gaps are closed and exercised end to end.

- `graphctl add-node` builds a DAG through the CLI; a rejected addition
  (duplicate id, unknown dependency, bad identifier, no criteria) is validated
  against a candidate document and leaves the graph byte-identical.
- The 512-entry wedge is gone on both legs. A node with a full trail can still
  be withdrawn, resubmitted, and given any verdict; the trail keeps the newest
  512 records.
- `review-packet` names each verified ancestor's reviewed files. The live run
  confirmed the intended recovery: the packet surfaced the shared file, the
  reviewer failed with `--faulty-node design`, the ancestor reopened, and the
  descendant was invalidated with its unspent attempt refunded.

### Follow-ups from the independent review

- The executor procedure never asked for `--relevant-file`, so the new packet
  field would have been empty in a graph built by the book. `graph-execution`
  now requires it and `core/verification.md` states what the packet actually
  lists.
- `graphctl init` had no `--description` or `--max-attempts`, so the root node
  of a DAG restated the whole objective and was the one node whose budget could
  not be set. Both now match `add-node`.
- `agent_harness/evals.py` carried its own copy of the ancestor walk; it now
  calls `Graph.ancestors`.
- The count bound alone did not keep a node movable. Sixty-three withdraw
  cycles with large evidence pushed the document past the storage layer's 4 MiB
  ceiling, after which every transition failed to save and the whole graph
  froze. The archive is now bounded by serialized size as well: 200 cycles peak
  at ~1 MB, and a single oversized record is dropped rather than wedging.
- `ReviewHistoryBoundTests` rebuilt its 512-record fixture per test. Building it
  once took the suite from 11.0s to 6.6s.
- `mypy` failed at HEAD on any 3.11+ machine because the `tomllib` ignore only
  covered the 3.10 error code. CI runs 3.10 and never saw it.

Checks: 102 tests on 3.8, 3.11, and 3.13; `black --check`; `mypy --strict`;
`sync_adapters.py --check`; two end-to-end CLI runs; independent subagent
review of the diff.

### Considered and not done

- Binding a verification to file hashes. Detecting drift needs a way to reopen
  a verified node for re-review, which does not exist, so a drifted graph would
  fail `completion-check` forever -- the same class of wedge this change set
  removes. The packet field routes the same problem into the working
  `--faulty-node` path instead.
- Refusing `verify --pass` when `relevant_files` is empty. It would invalidate
  existing graphs for a procedural omission the skill now prevents.
