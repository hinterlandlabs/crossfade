# Local development

```sh
uv sync
uv run ruff check src tests
uv run mypy
uv run pytest                     # everything, including end-to-end experiments (~2 minutes)
CROSSFADE_SKIP_SLOW=1 uv run pytest   # unit and integration only
```

Layout: `src/crossfade` (package), `examples/` (two independently written apps with candidate changes), `tests/unit`,
`tests/integration`, `tests/end_to_end`, `docs/`.

End-to-end tests materialize each example into a temporary git repo and run real experiments, so they need `git` and free
localhost ports. Nothing reaches the network.
