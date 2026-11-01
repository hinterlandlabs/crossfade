# Crossfade

Crossfade watches your app run, works out what to change next, has a coding agent make the change, and keeps it only if
it tests better than what you already had. Then it starts again from the new version.

The first thing it does is run your app against stand-ins for its database and outside services and read the traces.
In the order-service example the traces show checkout asking the supplier for the same product more than once in a
single request, with `GET /products/sku-1` repeated four times across the run. Findings like that go to the agent along with the traces, the scenarios it can see, and every earlier
attempt with the reason it failed. The agent picks a change and writes it on a copy of the code that doesn't include your
checks.

Crossfade then runs the old and new versions through the same scenarios, from the same starting data and seed, and
applies the checks in your `crossfade/` folder. A change that breaks one is thrown out, and the next attempt hears why.
In the example, skipping the stock reservation made orders 46% faster and sold the last units of an item twice, so it
went. Batching the catalog lookups cut latency from 594 to 419 ms, passed every check, and became the new base. Crossfade
traced that version too, and the repeated calls were gone from its findings.

```sh
crossfade optimize --agent "claude -p 'Read \$CROSSFADE_CONTEXT and make one change that helps.'" --rounds 10 --into main
```

`--agent` takes any shell command, so Claude Code, Codex and your own scripts all work. Everything runs on your machine
and nothing needs an account. Apache 2.0, from [Hinterland Labs](https://hinterlandlabs.com).

## The loop

```
   ┌───────────────────── next round starts from the kept version ─────────────────────┐
   ▼                                                                                   │
 trace the app ──▶ agent reads findings ──▶ run old and new ──▶ checks, then ──▶ kept ──┘
 on stand-ins      and past attempts,       on the same world   latency, cost    rejected (agent hears why)
 for its services  writes a change          and seed                             inconclusive (too noisy)
```

The loop stops at `--rounds`, after `--max-minutes`, or once `--patience` rounds in a row produce nothing it keeps. With
`--into main`, Crossfade fast-forwards main to the best commit at the end. If main moved while the loop ran, or the
checkout has uncommitted work, it leaves main alone and prints why. Without `--into`, the result stays on its
`crossfade/candidate-*` branch for you to look at.

Single steps are there too: `crossfade observe` traces a commit and records findings, `crossfade propose` asks the agent
for one change, `crossfade compare main some-branch` runs one experiment, and `crossfade history` lists what was tried.

## The parts

- `crossfade.harness` is the runtime for agents you build yourself. It handles the model loop, tools and state, enforces
  step limits, tool allow-lists and token budgets, holds irreversible tool calls behind a gate, and traces every model and
  tool call with OpenTelemetry.
- Worlds stand in for an app's dependencies. They keep state, so a write shows up in later reads. The clock is simulated,
  failures come from a seed, and a world can be snapshotted and restored. Each one documents what it doesn't model.
  Calls, latency and tokens are counted inside the world, where the app under test can't touch the numbers.
- The runner checks each commit out into its own git worktree, starts the real app, waits for its health check, drives the
  scenario and collects responses, world state, traces and resource usage.
- `crossfade lock` copies the `crossfade/` folder (policy, check functions, world, scenarios, holdouts) into a
  content-addressed store, and experiments only read that copy. A change that touches protected paths is rejected
  outright. Holdout failures are reported without saying which scenario failed.
- Agents get a staged copy of the base commit with `crossfade/` stripped from the files and from git history, and a
  minimal environment. Under `sandbox-exec` on macOS or `bwrap` on Linux they also can't read your real repository or
  write outside the stage. With no OS sandbox available that part isn't enforced, and `--require-sandbox` makes the
  command refuse to run.
- Decisions run checks first. Objectives are compared over repeated, interleaved trials with a bootstrap confidence
  interval, and a result inside the noise is reported as inconclusive.
- Every event goes into an append-only SQLite ledger with a hash chain, and artifacts are stored by SHA-256.
  `crossfade verify` re-checks both.

## Install

```sh
uv tool install git+https://github.com/hinterlandlabs/crossfade    # Python 3.12+, uv
crossfade --help
```

To work on Crossfade itself or run the examples from source:

```sh
git clone https://github.com/hinterlandlabs/crossfade && cd crossfade
uv sync
uv run crossfade --help
```

## Run the examples

Two unrelated applications ship with the repository. `crossfade example` turns each into a git repository with a baseline
commit and one branch per candidate change.

```sh
uv run crossfade example order-service /tmp/orders
cd /tmp/orders
uv run --project ~/path/to/crossfade crossfade init      # register the repo, inspect it
uv run --project ~/path/to/crossfade crossfade lock      # lock the evaluation bundle
uv run --project ~/path/to/crossfade crossfade observe main
uv run --project ~/path/to/crossfade crossfade compare main batch-lookups
uv run --project ~/path/to/crossfade crossfade compare main skip-reservation
uv run --project ~/path/to/crossfade crossfade history
uv run --project ~/path/to/crossfade crossfade serve     # UI at http://127.0.0.1:8700
```

(Inside an activated virtualenv, `crossfade` alone works.)

### What the examples show

Measured on an Apple-silicon Mac (Apple M1). Latency depends on the machine; call counts, token counts and decisions don't.

**order-service**: an HTTP service with SQLite orders and a supplier dependency (catalog, stock reservations, payments).

| Change | Result | Evidence |
|---|---|---|
| `batch-lookups`: one batched catalog call instead of two calls per line | **accepted** | latency 594 → 419 ms (+29.5%, 90% CI +27.9% to +31.1%, 5 trials); dependency calls 53 → 32; every gate passed |
| `skip-reservation`: trust the catalog stock count and skip the reservation | **rejected** | 46% faster, but confirmed orders had no stock reservation, stock was never decremented, and the holdout "last units" scenario oversold |

**support-agent**: a customer support agent built on the Crossfade harness. Its model is a simulated model served by the
world: it follows the instructions actually present in the system prompt and reports token usage.

| Change | Result | Evidence |
|---|---|---|
| `drop-prefetch`: stop pasting the order into the prompt, since the model looks it up anyway | **accepted** | model cost −7.7%, latency −7.3%, every gate passed |
| `trim-prompt`: shorten the system prompt to two lines | **rejected** | 26.9% cheaper, but the prompt no longer stated the return policy and the agent refunded 7 orders delivered 31–52 days earlier |

## Use it on your own project

```sh
cd your-repo && crossfade init    # scaffolds crossfade/ if missing and records a discovery manifest
```

Then edit `crossfade/`: describe how to start the app (`project.yaml`), model its dependencies (`world.py`), write the gates
that define correct behavior (`checks.py`), and the scenarios to run (`scenarios/*.yaml`). `crossfade lock` freezes it.
See [docs/guides/new-project.md](docs/guides/new-project.md).

After that, `crossfade optimize --agent "..."` works on your repo. Agents can also drive Crossfade through the local API
or the MCP server (`crossfade mcp`); see [docs/guides/agents.md](docs/guides/agents.md).

## Documentation

- [Architecture](docs/architecture/overview.md) and [threat model](docs/architecture/security.md)
- Concepts: [worlds](docs/concepts/worlds.md), [experiments](docs/concepts/experiments.md), [evaluation](docs/concepts/evaluation.md), [the harness](docs/concepts/harness.md)
- Guides: [quickstart](docs/guides/quickstart.md), [new project](docs/guides/new-project.md), [coding agents](docs/guides/agents.md), [local development](docs/guides/development.md)
- Reference: [CLI](docs/reference/cli.md), [API](docs/reference/api.md), [bundle format](docs/reference/bundle.md), [plugins](docs/reference/plugins.md)
- [Generalization notes](docs/generalization.md) and [known limitations](docs/limitations.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues: [SECURITY.md](SECURITY.md).
