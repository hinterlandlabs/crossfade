# CLI reference

Generated from `crossfade --help` by `scripts/gen_cli_reference.py`. Every command accepts `--json` where it prints data. Exit codes: 0 success, 1 usage or validation error, 2 experiment not accepted, 3 integrity failure.

## crossfade

```
 Usage: crossfade [OPTIONS] COMMAND [ARGS]...
 Crossfade: run software under controlled conditions and verify whether changes improve it.
 Options
 --help          Show this message and exit.
 Commands
 init        Register this git repository with Crossfade, inspect it, and scaffold the evaluation bundle if missing.
 inspect     Inspect the repository and print an evidence-labelled manifest.
 lock        Snapshot the evaluation bundle (crossfade/) into the artifact store. Experiments use the locked copy.
 revision    Register a git ref (or the dirty working tree) as an immutable revision.
 observe     Run scenarios once against a revision, capture traces, and record findings.
 baseline    Establish reproducible baseline measurements for a revision.
 compare     Create and run an experiment in one step.
 evaluate    Re-run the evaluator over an experiment's recorded runs and check it reproduces the recorded decision.
 history     Every experiment, its decision and why.
 findings    Findings recorded from traces.
 hypothesis  Record a hypothesis: a proposed mechanism and its expected impact.
 propose     Let a coding agent create a candidate from a staged copy of the base revision, without the evaluation
             bundle.
 optimize    Trace the app, let an agent change it, keep what tests better, and repeat from the kept version.
 verify      Verify the ledger hash chain and every artifact's digest.
 plugins     Installed plugins, what they provide, what they need, and whether they're healthy.
 serve       Start the local API and UI (localhost only by default).
 mcp         Run the MCP server on stdio for coding agents.
 example     Copy an example app into a new git repo, with a baseline commit and one branch per candidate.
 scenario    Scenarios in the locked evaluation bundle.
 world       World plugins: stateful stand-ins for dependencies.
 experiment  Experiments: baseline vs candidate under controlled conditions.
 traces      Traces captured from runs, or imported.
```

## crossfade init

```
 Usage: crossfade init [OPTIONS]
 Register this git repository with Crossfade, inspect it, and scaffold the evaluation bundle if missing.
 Options
 --name         <str>  Project name (defaults to the directory name).
 --start        <str>  Command that starts the app. [default: python app.py]
 --json                Machine-readable output.
 --help                Show this message and exit.
```

## crossfade inspect

```
 Usage: crossfade inspect [OPTIONS]
 Inspect the repository and print an evidence-labelled manifest.
 Options
 --json          Machine-readable output.
 --help          Show this message and exit.
```

## crossfade lock

```
 Usage: crossfade lock [OPTIONS]
 Snapshot the evaluation bundle (crossfade/) into the artifact store. Experiments use the locked copy.
 Options
 --json          Machine-readable output.
 --help          Show this message and exit.
```

## crossfade revision

```
 Usage: crossfade revision [OPTIONS] {ref}
 Register a git ref (or the dirty working tree) as an immutable revision.
 Arguments
 *    ref      <str>  [required]
 Options
 --label                  <str>
 --dirty    --no-dirty           Capture uncommitted changes. [default: no-dirty]
 --json                          Machine-readable output.
 --help                          Show this message and exit.
```

## crossfade observe

```
 Usage: crossfade observe [OPTIONS] [ref]
 Run scenarios once against a revision, capture traces, and record findings.
 Arguments
   ref      <str>  Git ref to run. [default: HEAD]
 Options
 --scenario        <str>
 --json                   Machine-readable output.
 --help                   Show this message and exit.
```

## crossfade baseline

```
 Usage: crossfade baseline [OPTIONS] [ref]
 Establish reproducible baseline measurements for a revision.
 Arguments
   ref      <str>  [default: HEAD]
 Options
 --trials        <int>  [default: 3]
 --json                 Machine-readable output.
 --help                 Show this message and exit.
```

## crossfade compare

```
 Usage: crossfade compare [OPTIONS] {baseline} {candidate}
 Create and run an experiment in one step.
 Arguments
 *    baseline       <str>  [required]
 *    candidate      <str>  [required]
 Options
 --name              <str>
 --trials            <int>
 --hypothesis        <str>
 --json                     Machine-readable output.
 --help                     Show this message and exit.
```

