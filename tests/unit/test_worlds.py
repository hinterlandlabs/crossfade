import importlib.util
from pathlib import Path

import httpx

from crossfade.worlds.base import FailureRule, WorldServer

ROOT = Path(__file__).resolve().parents[2]


def load(example: str, cls: str):
    spec = importlib.util.spec_from_file_location(f"w_{example.replace('-', '_')}", ROOT / "examples" / example / "crossfade" / "world.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, cls)


Supplier = load("order-service", "SupplierWorld")
Support = load("support-agent", "SupportWorld")


def test_writes_change_later_reads():
    w = Supplier(latency_scale=0)
    assert w.handle("GET", "/products/sku-6", None).body["stock"] == 2
    assert w.handle("POST", "/reservations", {"sku": "sku-6", "qty": 2, "order_ref": "o1"}).status == 201
    assert w.handle("GET", "/products/sku-6", None).body["stock"] == 0
    assert w.handle("GET", "/products?skus=sku-6", None).body[0]["stock"] == 0  # batch view agrees with the single view
    assert w.handle("POST", "/reservations", {"sku": "sku-6", "qty": 1, "order_ref": "o2"}).status == 409
    assert w.invariants() == []


def test_snapshot_restore_and_reset():
    w = Supplier(latency_scale=0)
    snap = w.snapshot()
    d0 = w.digest(snap)
    w.handle("POST", "/reservations", {"sku": "sku-1", "qty": 5, "order_ref": "o"})
    assert w.digest(w.snapshot()) != d0
    w.restore(snap)
    assert w.digest(w.snapshot()) == d0
    w.handle("POST", "/payments", {"order_ref": "o", "amount_cents": 1})
    w.reset()
    assert w.digest(w.snapshot()) == d0 and w.meters["calls_total"] == 0


def test_same_seed_same_world():
    assert Supplier(seed=3).digest(Supplier(seed=3).snapshot()) == Supplier(seed=3).digest(Supplier(seed=3).snapshot())


def test_seeded_failure_injection_is_reproducible():
    def statuses(seed):
        w = Supplier(seed=seed, failures=[FailureRule(method="POST", pattern="/payments", status=503, rate=0.5)], latency_scale=0)
        return [w.handle("POST", "/payments", {"order_ref": "o", "amount_cents": 1}).status for _ in range(20)]

    assert statuses(1) == statuses(1)
    assert 503 in statuses(1) and 201 in statuses(1)


def test_unsupported_operation_is_an_explicit_coverage_gap():
    w = Supplier(latency_scale=0)
    r = w.handle("DELETE", "/products/sku-1", None)
    assert r.status == 501 and r.body["coverage_gap"] is True
    assert w.meters["calls_unsupported"] == 1


def test_clock_is_simulated_and_controls_behavior():
    w = Support(latency_scale=0)
    age = w.handle("GET", "/orders/O-1009", None).body["days_since_delivery"]
    w.advance(2 * 86400)
    assert w.handle("GET", "/orders/O-1009", None).body["days_since_delivery"] == age + 2


def test_invariants_detect_inconsistent_state():
    w = Supplier(latency_scale=0)
    w.state["products"]["sku-1"]["stock"] = -1
    assert any("negative" in v for v in w.invariants())


def test_server_control_endpoints_need_the_token():
    w = Supplier(latency_scale=0)
    with WorldServer(w) as srv, httpx.Client(trust_env=False) as c:
        assert c.get(srv.url + "/products/sku-1").json()["sku"] == "sku-1"
        assert c.get(srv.url + "/_world/state").status_code == 403
        assert c.get(srv.url + "/_world/state", headers={"X-World-Token": srv.token}).status_code == 200


def test_simulated_model_follows_the_prompt():
    w = Support(latency_scale=0)
    msgs = [{"role": "user", "content": "refund O-1007 please"},
            {"role": "assistant", "tool_call": {"name": "lookup_order", "args": {"order_id": "O-1007"}}},
            {"role": "tool", "name": "lookup_order", "content": w.handle("GET", "/orders/O-1007", None).body}]  # 60 days old
    with_rule = w.handle("POST", "/v1/complete", {"system": "Customers may return items within the 30-day return window.", "messages": msgs, "tools": []}).body
    without = w.handle("POST", "/v1/complete", {"system": "You are a support agent.", "messages": msgs, "tools": []}).body
    assert with_rule["type"] == "text" and "window" in with_rule["text"]
    assert without["type"] == "tool_call" and without["tool"] == "issue_refund"
    assert w.meters["model_calls"] == 2 and w.meters["model_input_tokens"] > 0
