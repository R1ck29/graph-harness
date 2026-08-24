# Independent verification

Create the reviewer input with `graphctl review-packet NODE`. On submission,
reference repository-relative source paths, a diff artifact, and a test-output
artifact when needed. Do not include the executor's conversation or private
reasoning.

The reviewer returns one verdict and supplies its own criterion-linked evidence
rather than relying on the executor's evidence being copied into the review:

- `PASS`: all criteria are explicitly checked and independently supported by
  reviewer evidence.
- `FAIL`: include the failed criterion, observed evidence, reason, faulty node,
  affected descendants, and minimal retry recommendation.
- `UNCERTAIN`: evidence is insufficient; keep the node awaiting verification.

After UNCERTAIN, the original executor runs `graphctl withdraw NODE --actor-id
EXECUTOR`, collects stronger evidence, and submits again. Withdrawal is refused
for PASS, FAIL, or a different executor and does not increment `attempts`.

The reviewer reports and never repairs code in the same review context.
The faulty node may be the reviewed node or one of its verified ancestors; an
unrelated node cannot be reopened through the review command. When reopening an
ancestor, record both the observed node's failed criterion and the ancestor's
corresponding `--faulty-criterion` so recovery does not mix their contracts.

Reviewer and executor IDs are logical labels, not authentication. Use a fresh
read-only reviewer session or equivalent organizational control for actual
independence.
