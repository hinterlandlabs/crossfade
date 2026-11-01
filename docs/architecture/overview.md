# Architecture

Crossfade is a modular monolith: one Python package with a small trusted core and replaceable parts around it.

## The trusted core

These modules decide what happened and whether a change is accepted. They are small on purpose so they can be audited.

| Module | Owns |
|---|---|
| `models.py` | canonical object identities and schemas (Project, Revision, Run, Experiment, Evaluation, Decision, Finding, Hypothesis, Candidate, Policy, Event) |
| `store/ledger.py` | append-only SQLite ledger with a hash chain; triggers reject UPDATE/DELETE; span index |
| `store/artifacts.py` | SHA-256 content-addressed evidence; atomic, read-only writes; digest verification on every read |
| `project.py` | workspace, project registration, and locking the evaluation bundle |
| `evaluation.py` | gates before objectives, bootstrap comparison, decision rules, holdout withholding |
| `experiments.py` | lifecycle state machine, interleaved trials, decision recording |
| `revisions.py` | immutable revisions (commits or captured dirty trees), worktrees, protected-path checks |
| `redact.py` | secret redaction before anything is stored |

## Replaceable parts

| Kind | Built-in | Interface |
|---|---|---|
| discoverer | `discovery.py` (Python AST, manifests) | `inspect(root) -> manifest` |
| instrumentation | `instrument.py` (OTel SDK → JSONL), OTLP/JSON receiver and import | spans normalized to `models.Span` |
| world runtime | `worlds/base.py` (`World`, `WorldServer`) | subclass `World`, register routes |
| runner | `runner.py` (local process) | `run_scenario(ws, bundle, rev, scenario, trial)` |
| harness | `harness/` (agent loop, tools, constraints, providers) | `ModelProvider.complete`, `Tool` |
| agent adapter | `agents.py` (any command), MCP server | `propose(ws, base, command)` |
| reporters | CLI, `api.py` + `ui/`, `mcp_server.py` | read the ledger |

`plugins.py` lists every part with its version, capabilities, required permissions and a health check. The core resolves
capabilities (`resolve("execution.run")`), not concrete classes.

## Data flow of one experiment

1. `crossfade lock` snapshots `crossfade/` (policy, gates, world, scenarios) as an artifact. Its digest is the policy digest.
2. `experiments.create` registers both revisions (commits) and a Candidate (patch, touched paths, originator).
3. `experiments.run` validates the experiment (revisions exist, bundle present, scenarios valid, network denied by policy).
4. For each trial, baseline and candidate run in alternating order. Each run:
   - starts a fresh world from the fixture and seed and records its snapshot digest
   - starts the app from its worktree with a scrubbed environment and CPU/file limits
   - drives the scenario steps over HTTP
   - collects responses, final world state, world-measured meters and spans, app spans and resource usage
   - stores the outcome, trace and log as artifacts and records a Run
5. The evaluator loads outcomes, runs the gates (expectations, world invariants, bundle gate functions, split by holdout),
   compares objectives across trials, and records an Evaluation and a Decision with reasons.
6. History, findings and traces are all projections over the ledger.

## Data flow of the improvement loop

`optimize.py` puts a coding agent in front of the experiment above and repeats it. It first runs the starting revision
once per visible scenario and records findings from its traces. Each round `agents.propose` hands the agent a staged
copy of the current best revision plus a context file with that revision's findings and traces, and the earlier attempts
with the reasons they were rejected. The agent's commit then goes through a normal experiment, and if it's accepted it
becomes the base for the next round, and its runs replace the old findings. Stop conditions are a round limit, a time
budget and a number of misses in a row. With `--into`, a branch is fast-forwarded to the best revision at the end.
`optimize.py` isn't part of the trusted core. It creates and runs experiments through `experiments.py` like any other
caller and never touches `evaluation.py`.

## Why these choices

- **SQLite plus files**: local, zero setup, transactional. Large evidence stays out of the database.
- **Git worktrees**: exact revisions, isolated from each other and from the user's working tree.
- **Worlds measure, apps don't**: cost and call counts come from the world, so a candidate can't improve its numbers by
  changing its own instrumentation.
- **Bootstrap intervals**: few assumptions about distributions, and they work with small trial counts. The threshold and
  confidence come from policy rather than a universal constant.
