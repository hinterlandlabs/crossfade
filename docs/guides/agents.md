# Working with coding agents

Crossfade doesn't write code itself; it gives agents a safe place to try changes and a judge they can't influence.

## Command adapter

```sh
crossfade findings
crossfade hypothesis "Batch the catalog lookups" --finding fnd_...
crossfade propose --hypothesis hyp_... --agent "claude -p 'Read \$CROSSFADE_CONTEXT and implement the hypothesis. Do not touch crossfade/.'"
crossfade compare main <candidate-commit>
```

`propose` stages a copy of the base revision with the protected paths removed (from the files and from git history),
writes a context file (findings, non-holdout scenarios, objectives, trace summaries, rules), and runs the command in the
stage with a minimal environment. Pass anything the agent itself needs explicitly, e.g. `--pass-env ANTHROPIC_API_KEY
--allow-write ~/.claude`. On macOS and on Linux with `bwrap` the agent is sandboxed: it can't read the real repository or
write outside its stage. Its changes come back as a patch; writes to protected paths are refused. The result is applied to
a new branch and recorded as a Candidate with its patch and touched paths.

Any agent works: `codex exec "..."`, a script, or a person typing.

## MCP

`crossfade mcp` serves tools over stdio: `inspect_project`, `list_findings`, `list_scenarios`, `get_trace`, `record_findings`,
`propose_hypothesis`, `request_experiment`, `experiment_history`, `world_coverage`. Authorization and evaluation stay in the core.

## Capabilities

Agents get `read_code`, `read_traces`, `create_candidate` and `run_experiment` by default. `write_scenarios`, `write_worlds`,
`review_evaluation`, `admin_policy` and `external_writes` must be granted explicitly, and decisions are never writable by agents.

## The improvement loop

`crossfade optimize` runs the whole cycle with a budget:

```sh
crossfade optimize --agent "claude -p 'Read \$CROSSFADE_CONTEXT and make one change that improves the objectives.'" \
  --rounds 5 --patience 2 --max-minutes 60 --pass-env ANTHROPIC_API_KEY --allow-write ~/.claude
```

Before the first round, the starting revision runs each visible scenario once and its traces are turned into findings.
Each round then runs `propose` against the current best revision, then a full experiment. An accepted candidate becomes the
base for the next round; anything else is pruned and the base stays. The loop stops at `--rounds`, at `--max-minutes`, or
after `--patience` rounds in a row with nothing accepted. The agent's context includes `previous_attempts` (outcomes and
reasons, with holdout details withheld), so it can avoid repeating a change that already failed. Its `findings` and
`traces` come from the revision it is editing. When a change is accepted, findings are recomputed from that change's
runs, so a problem the change fixed drops out of the next agent's context.

Rounds are written to the ledger as `optimize.started`, `optimize.round` and `optimize.finished`. By default the best
commit stays on its `crossfade/candidate-*` branch. `--into BRANCH` fast-forwards that branch to it and logs
`optimize.applied`, but only if the branch still points where the loop started and, when checked out, has no uncommitted
changes. Otherwise the branch is left as it was and the output says why. Exit code 2 means nothing was kept.
