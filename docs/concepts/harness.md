# The harness

`crossfade.harness` is the environment agent-driven software runs in.

```python
from crossfade.harness import Agent, Constraints, HTTPModelProvider, tool

@tool(description="Look up an order", order_id="string")
def lookup_order(order_id): ...

@tool(description="Refund an order", irreversible=True, order_id="string", amount_cents="number")
def issue_refund(order_id, amount_cents): ...

agent = Agent(HTTPModelProvider(url), SYSTEM_PROMPT, [lookup_order, issue_refund],
              Constraints(max_steps=6, max_irreversible_per_turn=1, max_input_tokens=20000))
reply = agent.respond(conversation_id, message)
```

- **Loop**: model → tool → model … → text, within `max_steps`.
- **Constraints**: tool allow-list, required arguments, token budget per turn, irreversible tools gated and rate-limited.
  Violations end the turn safely and are recorded on the span.
- **State**: per-conversation messages and memory.
- **Context hooks**: optional function that adds context to the system prompt per turn.
- **Tracing**: spans `agent <name> turn`, `model complete` (with `gen_ai.*` attributes including token usage) and
  `tool <name>` (arguments, result, irreversibility). Execution is observable without extra work.
- **Providers**: `HTTPModelProvider` (a local or simulated model endpoint) and `AnthropicProvider` (Messages API with tool
  use; needs `ANTHROPIC_API_KEY` and a policy that allows the network). Anything with `complete(system, messages, tools)` works.

Because the harness runs inside Crossfade experiments, a change to prompts, tools or workflow is tested like any other change.
