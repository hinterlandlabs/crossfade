"""Order service: takes orders, prices them from the supplier catalog, reserves stock, takes payment.

Dependencies (reached through $WORLD_URL): the supplier catalog, stock reservations and payments.
Persistent state: orders in a local SQLite database under $APP_DATA_DIR.
"""

import json
import os
import socketserver
import sqlite3
import threading
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from opentelemetry import trace

tracer = trace.get_tracer("order-service")
SUPPLIER = os.environ.get("WORLD_URL", "http://127.0.0.1:9000")
DB = os.path.join(os.environ.get("APP_DATA_DIR", "."), "orders.db")
_db_lock = threading.Lock()


class OutOfStock(Exception):
    pass


def supplier(method, path, body=None):
    with tracer.start_as_current_span(f"supplier {method} {path.split('?')[0]}") as span:
        req = urllib.request.Request(SUPPLIER + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                span.set_attribute("http.status_code", r.status)
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            span.set_attribute("http.status_code", e.code)
            return e.code, json.loads(e.read() or b"null")


def init_db():
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    with sqlite3.connect(DB) as c:
        c.execute("CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, customer TEXT, lines TEXT, total_cents INTEGER, status TEXT)")


def fetch_products(lines):
    """One catalog call for every SKU in the order: price and stock together."""
    skus = ",".join(sorted({line["sku"] for line in lines}))
    _, products = supplier("GET", f"/products?skus={skus}")
    by_sku = {p["sku"]: p for p in products}
    for line in lines:
        if line["sku"] not in by_sku:
            raise LookupError(line["sku"])
    return by_sku


def reserve(order_id, lines):
    for line in lines:
        status, _ = supplier("POST", "/reservations", {"order_ref": order_id, "sku": line["sku"], "qty": line["qty"]})
        if status == 409:
            raise OutOfStock(line["sku"])


def create_order(payload):
    with tracer.start_as_current_span("create_order") as span:
        order_id = "ord-" + uuid.uuid4().hex[:10]
        span.set_attribute("order.id", order_id)
        lines = payload["lines"]
        products = fetch_products(lines)
        for line in lines:
            if products[line["sku"]]["stock"] < line["qty"]:
                raise OutOfStock(line["sku"])
        reserve(order_id, lines)
        priced = [{**line, "price_cents": products[line["sku"]]["price_cents"]} for line in lines]
        total = sum(line["price_cents"] * line["qty"] for line in priced)
        supplier("POST", "/payments", {"order_ref": order_id, "amount_cents": total})
        with _db_lock, sqlite3.connect(DB) as c:
            c.execute("INSERT INTO orders VALUES (?,?,?,?,?)", (order_id, payload.get("customer", ""), json.dumps(priced), total, "confirmed"))
        return {"order_id": order_id, "total_cents": total, "status": "confirmed"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self.send(200, {"ok": True})
        if self.path.startswith("/orders/"):
            with sqlite3.connect(DB) as c:
                row = c.execute("SELECT id, customer, lines, total_cents, status FROM orders WHERE id=?", (self.path.split("/")[2],)).fetchone()
            if not row:
                return self.send(404, {"error": "not found"})
            return self.send(200, {"order_id": row[0], "customer": row[1], "lines": json.loads(row[2]), "total_cents": row[3], "status": row[4]})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/orders":
            return self.send(404, {"error": "not found"})
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        try:
            self.send(201, create_order(payload))
        except OutOfStock as e:
            self.send(409, {"error": "out_of_stock", "sku": str(e)})
        except LookupError as e:
            self.send(422, {"error": "unknown_sku", "sku": str(e)})


class Server(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind looks up the host's full name, a reverse DNS call that can hang (it did on macOS CI)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = str(self.server_address[0]), self.server_address[1]


if __name__ == "__main__":
    init_db()
    Server(("127.0.0.1", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
