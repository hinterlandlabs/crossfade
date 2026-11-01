# Security and threat model

## What is trusted

The Crossfade core, the locked evaluation bundle (once a person has reviewed and locked it), and the ledger and artifact store
on the local disk.

## What is not trusted

The application under test, every candidate revision, coding agents, imported traces, repository contents and plugin output.
Instruction-like text inside any of these never changes policy.

## Threats and controls

| Threat | Control |
|---|---|
| Candidate edits its own gates, thresholds or world | Experiments load the bundle from its locked digest, never from the worktree. Diffs touching `protected_paths` are rejected before evaluation. |
| Candidate learns holdout cases from feedback | Holdout runs are evaluated separately; failures are reported as "details withheld". The API, MCP server and agent context never expose holdout contents. |
| Candidate fakes metrics | Decisions use metrics measured by Crossfade: wall time around requests, world-side call and token meters, rusage of the child process. App-reported spans are evidence for humans, not inputs to objectives. |
| Candidate omits required work to look faster | Correctness gates come first. A failed gate rejects regardless of improvement. |
| Agent reads gates or holdouts from its workspace | Agents get a staged copy made with `git archive` minus protected paths, in a new repo, so the bundle is in neither the files nor the history. |
| Agent reads the real repository, ledger or bundle store, or writes elsewhere | OS sandbox: `sandbox-exec` on macOS, `bwrap` on Linux. Reads of the repository are denied; writes are allowed only to the stage, a scratch dir and operator-approved paths. Without a sandbox this is not enforced and the run is recorded as `agent.unsandboxed`; `--require-sandbox` refuses instead. |
| Agent inherits secrets from the environment | Agents get PATH, HOME, locale and terminal variables plus only what `--pass-env` names. |
| Agent writes to protected paths | The patch is checked before it is applied; writes to protected paths are refused and recorded as `agent.protected_write`. |
| Evidence leaks through the API | `/v1/artifacts` never serves locked bundles or holdout-run evidence; holdout run outcomes and traces are withheld. |
| Holdouts leak through comparisons or export | Behaviour diffs skip holdout runs; `/v1/traces/diff` refuses them; `traces export` leaves them out unless `--include-holdouts` is given, and records every export as `trace.exported`. |
| Conversation content lands in traces | The harness records prompts and replies only when content capture is switched on; redaction still runs before storage. |
| Residue from earlier runs affects a later one | Worktrees are force-checked-out and `git clean -ffdx`'d before every run. |
| Missing or failed measurements fake an improvement | Objectives are compared only over identical, complete trial/scenario cells; any baseline crash makes the result inconclusive. |
| Agent writes a decision | Decisions are written only by the evaluator. Agents have explicit capabilities (`agents.Capability`); `admin_policy` and `external_writes` are not granted by default. |
| History rewritten | Ledger triggers block UPDATE/DELETE; the hash chain detects edits made by bypassing them; artifacts are verified on read. |
| Secrets leak into evidence | Redaction runs before anything is written to the artifact store; the runner passes a scrubbed environment. |
| Production writes | Apps reach dependencies only through `WORLD_URL`. Policy denies network by default; the runner sets a dead-end HTTP proxy. |

## Known limits of the local runner

The local runner is not a sandbox for hostile code. It uses a separate process group, a private temp directory, CPU-time and
file-descriptor limits, a scrubbed environment and a dead-end proxy. A malicious candidate could still read files the user can
read, open raw sockets, or ignore proxy settings. Use a container or VM runner for untrusted candidates; a Docker runner is
declared in the plugin registry and is not implemented in this release. Docker alone is also not a complete boundary against
hostile code: do not mount the Docker socket into candidate containers.

The hash chain detects casual or accidental modification. Someone with write access to `.crossfade/` can rebuild the chain.
For stronger guarantees, sign exported evidence or anchor digests externally.
