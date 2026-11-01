# Worlds

A world is a coherent, stateful stand-in for the dependencies an application calls. It is not a set of recorded responses.

- **One state model.** Entities live in `world.state` tables; every route reads and writes the same state, so a reservation
  lowers the stock the catalog reports, and a refund appears on the next order lookup.
- **Fixtures** set the initial state by name (`default`, …). A run records the snapshot digest it started from.
- **Clock.** `world.clock` is simulated time, independent of the wall clock. Scenarios can advance it (`kind: clock`).
- **Failures.** `FailureRule`s inject errors or timeouts on matching routes using the world's seeded RNG, so they are reproducible.
- **Snapshots.** `snapshot()`, `restore()`, `reset()`, `digest()`.
- **Invariants.** `invariants()` returns self-consistency violations (negative stock, orphan references). They are a gate.
- **Coverage.** Calls to routes the world doesn't implement return `501` with `coverage_gap: true` and are counted, so gaps
  show up instead of silently passing.
- **Fidelity.** Every world declares a `Fidelity`: what it implements, what came from contracts or traces, what was inferred
  or simplified, what is unsupported, and divergence risks.
- **Metering.** The world counts calls per route and anything the plugin meters (`model_input_tokens`, `model_cost_usd`).
  These are the trusted numbers experiments compare.

Writing one:

```python
from crossfade.models import Fidelity
from crossfade.worlds.base import Request, Response, World, WorldError, route

class PaymentsWorld(World):
    name, version, default_latency_ms = "payments", "1.0", 5
    fidelity = Fidelity(implements=["charges"], unsupported=["refunds"])

    def fixtures(self):
        return {"default": lambda w: w.state.setdefault("charges", {})}

    @route("POST", "/charges")
    def charge(self, req: Request) -> Response:
        if req.body.get("amount", 0) <= 0:
            raise WorldError(422, "amount must be positive")
        cid = f"ch_{len(self.table('charges')) + 1}"
        self.table("charges")[cid] = {"id": cid, **req.body}
        return Response(201, {"id": cid})
```

Generating worlds automatically from traces and contracts is an open research problem. Today worlds are written by people
(or agents) and reviewed before they are locked.
