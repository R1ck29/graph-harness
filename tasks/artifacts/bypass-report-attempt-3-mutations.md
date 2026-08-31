# bypass-report, attempt 3: mutation record

Both mutations were applied in a throwaway `git worktree` at `HEAD` (7b0eaf6),
never in the working tree, and the worktree was removed afterwards. Each one
reintroduces a defect a previous review reported, to show the guarding test
actually fails when the fix is removed.

Command in each case:

```
python -m unittest tests.test_session_hook_contract    # 44 tests
```

## Mutation 1 — the attempt-2 failure, restored

`previous_bypass` reads the whole journal and applies the positional cut only
to the candidate open and end pairs. The mutation moved the cut above the
`bypass_reported` branch, so a concurrent winner's claim falls outside the
loser's view again.

```python
-        if event == "bypass_reported":
-            reported.add(session)
-            continue
-        if position >= cut:
-            continue
+        if position >= cut:
+            continue
+        if event == "bypass_reported":
+            reported.add(session)
+            continue
```

Result: `FAILED (failures=1)`

```
FAIL: test_two_starts_at_once_report_one_bypass_between_them
AssertionError: 1 != 2
```

Two starts claimed the same bypass, which is the reviewer's reported failure
reproduced exactly. Unmutated, the same test passes and exactly one
`bypass_reported` record is written.

## Mutation 2 — the defect the first fix nearly caused

Reading claims whole while cutting transitions leaves a `graphctl` run that was
flushed after its own session closed outside every window, which accuses the
session that did use the graph.

```python
+        if position >= cut and event == "graph_transition":
+            continue
```

Result: `FAILED (failures=1)`

```
FAIL: test_a_graphctl_run_recorded_late_still_counts_for_its_session
```

Both halves of the fix are therefore load-bearing and each is pinned by its own
test.
