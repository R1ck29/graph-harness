# Evaluation protocol

The five cases in `evals/cases/` describe portable coding objectives and task
graphs. Run the dependency-free protocol comparison with:

```bash
python evals/runner/run.py --output evals/results/offline-protocol.json
```

The offline runner compares a full-replay baseline with descendant-only graph
retry. It records measured protocol checks, retries, unnecessary reruns, and
wall time. Model-dependent fields such as task success, first-pass status,
reviewer detection, regressions, and token usage remain `null`; the offline run
does **not** claim that a model becomes more accurate or token-efficient.

For live evaluation, execute the same case once without graph instructions and
once with them, pair results by client/case/repetition, alternate AB/BA order,
and record real CLI/model versions plus raw token fields. Do not pool unmatched
runs or infer token savings from prompt size.
