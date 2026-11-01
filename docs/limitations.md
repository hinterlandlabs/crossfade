# Known limitations

- **Isolation.** The local runner is not a hostile-code boundary (see architecture/security.md). The Docker runner is declared
  but not implemented, so agent changes run as your user.
- **Worlds are hand-written.** Discovery finds dependencies; it does not generate worlds or contracts from traces yet.
  `scenario generate` drafts scenarios from runs, but expectations must be reviewed by a person.
- **Simulated model.** The support-agent example uses a deterministic simulated model. Real models are probabilistic; run the
  same scenarios against a real provider (`AnthropicProvider`, with network allowed by policy) before trusting a prompt change.
- **Statistics.** The bootstrap with 3–5 trials is fine for large, stable effects. Small effects need more trials; the
  evaluator reports inconclusive rather than guessing.
- **Discovery** is Python-focused (AST) with manifest detection for Node. Other languages get file-level facts only.
- **Runs are sequential.** Experiments with many scenarios and trials take minutes.
- **Tamper evidence** is a local hash chain, not signatures.
- **Out-of-process plugins** are specified, not implemented.
- **Trace propagation** is automatic for Python HTTP clients built on `http.client` or `httpx`. Raw sockets, gRPC and
  other clients need their own OpenTelemetry instrumentation; their calls are still measured, just not placed in the app's trace.
- **Behaviour diffs** compare app-reported spans, which the app could shape. They explain a decision; they don't make it.
- **No streaming** in the harness providers yet, and no structured-output mode.
- **Local branches only.** `optimize --into` moves a local branch. Pushing and opening a pull request are up to you.
- **No signed exports.** There is no `crossfade export` of a signed evidence bundle.
- **Node** apps run, but discovery and tracing setup for them are manual. There are two reference apps, both Python.
- **Single project per workspace**, single user, no authentication beyond the local API token.
