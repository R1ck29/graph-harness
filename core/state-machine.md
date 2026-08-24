# State machine

```text
blocked -> ready -> running -> awaiting_verification -> verified
                    ^                 |          |
                    |                 |          -> awaiting_verification (UNCERTAIN)
                    |                 |                         |
                    +---- withdraw ---+-------------------------+
                                      -> failed -> ready or blocked (retry)

affected descendant -> invalidated -> ready or blocked (retry)
```

- Dependencies must be `verified` before a node becomes `ready`.
- `start` is the only transition to `running` and increments `attempts`.
- `submit` is the only transition to `awaiting_verification`.
- `verify` is the only transition to `verified` or `failed`.
- `withdraw` returns only an UNCERTAIN submission to its original executor for
  stronger evidence; it keeps the current attempt count.
- A FAIL review may reopen one verified ancestor as `failed` when that ancestor
  is reported as the cause; all of its descendants become `invalidated`.
- `retry` refuses a node whose `attempts` reached `max_attempts`.
- Arbitrary transitions are rejected.
- A failed node invalidates its transitive descendants, never ancestors or
  unrelated branches.
