# The harness

`crossfade.harness` is the environment agent-driven software runs in: the model loop, the tools, the state, the limits,
and the tracing. Because Crossfade runs the app inside experiments, a change to a prompt, a tool or the loop itself is
tested like any other change.

```python
from crossfade.harness import Agent, Constraints, FallbackProvider, AnthropicProvider, OpenAIChatProvider, tool

@tool(description="Look up an order", order_id="string")
def lookup_order(order_id): ...

@tool(description="Refund an order", irreversible=True, order_id="string", amount_cents="number")
def issue_refund(order_id, amount_cents): ...

model = FallbackProvider(AnthropicProvider("claude-sonnet-5-5"),
                         OpenAIChatProvider("qwen3:8b", base_url="http://localhost:11434/v1", api_key=""))
agent = Agent(model, SYSTEM_PROMPT, [lookup_order, issue_refund],
              Constraints(max_steps=6, max_irreversible_per_turn=1, max_input_tokens=20000))
reply = agent.respond(conversation_id, message)
```

## The loop

A turn goes model, tools, model, until the model answers in text or `max_steps` runs out. A model may ask for several
tools in one reply; they run in the order given, each result goes back with the id of the call it answers, and the next
model call sees all of them. That id is what Anthropic and OpenAI require to match results to calls, so conversations stay
valid across providers. If a turn ends early (a constraint, a provider failure), any call left without a result gets an
`{"error": "not run"}` result before the turn closes, so the next turn doesn't start from a malformed history.

A tool that raises doesn't end the turn. The model gets `{"error": "RuntimeError: ..."}` and decides what to do, and the
`execute_tool` span records the exception.

## Constraints

- a tool allow-list (`allowed_tools`), and required arguments checked before the tool runs;
- an input-token budget per turn;
- irreversible tools (`irreversible=True`) can be switched off or limited per turn.

A violation ends the turn with a safe reply and is recorded on the turn's span as an event and as `error.type`.

## Hooks

Subclass `Hook` and pass `hooks=[...]` to change behavior around the loop without forking it.

| Method | Called | Can |
|---|---|---|
| `before_model(call)` | before every model call | edit `call.system` and `call.messages` (a copy), or raise `ConstraintViolation` |
| `after_model(call, reply)` | after every model call | return a replacement reply |
| `before_tool(conversation_id, call)` | after constraints pass, before the tool runs | return a result to skip the tool (a cache hit, a refusal), or raise `ConstraintViolation` |
| `after_tool(conversation_id, call, result)` | after the tool | return a replacement result (redaction, truncation) |

When `before_tool` short-circuits a tool, the span records which hook did it.

## Providers

| Provider | Talks to |
|---|---|
| `AnthropicProvider` | the Messages API with tool use (`ANTHROPIC_API_KEY`) |
| `OpenAIChatProvider` | any OpenAI-compatible chat completions endpoint: OpenAI, Ollama, vLLM, LM Studio (`base_url`, `api_key`) |
| `HTTPModelProvider` | a simple `{system, messages, tools}` endpoint, used for simulated models served by a world |
| `FallbackProvider(a, b, ...)` | tries each in order; the span records which one answered |

Each provider retries connection errors and HTTP 408, 409, 429, 5xx and 529 with exponential backoff and jitter, and honors
`Retry-After`. `Retry(attempts=..., base_delay=...)` changes that. Every retry and every fallback is an event on the model
call's span, so a slow turn says why it was slow. Anything with `complete(system, messages, tools) -> ModelReply` works as a
provider. Experiments deny network access by default, so a real provider needs a policy that allows it.

## Tracing

Spans follow the OpenTelemetry GenAI semantic conventions, so any backend that understands them shows harness traces
without configuration.

| Span | Kind | Attributes |
|---|---|---|
| `invoke_agent {name}` | internal | `gen_ai.agent.name`, `gen_ai.conversation.id`, `gen_ai.provider.name`, turn token totals and steps |
| `chat {model}` | client | `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.response.id`, `gen_ai.response.finish_reasons`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `error.type` |
| `execute_tool {name}` | internal | `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.type`, arguments, result, irreversibility, `error.type` |

The harness also records the `gen_ai.client.token.usage` and `gen_ai.client.operation.duration` histograms. They go to
whatever OpenTelemetry meter provider the app sets up, and nowhere if it sets none.

Prompts and replies are not recorded by default. Set `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`, or pass
`capture_content=True`, to add `gen_ai.system_instructions`, `gen_ai.input.messages` and `gen_ai.output.messages` to each
`chat` span in the conventions' role-and-parts format. Crossfade redacts known secret patterns before anything reaches the
artifact store, but conversation content can still contain personal data, so turn this on deliberately.

Under `crossfade.instrument` the trace context also goes out on every HTTP call the tools make. That puts the dependency
calls Crossfade measures under the tool call that made them; see [traces](traces.md).
