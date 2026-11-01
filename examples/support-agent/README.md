# Example: support-agent

A customer support agent built on the Crossfade harness (`agent.py`), with lookup and refund tools. The world
(`crossfade/world.py`) provides the order and refund APIs and a simulated model that follows the instructions in the
system prompt and reports token usage.

Candidates: `drop-prefetch` (removes a duplicate order fetch; accepted) and `trim-prompt` (much cheaper prompt that drops the
return policy; rejected for refunding outside the return window).