## crossfade evaluate

```
 Usage: crossfade evaluate [OPTIONS] {experiment_id}
 Re-run the evaluator over an experiment's recorded runs and check it reproduces the recorded decision.
 Arguments
 *    experiment_id      <str>  [required]
 Options
 --json          Machine-readable output.
 --help          Show this message and exit.
```

## crossfade history

```
 Usage: crossfade history [OPTIONS]
 Every experiment, its decision and why.
 Options
 --json          Machine-readable output.
 --help          Show this message and exit.
```

## crossfade findings

```
 Usage: crossfade findings [OPTIONS]
 Findings recorded from traces.
 Options
 --json          Machine-readable output.
 --help          Show this message and exit.
```

## crossfade hypothesis

```
 Usage: crossfade hypothesis [OPTIONS] {mechanism}
 Record a hypothesis: a proposed mechanism and its expected impact.
 Arguments
 *    mechanism      <str>  [required]
 Options
 --finding        <str>
 --impact         <str>
 --risks          <str>
 --author         <str>  [default: human]
 --json                  Machine-readable output.
 --help                  Show this message and exit.
```

## crossfade propose

```
 Usage: crossfade propose [OPTIONS]
 Let a coding agent create a candidate from a staged copy of the base revision, without the evaluation bundle.
 Options
 *  --agent                                      <str>   Command that runs a coding agent in the candidate worktree.
                                                         [required]
    --base                                       <str>   [default: HEAD]
    --hypothesis                                 <str>
    --name                                       <str>
    --pass-env                                   <str>   Environment variable the agent may see (repeatable).
    --allow-write                                <path>  Extra directory the agent may write, e.g. its cache
                                                         (repeatable).
    --require-sandbox    --no-require-sandbox            Refuse to run without an OS sandbox.
                                                         [default: no-require-sandbox]
    --json                                               Machine-readable output.
    --help                                               Show this message and exit.
```

## crossfade optimize

```
 Usage: crossfade optimize [OPTIONS]
 Trace the app, let an agent change it, keep what tests better, and repeat from the kept version.
 Options
 *  --agent                                      <str>    Command that runs a coding agent; it gets a staged copy of
                                                          the current best revision.
                                                          [required]
    --base                                       <str>    [default: HEAD]
    --rounds                                     <int>    Most rounds to run. [default: 5]
    --patience                                   <int>    Stop after this many rounds in a row without an accepted
                                                          change.
                                                          [default: 3]
    --max-minutes                                <float>  Time budget for the whole loop. [default: 60]
    --trials                                     <int>
    --pass-env                                   <str>    Environment variable the agent may see (repeatable).
    --allow-write                                <path>   Extra directory the agent may write (repeatable).
    --require-sandbox    --no-require-sandbox             Refuse to run without an OS sandbox.
                                                          [default: no-require-sandbox]
    --into                                       <str>    Fast-forward this branch to the best revision when the loop
                                                          keeps something.
    --json                                                Machine-readable output.
    --help                                                Show this message and exit.
```

## crossfade verify

```
 Usage: crossfade verify [OPTIONS]
 Verify the ledger hash chain and every artifact's digest.
 Options
 --json          Machine-readable output.
 --help          Show this message and exit.
```

## crossfade plugins

```
 Usage: crossfade plugins [OPTIONS]
 Installed plugins, what they provide, what they need, and whether they're healthy.
 Options
 --json          Machine-readable output.
 --help          Show this message and exit.
```

## crossfade serve

```
 Usage: crossfade serve [OPTIONS]
 Start the local API and UI (localhost only by default).
 Options
 --host        <str>  [default: 127.0.0.1]
 --port        <int>  [default: 8700]
 --help               Show this message and exit.
```

## crossfade mcp

```
 Usage: crossfade mcp [OPTIONS]
 Run the MCP server on stdio for coding agents.
 Options
 --help          Show this message and exit.
```

## crossfade example

```
 Usage: crossfade example [OPTIONS] {name} {dest}
 Copy an example app into a new git repo, with a baseline commit and one branch per candidate.
 Arguments
 *    name      <str>   [required]
 *    dest      <path>  [required]
 Options
 --help          Show this message and exit.
```

