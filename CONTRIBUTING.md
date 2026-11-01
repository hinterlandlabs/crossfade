# Contributing to Crossfade

Thanks for helping. A few things make reviews fast.

## Setup

```sh
uv sync
uv run ruff check src tests && uv run mypy && uv run pytest
```

## What makes a good change

- **A real use case.** Every feature should be exercised by a test that runs real code, not a mock of Crossfade itself.
- **No fabricated numbers.** Any performance claim in docs or PRs comes from an experiment you ran, with the command to reproduce it.
- **Keep the core small.** The trusted core (models, ledger, artifacts, evaluation, experiments, revisions) changes rarely and
  gets extra review. New capabilities usually belong in a plugin, a world, or the harness.
- **Evaluation integrity.** Changes must not let a candidate influence its own evaluation, see holdouts, or write decisions.

## Good first contributions

- World plugins for common dependencies (payments, email, queues, object storage), with honest `fidelity` statements and tests.
- Gate libraries for common invariants.
- Discovery for more languages and frameworks.
- New reference applications that exercise different execution patterns.

## Pull requests

Describe the problem, the change and how you verified it. CI runs lint, type checks and the full test suite, including
end-to-end experiments. By contributing you agree your work is licensed under Apache 2.0.

Please follow the [code of conduct](CODE_OF_CONDUCT.md).
