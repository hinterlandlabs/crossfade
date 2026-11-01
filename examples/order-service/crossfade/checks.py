"""Gates for the order service. Locked with the bundle; candidates cannot change them."""


def _confirmed(outcome):
    return [r["body"] for r in outcome["responses"] if r.get("status") == 201 and isinstance(r.get("body"), dict)]


def every_confirmed_order_is_reserved(v):
    """An order may only be confirmed if the supplier holds stock for every unit of it."""
    problems = []
    for run, o in v.completed():
        reserved = {}
        for r in o["world"].get("reservations", {}).values():
            reserved[r["order_ref"]] = reserved.get(r["order_ref"], 0) + r["qty"]
        for order in _confirmed(o):
            if reserved.get(order["order_id"], 0) == 0:
                problems.append(f"{run.scenario}: {order['order_id']} confirmed without a stock reservation")
    return not problems, "; ".join(problems[:5])


def payments_match_orders(v):
    """Every confirmed order is paid exactly its total, once."""
    problems = []
    for run, o in v.completed():
        paid = {}
        for p in o["world"].get("payments", {}).values():
            paid.setdefault(p["order_ref"], []).append(p["amount_cents"])
        for order in _confirmed(o):
            if paid.get(order["order_id"]) != [order["total_cents"]]:
                problems.append(f"{run.scenario}: {order['order_id']} total {order['total_cents']} but payments {paid.get(order['order_id'])}")
    return not problems, "; ".join(problems[:5])


def no_errors(v):
    errs = [f"{run.scenario}: step {r['step']} returned {r['status']}" for run, o in v.completed() for r in o["responses"]
            if r.get("status", 200) >= 500 or r.get("status") == 0]
    return not errs, "; ".join(errs[:5])
