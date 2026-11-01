# Traces

Crossfade keeps two kinds of spans for every run, and joins them.

- **App spans** are what the app reports about itself through OpenTelemetry: request handlers, the harness's
  `invoke_agent`, `chat` and `execute_tool` spans, anything else the app instruments. They're useful evidence, but the app
  under test wrote them, so decisions never depend on them.
- **Measured spans** are dependency calls as the world saw them: method, route, status and timing, taken on Crossfade's
  side of the connection. Calls, tokens and cost in experiment metrics come from these.

## How they're joined

Python apps run under `python -m crossfade.instrument`. Besides writing spans to a file, it adds a W3C `traceparent`
header to every outgoing HTTP request made through `http.client` (so `urllib`, `urllib3` and `requests`) or `httpx`. When
the world receives a call with that header, its measured span goes into the app's trace as a child of the span that made
the call. Apps in other languages get the same result if they use an OpenTelemetry HTTP client instrumentation, which sends
the header.

The result is one tree per request:

```
invoke_agent support                                  145.93 ms
  support GET /orders/{order_id}                        6.05 ms  (measured)
  chat simulated-support-1                             37.42 ms
    support POST /v1/complete                          36.07 ms  (measured)
  execute_tool lookup_order                             7.42 ms
    support GET /orders/{order_id}                      6.06 ms  (measured)
  execute_tool issue_refund                            15.33 ms
    support POST /refunds                              14.10 ms  (measured)
```

`crossfade traces show RUN` prints it, the local UI draws it, and the coverage line says how many measured calls were
placed: `20 dependency calls measured by Crossfade, 20 traced to the code that made them`. A call that arrives without a
header still counts in the metrics. It just sits in a trace of its own.

## Comparing two versions

`crossfade traces diff -e EXPERIMENT` lines up the original's and the change's traces by call path (span names from the
root down, with ids such as `O-1042` folded so the same step matches) and averages per run. Rows that appear, disappear or
run a different number of times come first. In the support-agent `trim-prompt` experiment:

```
count        9 -> 16      162.6 -> 292.2   ms  invoke_agent support > execute_tool issue_refund
removed     20 -> 0       117.5 -> 0.0     ms  invoke_agent support > support GET /orders/{order_id}
```

The shorter prompt dropped the order prefetch, and the agent issued seven more refunds per run, the ones the return-window
check then caught. The same comparison is on the experiment page in the UI, at `GET /v1/experiments/{id}/diff`, and in the
MCP tool `compare_behaviour`, and `request_experiment` returns its top rows, so an agent sees what its change did and not
only that it was rejected. Holdout runs are never part of a comparison.

## Sending traces elsewhere

```sh
crossfade traces export -e EXPERIMENT --otlp http://localhost:4318   # Jaeger, Phoenix, Langfuse, an OTel Collector
crossfade traces export RUN_ID --out run.json                        # OTLP/JSON file
```

`OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_HEADERS` work as they do for any OpenTelemetry exporter, so an API key
for a hosted backend stays in the environment. Every exported span carries resource attributes for what it is evidence of:

| Attribute | Value |
|---|---|
| `vcs.ref.head.revision` | the commit that ran |
| `crossfade.revision.id`, `crossfade.revision.label` | the Crossfade revision and its branch name |
| `crossfade.run.id`, `crossfade.run.status`, `crossfade.scenario`, `crossfade.trial`, `crossfade.seed` | the run |
| `crossfade.experiment.id`, `crossfade.experiment.name`, `crossfade.side` | the experiment, and `original` or `change` |
| `crossfade.decision` | `accepted`, `rejected`, `inconclusive`, `needs_review` or `undecided` |

So in the backend you already use, you can filter to `crossfade.decision = rejected` and read what the rejected change did,
or group by `vcs.ref.head.revision`. Holdout runs are left out unless `--include-holdouts` is given, and every export is
recorded in the ledger as `trace.exported`.

## Importing

`crossfade traces import file.json` stores an OTLP/JSON export as evidence and indexes its spans for search. Imported
traces aren't attached to a run.
