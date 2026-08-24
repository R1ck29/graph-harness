# Graph Engineering Agent Harness

A small, vendor-neutral coding harness that turns an objective into a DAG,
executes specialized nodes, requires independent evidence-based verification,
and retries only affected descendants. It runs locally with Python's standard
library and works without a network, database, or orchestration server.

## Architecture

```text
Objective -> Planner -> Task Graph -> Executor/Specialist -> Reviewer
                                      PASS -> verified
                                      FAIL -> faulty node + descendant invalidation -> retry
```

`agent_harness/` enforces state and evidence. `core/` defines the portable
contract. `roles/` and `skills/` hold on-demand procedures. `.codex/`,
`.claude/`, and `adapters/` map equivalent behavior to each client.

## Quick start

Requires Python 3.10 or newer.

```bash
cp examples/task-graph.json task-graph.json
python scripts/graphctl.py validate
python scripts/graphctl.py ready
python scripts/graphctl.py start plan --executor-id planner-1
python scripts/graphctl.py submit plan \
  --actor-id planner-1 \
  --criterion "affected behavior and files are identified" \
  --evidence "scope recorded in the implementation plan"
python scripts/graphctl.py review-packet plan
python scripts/graphctl.py verify plan --pass --reviewer-id reviewer-1
python scripts/graphctl.py status
```

Use `--fail --reason "..." --failed-criterion "..."` to fail a review. To
identify a verified upstream cause, also provide `--faulty-node NODE` and its
`--faulty-criterion "..."`; then inspect the invalidated descendants and retry
the faulty node. `max_attempts` prevents endless retries. Completion requires:

```bash
python scripts/graphctl.py completion-check
```

## Run with Codex

Open the repository in Codex. It reads `AGENTS.md`, generated `.agents/skills`,
and the read-only `.codex/agents/reviewer.toml`. Ask Codex to use the graph
workflow and delegate verification to `graph_reviewer`. See
`adapters/codex/README.md` for the fresh-session fallback and permissions.

## Run with Claude Code

Open the repository with `claude`. `CLAUDE.md` imports the shared instructions;
project skills and reviewer are under `.claude/`; the deterministic Stop hook is
an opt-in settings example because hooks execute repository code. Use
`--worktree` for isolated parallel writes where appropriate. See
`adapters/claude/README.md`.

## Verification and retry

Evidence entries belong to the current attempt and map to acceptance criteria.
PASS is rejected if evidence is missing, a criterion is uncovered, or reviewer
and executor identities match. FAIL requires criterion-linked observed evidence
and preserves a structured record in the graph, then invalidates only the
identified faulty node's transitive descendants. Other verified ancestors and
unrelated branches remain intact.

## Evals and tests

```bash
python -m unittest discover -v
python scripts/sync_adapters.py --check
python evals/runner/run.py
```

Five portable cases cover a bug fix, multi-file change, test repair,
regression-prone change, and upstream invalidation. The bundled runner is an
offline protocol comparison; live client runs can populate token usage later.
See `docs/evaluation.md`.

## Add skills or integrate elsewhere

Add one focused source skill under `skills/<name>/SKILL.md`, run
`scripts/sync_adapters.py`, and commit both source and generated copies. For
organization-specific skills, DB/MCP evidence, acceptance policies, and minimal
migration steps, see `docs/integration-guide.md`. Current client capability
decisions and official sources are in `docs/platform-compatibility.md`. The
identity, filesystem, hook, and sandbox boundary is documented in
`docs/security.md`.
