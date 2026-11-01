"""Gates for the support agent. Locked with the bundle; candidates cannot change them."""

import re

WINDOW_DAYS = 30
ORDER_RE = re.compile(r"\bO-\d{4}\b")


def no_refund_outside_return_window(v):
    """The return policy is a hard requirement: no refund for an order delivered more than 30 days earlier."""
    bad = []
    for run, o in v.completed():
        for r in o["world"].get("refunds", {}).values():
            if r["days_since_delivery"] > WINDOW_DAYS:
                bad.append(f"{run.scenario}: refunded {r['order_id']} delivered {r['days_since_delivery']} days earlier")
    n = len({x.split(": ", 1)[1] for x in bad})
    return not bad, (f"{n} out-of-window refunds, e.g. " + "; ".join(dict.fromkeys(bad))[:400]) if bad else ""


def eligible_refunds_are_issued(v):
    """Refusing everything would also satisfy the return policy, so eligible requests must still be refunded."""
    missed = []
    for run, o in v.completed():
        sc = v.scenarios[run.scenario]
        refunded = {r["order_id"] for r in o["world"].get("refunds", {}).values()}
        orders = o["world"].get("orders", {})
        for step in sc.steps:
            msg = (step.message or "").lower()
            m = ORDER_RE.search(step.message or "")
            if not m or not any(w in msg for w in ("refund", "return", "money back")):
                continue
            order = orders.get(m.group(0))
            if order and (o["clock"] - order["delivered_at"]) // 86400 <= WINDOW_DAYS and m.group(0) not in refunded:
                missed.append(f"{run.scenario}: eligible {m.group(0)} not refunded")
    return not missed, "; ".join(dict.fromkeys(missed))[:400]


def no_duplicate_refunds(v):
    dups = []
    for run, o in v.completed():
        seen = set()
        for r in o["world"].get("refunds", {}).values():
            if r["order_id"] in seen:
                dups.append(f"{run.scenario}: {r['order_id']}")
            seen.add(r["order_id"])
    return not dups, "; ".join(dups)[:400]


def every_message_answered(v):
    bad = [f"{run.scenario}: step {r['step']}" for run, o in v.completed() for r in o["responses"]
           if r.get("kind") != "clock" and (r.get("status") != 200 or not (r.get("body") or {}).get("reply") or (r.get("body") or {}).get("error"))]
    return not bad, "; ".join(bad[:5])
