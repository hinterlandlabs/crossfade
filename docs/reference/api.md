# Local API

`crossfade serve` starts the API and UI on `127.0.0.1:8700`. OpenAPI: `/v1/openapi.json`, interactive docs `/v1/docs`.
Read endpoints are open on localhost. Mutating endpoints require `Authorization: Bearer <token>`, with the token in
`.crossfade/api-token` (created with mode 0600). Lists are paginated with `limit` and `offset` and return
`{items, total, limit, offset}`.

| Method | Path | |
|---|---|---|
| GET | `/v1/project` | project, locked bundle digest, latest discovery manifest, object counts |
| GET | `/v1/manifest` | fresh discovery manifest |
| GET | `/v1/revisions` | registered revisions |
| GET | `/v1/experiments` | history with decisions |
| GET | `/v1/experiments/{id}` | experiment, hypothesis, revisions, patch, runs, evaluation, decision, events |
| POST | `/v1/experiments` | create `{baseline, candidate, name?, trials?, hypothesis?}` (auth) |
| POST | `/v1/experiments/{id}/run` | run in the background; poll the experiment (auth) |
| GET | `/v1/runs`, `/v1/runs/{id}` | runs; outcome (responses, world state, meters) and log tail |
| GET | `/v1/runs/{id}/trace` | span tree with measured dependency calls under the spans that made them, the calls as a flat list, coverage |
| GET | `/v1/experiments/{id}/diff?scenario=` | what the candidate did differently, per call path (holdouts excluded) |
| GET | `/v1/traces/diff?a=&b=` | the same for two runs (403 for holdout runs) |
| GET | `/v1/traces/search?name=&status=` | span index |
| POST | `/v1/traces` | import OTLP/JSON (auth) |
| GET | `/v1/scenarios` | scenarios; holdout contents are withheld |
| GET | `/v1/worlds` | world routes, fixtures, entity counts, fidelity, snapshot digest |
| GET | `/v1/findings` | findings |
| POST | `/v1/hypotheses` | record a hypothesis (auth) |
| GET | `/v1/artifacts/{digest}` | raw artifact (digest-verified) |
| GET | `/v1/events?subject=` | ledger events |
| GET | `/v1/plugins` | plugin registry with health |
| GET | `/v1/verify` | hash chain and artifact integrity |