## crossfade scenario list

```
 Usage: crossfade scenario list [OPTIONS]
 Options
 --json          Machine-readable output.
 --help          Show this message and exit.
```

## crossfade scenario generate

```
 Usage: crossfade scenario generate [OPTIONS] {run_id}
 Draft a scenario from a recorded run. Observed outputs become *draft* expectations to review, not truth.
 Arguments
 *    run_id      <str>  [required]
 Options
 --name        <str>
 --json               Machine-readable output.
 --help               Show this message and exit.
```

## crossfade world list

```
 Usage: crossfade world list [OPTIONS]
 Options
 --json          Machine-readable output.
 --help          Show this message and exit.
```

## crossfade world serve

```
 Usage: crossfade world serve [OPTIONS]
 Serve the locked world locally for development. Control endpoints need the printed token.
 Options
 --fixture        <str>  [default: default]
 --seed           <int>  [default: 0]
 --port           <int>  [default: 8710]
 --help                  Show this message and exit.
```

## crossfade world reset

```
 Usage: crossfade world reset [OPTIONS]
 Reset a world started with `crossfade world serve` to its fixture state.
 Options
 --help          Show this message and exit.
```

## crossfade experiment create

```
 Usage: crossfade experiment create [OPTIONS]
 Create an experiment comparing two revisions.
 Options
 *  --baseline          <str>  [required]
 *  --candidate         <str>  [required]
    --name              <str>
    --hypothesis        <str>
    --trials            <int>
    --scenario          <str>
    --json                     Machine-readable output.
    --help                     Show this message and exit.
```

## crossfade experiment run

```
 Usage: crossfade experiment run [OPTIONS] {experiment_id}
 Run an experiment: baseline and candidate trials, independent evaluation, decision.
 Arguments
 *    experiment_id      <str>  [required]
 Options
 --json          Machine-readable output.
 --help          Show this message and exit.
```

## crossfade experiment show

```
 Usage: crossfade experiment show [OPTIONS] {experiment_id}
 Show an experiment's hypothesis, revisions, runs, evaluation and decision.
 Arguments
 *    experiment_id      <str>  [required]
 Options
 --json          Machine-readable output.
 --help          Show this message and exit.
```

## crossfade traces show

```
 Usage: crossfade traces show [OPTIONS] {run_id}
 Show a run's spans as a tree, with timing and status. Dependency calls Crossfade measured sit under the span that made
 them.
 Arguments
 *    run_id      <str>  [required]
 Options
 --json          Machine-readable output.
 --help          Show this message and exit.
```

## crossfade traces diff

```
 Usage: crossfade traces diff [OPTIONS] [a] [b]
 What the change does differently: call paths that appear, disappear, run more or fewer times, or take longer.
 Arguments
   a      <str>  Run id of the original (or use --experiment).
   b      <str>  Run id of the change.
 Options
 --experiment  -e      <str>  Compare every completed run of both sides.
 --scenario            <str>  Only this scenario (with --experiment).
 --limit               <int>  Rows to print. [default: 20]
 --json                       Machine-readable output.
 --help                       Show this message and exit.
```

## crossfade traces export

```
 Usage: crossfade traces export [OPTIONS] [run_ids]...
 Send runs to any OpenTelemetry backend, tagged with their commit, scenario, experiment and decision.
 Arguments
   run_ids      <str>  Runs to export (or use --experiment).
 Options
 --experiment        -e      <str>   Export every run of an experiment.
 --otlp                      <str>   OTLP/HTTP endpoint, e.g. http://localhost:4318. Headers come from
                                     OTEL_EXPORTER_OTLP_HEADERS.
                                     [env var: OTEL_EXPORTER_OTLP_ENDPOINT]
 --out                       <path>  Write OTLP/JSON to a file instead of sending it.
 --include-holdouts                  Also export holdout runs (left out by default).
 --json                              Machine-readable output.
 --help                              Show this message and exit.
```

## crossfade traces import

```
 Usage: crossfade traces import [OPTIONS] {path}
 Import an OTLP/JSON trace export as evidence (no run is attached).
 Arguments
 *    path      <path>  [required]
 Options
 --json          Machine-readable output.
 --help          Show this message and exit.
```
