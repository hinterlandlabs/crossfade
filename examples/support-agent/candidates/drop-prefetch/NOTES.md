Drop the per-turn order prefetch

The app fetches the mentioned order and pastes it into the prompt, but the system prompt already makes the model call lookup_order. The prefetch is a duplicate dependency call plus a few hundred tokens per model call.
