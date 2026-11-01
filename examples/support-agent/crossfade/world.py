"""Support world: orders, refunds and a simulated language model, in one consistent state.

The order system and the payment provider behave like real ones: a refund API will refund any order you ask it to.
Enforcing the return policy is the agent's job, which is exactly what experiments need to check.

The model endpoint is a deterministic simulated model. It follows the instructions present in the system prompt
(for example a return-window rule) and reports token usage, so prompt changes have real behavioral and cost effects.
Token counts and prices are illustrative: roughly 4 characters per token, $3 per million input tokens and $15 per
million output tokens.
"""

import json
import re

from crossfade.models import Fidelity
from crossfade.worlds.base import Request, Response, World, WorldError, route

DAY = 86400
IN_PRICE, OUT_PRICE = 3 / 1e6, 15 / 1e6
ORDER_RE = re.compile(r"\bO-\d{4}\b")


def tokens(text: str) -> int:
    return max(1, len(text) // 4)


class SupportWorld(World):
    name = "support"
    version = "1.0"
    default_latency_ms = 4
    fidelity = Fidelity(
        implements=["order lookup with delivery age", "refunds (the provider refunds whatever it is asked to)",
                    "a simulated chat model with tool calls and token accounting"],
        from_contracts=["order and refund API shapes (written by hand for this example)"],
        simplified=["the simulated model follows prompt instructions deterministically; real models are probabilistic",
                    "token counts are estimated at 4 characters per token", "prices are illustrative"],
        unsupported=["partial refunds", "multi-turn clarification", "languages other than English"],
        divergence_risks=["a real model may ignore or over-apply instructions; run the same scenarios against a real model before trusting a change"],
    )

    def fixtures(self):
        def default(w):
            ages = [3, 12, 45, 8, 31, 20, 60, 2, 29, 33, 15, 40, 6, 27, 52, 10, 35, 18, 30, 44, 5, 25, 38, 14]
            w.state["orders"] = {}
            for i, age in enumerate(ages):
                oid = f"O-{1001 + i}"
                w.state["orders"][oid] = {"id": oid, "customer": f"cust-{i % 9}", "item": ["headphones", "kettle", "backpack", "lamp"][i % 4],
                                          "amount_cents": 1500 + (i * 737) % 9000, "delivered_at": w.clock - age * DAY}
            w.state["refunds"] = {}

        return {"default": default}

    def days_since(self, order):
        return int((self.clock - order["delivered_at"]) // DAY)

    # ----- order and refund APIs -----

    @route("GET", "/orders/{order_id}")
    def get_order(self, req: Request) -> Response:
        o = self.table("orders").get(req.params["order_id"])
        if not o:
            raise WorldError(404, "no such order")
        return Response(200, {**o, "days_since_delivery": self.days_since(o),
                              "refunded": any(r["order_id"] == o["id"] for r in self.table("refunds").values())})

    @route("POST", "/refunds", latency_ms=12)
    def refund(self, req: Request) -> Response:
        b = req.body or {}
        o = self.table("orders").get(b.get("order_id"))
        if not o:
            raise WorldError(404, "no such order")
        if any(r["order_id"] == o["id"] for r in self.table("refunds").values()):
            raise WorldError(409, "already refunded")
        rid = f"rf-{len(self.table('refunds')) + 1}"
        self.table("refunds")[rid] = {"id": rid, "order_id": o["id"], "amount_cents": int(b.get("amount_cents", o["amount_cents"])),
                                      "days_since_delivery": self.days_since(o), "at": self.clock}
        return Response(201, {"id": rid, "status": "refunded"})

    # ----- simulated model -----

    @route("POST", "/v1/complete", latency_ms=0)
    def complete(self, req: Request) -> Response:
        b = req.body or {}
        system, messages = b.get("system", ""), b.get("messages", [])
        reply = self._decide(system, messages)
        tin = tokens(system) + sum(tokens(json.dumps(m)) for m in messages) + sum(tokens(json.dumps(t)) for t in b.get("tools", []))
        tout = tokens(json.dumps(reply))
        self.meter("model_calls")
        self.meter("model_input_tokens", tin)
        self.meter("model_output_tokens", tout)
        self.meter("model_cost_usd", tin * IN_PRICE + tout * OUT_PRICE)
        import time

        time.sleep((25 + tin / 400) / 1000 * self.latency_scale)  # inference time grows with prompt size
        return Response(200, {**reply, "usage": {"input_tokens": tin, "output_tokens": tout}, "model": "simulated-support-1", "system_name": "simulated"})

    def _decide(self, system: str, messages: list) -> dict:
        last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
        text = messages[last_user]["content"]
        turn = messages[last_user + 1:]
        results = {m["name"]: m["content"] for m in turn if m["role"] == "tool"}
        found = ORDER_RE.search(text)
        sys_l = system.lower()
        window = None
        if "return window" in sys_l:
            m = re.search(r"(\d+)[- ]day", sys_l)
            window = int(m.group(1)) if m else 30
        order = results.get("lookup_order")
        if order is None and "verify order details with lookup_order" not in sys_l and found:
            # use order details already provided in the system prompt, if any
            m = re.search(r"order on file: (\{.*?\})", system, re.I | re.S)
            if m:
                order = json.loads(m.group(1))
        if not found:
            return {"type": "text", "text": "I can help with refunds and order status. Which order is this about?"}
        oid = found.group(0)
        wants_refund = any(w in text.lower() for w in ("refund", "return", "money back"))
        if order is None:
            return {"type": "tool_call", "tool": "lookup_order", "args": {"order_id": oid}}
        if isinstance(order, dict) and order.get("error"):
            return {"type": "text", "text": f"I couldn't find order {oid}."}
        if not wants_refund:
            return {"type": "text", "text": f"Order {oid} ({order['item']}) was delivered {order['days_since_delivery']} days ago."}
        if order.get("refunded") and "issue_refund" not in results:
            return {"type": "text", "text": f"Order {oid} has already been refunded."}
        if window is not None and order["days_since_delivery"] > window:
            return {"type": "text", "text": f"Order {oid} was delivered {order['days_since_delivery']} days ago, outside our {window}-day return window, "
                                            "so I can't refund it. I can pass this to a specialist if the item is faulty."}
        if "issue_refund" not in results:
            return {"type": "tool_call", "tool": "issue_refund", "args": {"order_id": oid, "amount_cents": order["amount_cents"]}}
        return {"type": "text", "text": f"Done. I've refunded order {oid}; you'll see it in 3 to 5 days."}

    def invariants(self):
        out = []
        orders = self.table("orders")
        for r in self.table("refunds").values():
            if r["order_id"] not in orders:
                out.append(f"refund {r['id']} references unknown order {r['order_id']}")
            elif r["amount_cents"] > orders[r["order_id"]]["amount_cents"]:
                out.append(f"refund {r['id']} exceeds the order amount")
        return out
