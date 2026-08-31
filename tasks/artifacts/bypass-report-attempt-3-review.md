# bypass-report, attempt 3: independent review

Reviewer id: `claude-graph-reviewer-bypass-3`. Verdict: **FAIL**, on one of five
criteria. Repository at `HEAD` 7b0eaf6, branch `feat/session-conformance`. The
reviewer wrote nothing into the repository and recorded no graph state.

## Verdict per criterion

| criterion | result |
| --- | --- |
| a bypass above the size threshold is reported once and never repeated | PASS |
| a session that used graphctl is not reported, including when the run carried no client session identifier and when it ran from a subdirectory | PASS |
| a session whose observation degraded is not judged in either direction | PASS |
| committed work is sized from the commits themselves, so a pull, a checkout or a one-line commit is not reported | **FAIL** |
| no session start can be made to report or to stay silent by a second-precision timestamp collision | PASS |

## The concurrency defect from attempt 2 is closed

The reviewer did not take the executor's sequential test on trust. It wrote its
own racer: twelve separate operating-system processes sharing one journal home,
spin-waiting on a common wall-clock start and then entering `_claim_bypass`,
and in a second variant the whole of `session_start` with the barrier placed
before `observe`, so opening records interleave with claims.

- At `HEAD`, over eight rounds: one claim returned, one `bypass_reported`
  record written, and in the full variant exactly one process exited 2 and
  wrote the warning.
- Mutation A, the attempt-2 defect restored: **12 of 12** processes claimed and
  twelve records were written.
- Mutation B, the `FileLock` replaced by a bare block plus a 50 ms delay:
  **12 of 12** again, which is what proves the twelve processes genuinely
  overlap and that the lock is load-bearing rather than decorative.

## The failure

`worth_reporting` asks `committed_size` how large a `HEAD` move was, and
`committed_size` runs `git diff --shortstat <before> <after>`. That measures a
pulled or checked-out diff exactly as it measures work the session authored.
Nothing anywhere discriminates the *origin* of a `HEAD` move; a grep for
`merge-base`, `rev-list`, `reflog`, `--author`, `ORIG_HEAD` and `is-ancestor`
across `agent_harness/` and `tests/` returns nothing.

Driven end to end through `session_start` and `session_end` against a real
upstream repository and a real clone:

| scenario | session's own work | next start |
| --- | --- | --- |
| `git pull --ff-only`, 2 files / 80 lines upstream | none | **exit 2**, "changed 2 file(s) and 80 line(s) without recording any task-graph state" |
| `git checkout feature`, branch differs by 2 files / 80 lines | none | **exit 2**, same message |
| one-line commit | one line | exit 0, silent |
| real 2-file / 40-line commit | real work | exit 2, correct |

A commit made in another terminal while the session ran isolates the mechanism:
`committed_size` returns `(2, 60)`, `change_size` returns `(0, 0)`, and
`worth_reporting` returns `(True, 2, 60)`.

Of the three false-positive sources the criterion names, only the one-line
commit is actually prevented, and it is prevented by the pre-existing size
threshold rather than by commit sizing. A pull and a checkout are the two cases
that specifically needed commit sizing, and both are normally far above
`WARN_MIN_FILES = 2` and `WARN_MIN_LINES = 20`.

The submitted evidence overstated its coverage. `test_a_pull_sized_head_move_is_not_reported`
(`tests/test_session_hook_contract.py:632`) performs no pull; it makes a local
one-line commit inside the session. No test in the 247-test suite performs a
`git pull` or a branch `checkout`. The suite is green; the gap is in coverage.

Two shipped statements assert the property the code does not have:

- `docs/conformance.md:84-86` — "Committed work is sized from `git diff
  --shortstat` between the two recorded commits, which is what keeps a pull, a
  checkout, or a one-line commit below the threshold."
- the `committed_size` docstring in `agent_harness/worktree.py` — "Asking git
  what moved distinguishes work the session did from a pull, a checkout, or a
  commit made elsewhere that happens to have landed while it ran."

It does not distinguish them. It only measures them.

## Failure scenario in one sentence

Someone opens a session in this repository, runs `git pull`, reads some code,
and closes without touching `graphctl` — correctly, because nothing was built —
and the next session start accuses them of bypassing the protocol. That is the
false accusation the module says it errs away from, and it is triggered by one
of the most routine git operations there is.

## Recommendation

Size only the commits the session is itself responsible for, rather than the
whole span between the two recorded heads: refuse a head move whose new head is
not a descendant of the old (`git merge-base --is-ancestor`), and count only
commits whose committer matches the local `user.email` and whose committer date
falls inside the session window, falling back to silence when the origin cannot
be established. Then add tests that perform a real `git pull` from a real
upstream clone and a real branch `checkout` with a diff well above both
thresholds, and correct `docs/conformance.md:84-86` and the `committed_size`
docstring.
