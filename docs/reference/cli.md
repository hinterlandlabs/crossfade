# CLI reference

Generated from `crossfade --help`. Every command accepts `--json` where it prints data. Exit codes: 0 success, 1 usage or validation error, 2 experiment not accepted, 3 integrity failure.

## crossfade 

```
 Usage: crossfade [OPTIONS] COMMAND [ARGS]...
 Crossfade: run software under controlled conditions and verify whether changes improve it.
 Options
 --help          Show this message and exit.
 Commands
 init        Register this git repository with Crossfade, inspect it, and scaffold the evaluation bundle if
             missing.
 inspect     Inspect the repository and print an evidence-labelled manifest.
 lock        Snapshot the evaluation bundle (crossfade/) into the artifact store. Experiments use the
             locked copy.
 revision    Register a git ref (or the dirty working tree) as an immutable revision.
 observe     Run scenarios once against a revision, capture traces, and record findings.
 baseline    Establish reproducible baseline measurements for a revision.
 compare     Create and run an experiment in one step.
 evaluate    Re-run the evaluator over an experiment's recorded runs and check it reproduces the recorded
             decision.
 history     Every experiment, its decision and why.
 findings    Findings recorded from traces.
 hypothesis  Record a hypothesis: a proposed mechanism and its expected impact.
 propose     Let a coding agent create a candidate in an isolated worktree. The agent never sees the
             evaluation bundle.
 optimize    Trace the app, let an agent change it, keep what tests better, and repeat from the kept
             version.
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
 Let a coding agent create a candidate from a staged copy of the base revision, without the evaluation
 bundle.
 Options
 *  --agent                                      <str>   Command that runs a coding agent in the candidate
                                                         worktree.
                                                         [required]
    --base                                       <str>   [default: HEAD]
    --hypothesis                                 <str>
    --name                                       <str>
    --pass-env                                   <str>   Environment variable the agent may see
                                                         (repeatable).
    --allow-write                                <path>  Extra directory the agent may write, e.g. its
                                                         cache (repeatable).
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
 *  --agent                                      <str>    Command that runs a coding agent; it gets a
                                                          staged copy of the current best revision.
                                                          [required]
    --base                                       <str>    [default: HEAD]
    --rounds                                     <int>    Most rounds to run. [default: 5]
    --patience                                   <int>    Stop after this many rounds in a row without an
                                                          accepted change.
                                                          [default: 3]
    --max-minutes                                <float>  Time budget for the whole loop. [default: 60]
    --trials                                     <int>
    --pass-env                                   <str>    Environment variable the agent may see
                                                          (repeatable).
    --allow-write                                <path>   Extra directory the agent may write (repeatable).
    --require-sandbox    --no-require-sandbox             Refuse to run without an OS sandbox.
                                                          [default: no-require-sandbox]
    --into                                       <str>    Fast-forward this branch to the best revision
                                                          when the loop keeps something.
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

## crossfade experiment create

```
Usage: crossfade [OPTIONS] COMMAND [ARGS]...
Try 'crossfade --help' for help.
 Error
 No such command 'experiment create'. Did you mean 'experiment'?
```

## crossfade experiment run

```
Usage: crossfade [OPTIONS] COMMAND [ARGS]...
Try 'crossfade --help' for help.
 Error
 No such command 'experiment run'. Did you mean 'experiment'?
```

## crossfade experiment show

```
Usage: crossfade [OPTIONS] COMMAND [ARGS]...
Try 'crossfade --help' for help.
 Error
 No such command 'experiment show'. Did you mean 'experiment'?
```

## crossfade scenario list

```
Usage: crossfade [OPTIONS] COMMAND [ARGS]...
Try 'crossfade --help' for help.
 Error
 No such command 'scenario list'. Did you mean 'scenario'?
```

## crossfade scenario generate

```
Usage: crossfade [OPTIONS] COMMAND [ARGS]...
Try 'crossfade --help' for help.
 Error
 No such command 'scenario generate'. Did you mean 'scenario'?
```

## crossfade world list

```
Usage: crossfade [OPTIONS] COMMAND [ARGS]...
Try 'crossfade --help' for help.
 Error
 No such command 'world list'. Did you mean 'world'?
```

## crossfade world serve

```
Usage: crossfade [OPTIONS] COMMAND [ARGS]...
Try 'crossfade --help' for help.
 Error
 No such command 'world serve'. Did you mean 'observe', 'world', 'serve'?
```

## crossfade world reset

```
Usage: crossfade [OPTIONS] COMMAND [ARGS]...
Try 'crossfade --help' for help.
 Error
 No such command 'world reset'. Did you mean 'world'?
```

## crossfade traces show

```
Usage: crossfade [OPTIONS] COMMAND [ARGS]...
Try 'crossfade --help' for help.
 Error
 No such command 'traces show'. Did you mean 'traces'?
```

## crossfade traces import

```
Usage: crossfade [OPTIONS] COMMAND [ARGS]...
Try 'crossfade --help' for help.
 Error
 No such command 'traces import'. Did you mean 'traces'?
```

