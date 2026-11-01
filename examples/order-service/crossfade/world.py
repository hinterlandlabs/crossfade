"""Supplier world: catalog, stock, reservations and payments in one consistent state model.

A reservation decrements stock, so a later catalog read sees the lower stock. Payments reference orders.
Every call costs simulated latency, so redundant calls show up in measurements.
"""

from crossfade.models import Fidelity
from crossfade.worlds.base import Request, Response, World, WorldError, route


class SupplierWorld(World):
    name = "supplier"
    version = "1.0"
    default_latency_ms = 6
    fidelity = Fidelity(
        implements=["catalog lookup by SKU", "batch catalog lookup", "stock reservations with insufficient-stock rejection", "payments"],
        from_contracts=["supplier API v2 request and response shapes (written by hand for this example)"],
        simplified=["no reservation expiry", "payments always succeed unless a failure is injected", "flat per-call latency"],
        unsupported=["refunds", "partial reservations", "currency other than integer cents"],
        divergence_risks=["real supplier latency varies with load; this world uses a fixed delay per call"],
    )

    def fixtures(self):
        def default(w):
            w.state["products"] = {
                sku: {"sku": sku, "price_cents": price, "stock": stock}
                for sku, price, stock in [("sku-1", 1299, 50), ("sku-2", 499, 50), ("sku-3", 2450, 20), ("sku-4", 899, 30), ("sku-5", 15000, 10), ("sku-6", 3200, 2)]
            }
            w.state["reservations"] = {}
            w.state["payments"] = {}
            w.state["initial_stock"] = {s: p["stock"] for s, p in w.state["products"].items()}

        return {"default": default}

    @route("GET", "/products/{sku}")
    def product(self, req: Request) -> Response:
        p = self.table("products").get(req.params["sku"])
        if not p:
            raise WorldError(404, "unknown sku")
        return Response(200, dict(p))

    @route("GET", "/products", latency_ms=8)
    def products(self, req: Request) -> Response:
        skus = req.query.get("skus", [""])[0].split(",")
        found = [dict(self.table("products")[s]) for s in skus if s in self.table("products")]
        return Response(200, found)

    @route("POST", "/reservations")
    def reserve(self, req: Request) -> Response:
        b = req.body or {}
        p = self.table("products").get(b.get("sku"))
        if not p:
            raise WorldError(404, "unknown sku")
        qty = int(b.get("qty", 0))
        if qty <= 0:
            raise WorldError(422, "qty must be positive")
        if p["stock"] < qty:
            raise WorldError(409, "insufficient stock")
        p["stock"] -= qty
        rid = f"res-{len(self.table('reservations')) + 1}"
        self.table("reservations")[rid] = {"id": rid, "order_ref": b.get("order_ref"), "sku": b["sku"], "qty": qty}
        return Response(201, {"id": rid})

    @route("POST", "/payments", latency_ms=10)
    def pay(self, req: Request) -> Response:
        b = req.body or {}
        pid = f"pay-{len(self.table('payments')) + 1}"
        self.table("payments")[pid] = {"id": pid, "order_ref": b.get("order_ref"), "amount_cents": int(b.get("amount_cents", 0))}
        return Response(201, {"id": pid})

    def invariants(self):
        out = []
        for sku, p in self.table("products").items():
            if p["stock"] < 0:
                out.append(f"{sku} stock is negative ({p['stock']})")
            reserved = sum(r["qty"] for r in self.table("reservations").values() if r["sku"] == sku)
            if reserved + p["stock"] != self.state["initial_stock"][sku]:
                out.append(f"{sku}: reserved {reserved} + stock {p['stock']} != initial {self.state['initial_stock'][sku]}")
        for r in self.table("reservations").values():
            if r["sku"] not in self.table("products"):
                out.append(f"reservation {r['id']} references unknown sku {r['sku']}")
        return out
