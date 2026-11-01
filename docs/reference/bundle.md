# Evaluation bundle format

The `crossfade/` directory at the repository root. `crossfade lock` validates it and stores it by digest.

## project.yaml

```yaml
name: order-service
app:
  start: python app.py        # gets PORT, WORLD_URL, APP_DATA_DIR, CROSSFADE_TRACE_FILE
  health: /health
  startup_timeout: 20
  env: {}                     # extra variables (no secrets: use the runner environment)
world:
  plugin: world.py:SupplierWorld
  fixture: default
  latency_scale: 1.0          # multiply simulated dependency latency
  failures:                   # optional seeded failure injection
    - {method: POST, pattern: /payments, status: 503, rate: 0.2, after_calls: 0, kind: error}
checks: checks.py
policy: policy.yaml
scenarios: scenarios
```

## policy.yaml

```yaml
version: "1"
owner: maintainers
protected_paths: ["crossfade/", ".crossfade/"]   # prefix match for entries ending in "/", glob otherwise
require_review_for: []                            # paths whose changes always need a person
gates: [every_confirmed_order_is_reserved]        # functions in checks.py
objectives:
  - {metric: latency_ms, direction: lower, min_improvement: 0.10, role: primary}
  - {metric: dependency_calls, direction: lower, max_regression: 0.10, role: guard}
confidence: 0.9
min_trials: 5
max_cv: 0.35
allow_network: false
timeout_seconds: 60
```

Metrics available to objectives: `latency_ms`, `cpu_ms`, `dependency_calls`, `unsupported_calls`, `errors`, and any world
meter starting with `model_` or `tool_` (e.g. `model_cost_usd`, `model_input_tokens`).

## scenarios/*.yaml

```yaml
name: checkout
description: Ordinary checkouts, including one that asks for more than the supplier has.
world_snapshot: default
seed: 1
holdout: false
steps:
  - {kind: http, method: POST, path: /orders, body: {customer: c-1, lines: [{sku: sku-1, qty: 2}]}}
  - {kind: conversation, message: "Refund order O-1001 please", body: {conversation_id: c-1}}   # POST /chat
  - {kind: clock, advance_seconds: 86400}
expect:
  - {target: "responses[0].status", op: eq, value: 201}
  - {target: "world.products.sku-1.stock", op: eq, value: 48, description: "stock is reserved"}
```

Targets address the run outcome: `responses[i].status|body…|ms`, `world.<table>.<id>.<field>`, `clock`, `meters.<name>`.
Operators: `eq ne gt ge lt le in contains exists`.
