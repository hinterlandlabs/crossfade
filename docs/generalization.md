# Generalization: two unrelated applications

We wanted to know whether the core would run a second, unrelated application with no changes to the core. These are the two apps.

| | order-service | support-agent |
|---|---|---|
| Shape | HTTP CRUD service, SQLite, synchronous dependency calls | LLM agent on the Crossfade harness, tool calls, multi-step turns |
| Dependency | supplier catalog, reservations, payments | order system, refunds, a simulated model |
| Objective | latency | model cost (tokens), latency as a guard |
| Correctness | reservations back every confirmed order; payments match totals | no refunds outside the return window; eligible refunds still issued |
| Core changes needed | none | none |

What was reused unchanged: workspace, ledger, artifacts, revisions and worktrees, runner, experiment lifecycle, evaluator,
CLI, API, UI, MCP.

What was application-specific (and lives in each bundle): the world plugin, the gate functions, scenarios, and the policy.
The second application also needed one runner feature that is general rather than app-specific: `conversation` steps (a POST
to `/chat` with a message), and world-side meters for model tokens and cost picked up as objectives.

What this does not show: both apps are Python HTTP services written for this purpose. Neither is a background worker, an
event-driven system, a browser workflow or a large production codebase. World construction is manual in both. Claims beyond
"the core worked unchanged for two different request/response applications" would be unsupported.
