# Graph Engineering Agent Harness

A small, vendor-neutral task-state harness for coding and other structured work.
It records an objective as a DAG, code-gates state transitions, requires explicit
executor and reviewer evidence, and retries only affected descendants. The
deterministic core runs locally with Python's standard library and needs no
network, database, or orchestration server.

The CLI is a ledger and verification gate. It does not generate a plan from an
objective, execute task commands, start an AI client, or prove that two IDs are
different people. Codex, Claude Code, another client, or a human performs the
work. Use a separate read-only reviewer session when independence matters.

## Architecture

```text
Objective -> Planner -> Task Graph -> Executor/Specialist -> Reviewer
                                      PASS -> verified
                                      FAIL -> faulty node + descendant invalidation -> retry
```

`agent_harness/` enforces state and evidence. `core/` defines the portable
contract. `roles/` and `skills/` hold on-demand procedures. `.codex/`,
`.claude/`, and `adapters/` map equivalent behavior to each client.

## Before you start

Requires Python 3.10 or newer.

Do not put passwords, API keys, customer names, contract terms, personal data,
or unredacted client material in graph evidence. Evidence is retained in the
graph and may also remain in shell history. Use repository-relative artifact
references and redacted summaries, as the example below does.

### macOS or Linux installation

```bash
python3 --version
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
cp examples/quickstart/task-graph.json task-graph.json
```

### Windows PowerShell installation

```powershell
py -3.10 --version
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
Copy-Item examples\quickstart\task-graph.json task-graph.json
$Graphctl = (Resolve-Path .\.venv\Scripts\graphctl.exe).Path
```

If `py -3.10` is unavailable, install a supported Python release and substitute
the exact interpreter path. Do not rely on an unrelated `python` command that
may point to an older installation.

## Complete quick start

The sample is intentionally non-sensitive and has one node so the walkthrough
finishes with a verified objective.

### macOS or Linux

```bash
graphctl validate
graphctl ready
graphctl start deliverable --executor-id executor-1
graphctl submit deliverable --actor-id executor-1 --evidence "@examples/quickstart/submission-evidence.json" --relevant-file examples/quickstart/deliverable.txt
graphctl review-packet deliverable
graphctl verify deliverable --pass --reviewer-id reviewer-1 --criterion "the deliverable contains a title, intended audience, and next step without customer data" --evidence "@examples/quickstart/review-evidence.json"
graphctl completion-check
```

### Windows PowerShell

```powershell
& $Graphctl validate
& $Graphctl ready
& $Graphctl start deliverable --executor-id executor-1
& $Graphctl submit deliverable --actor-id executor-1 --evidence "@examples/quickstart/submission-evidence.json" --relevant-file examples/quickstart/deliverable.txt
& $Graphctl review-packet deliverable
& $Graphctl verify deliverable --pass --reviewer-id reviewer-1 --criterion "the deliverable contains a title, intended audience, and next step without customer data" --evidence "@examples/quickstart/review-evidence.json"
& $Graphctl completion-check
```

The final command returns `{"complete": true}`. In real use, stop after
`review-packet`, give that packet and the referenced artifacts to a separate
read-only reviewer, and let the reviewer supply the final two arguments.

Create a graph for your own one-step objective with `graphctl init --objective
"..." --criterion "..."`. The command refuses to overwrite an existing graph.
Grow it into a dependency graph with `graphctl add-node NODE --description
"..." --criterion "..." --depends-on EXISTING`, adding each node after the ones
it depends on. A new node starts `blocked` and becomes `ready` once every
dependency is verified. Run `graphctl doctor` when setup or a lock appears
unhealthy.

Run every `graphctl` command from the directory that holds `task-graph.json`.
The workspace is the current directory, so a graph in a parent directory is
rejected as outside the workspace even when named by an absolute path.

Use `--fail --reason "..." --failed-criterion "..." --evidence "..."` to fail a
review. An `UNCERTAIN` review remains pending; the original executor can run
`graphctl withdraw NODE --actor-id EXECUTOR`, collect stronger evidence, and
submit again without consuming another attempt. To identify a verified upstream
cause, also provide `--faulty-node NODE` and its `--faulty-criterion "..."`.
`max_attempts` prevents endless retries.

## Run with Codex

Open the repository in Codex after creating or copying `task-graph.json`. It
reads `AGENTS.md`, generated `.agents/skills`,
and the read-only `.codex/agents/reviewer.toml`. Ask Codex to use the graph
workflow and delegate verification to `graph_reviewer`. See
`adapters/codex/README.md` for the fresh-session fallback and permissions.

## Run with Claude Code

Open the repository with `claude` after creating or copying `task-graph.json`.
`CLAUDE.md` imports the shared instructions;
project skills and reviewer are under `.claude/`; the deterministic Stop hook is
an opt-in settings example because hooks execute repository code. Use
`--worktree` for isolated parallel writes where appropriate. See
`adapters/claude/README.md`.

## Verification and retry

Evidence entries belong to the current attempt and map to acceptance criteria.
PASS is rejected if executor evidence, explicit reviewer evidence, or checked
criteria are incomplete, or when reviewer and executor IDs match. Those IDs are
logical labels; the client and operating environment must provide the actual
separate context. FAIL requires criterion-linked observed evidence and preserves
a structured record in the graph, then invalidates only the identified faulty
node's transitive descendants. Other verified ancestors and unrelated branches
remain intact.

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
