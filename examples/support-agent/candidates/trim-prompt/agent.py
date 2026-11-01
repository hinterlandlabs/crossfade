"""Customer support agent built on the Crossfade harness.

It answers order questions and issues refunds through two tools. Before each turn it fetches the order the
customer mentions and adds it to the prompt as context.
"""

import json
import os
import re
import socketserver
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from crossfade.harness import Agent, Constraints, HTTPModelProvider, tool

WORLD = os.environ.get("WORLD_URL", "http://127.0.0.1:9000")
ORDER_RE = re.compile(r"\bO-\d{4}\b")

SYSTEM = """You are Meridian's support agent. Be brief and friendly.
Always verify order details with lookup_order before acting. Use issue_refund to refund an order.
"""

def api(method, path, body=None):
    req = urllib.request.Request(WORLD + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return {"error": e.code, **(json.loads(e.read() or b"{}") or {})}


@tool(description="Look up an order by id: item, amount, delivery age, refund status.", order_id="string")
def lookup_order(order_id):
    return api("GET", f"/orders/{order_id}")


@tool(description="Refund an order in full. Irreversible.", irreversible=True, order_id="string", amount_cents="number")
def issue_refund(order_id, amount_cents):
    return api("POST", "/refunds", {"order_id": order_id, "amount_cents": amount_cents})


def order_context(conversation_id, message, memory):
    """Fetch the order mentioned in the message so the model has it up front."""
    m = ORDER_RE.search(message)
    if not m:
        return ""
    order = api("GET", f"/orders/{m.group(0)}")
    return "Order on file: " + json.dumps(order)


agent = Agent(HTTPModelProvider(WORLD), SYSTEM, [lookup_order, issue_refund], Constraints(max_steps=6, max_irreversible_per_turn=1),
              name="support")


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
        self.send(200, {"ok": True}) if self.path == "/health" else self.send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/chat":
            return self.send(404, {"error": "not found"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        result = agent.respond(body.get("conversation_id", "default"), body.get("message", ""))
        self.send(200, {"reply": result.text, "steps": result.steps, "tool_calls": [c["tool"] for c in result.tool_calls], "error": result.error})


class Server(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind looks up the host's full name, a reverse DNS call that can hang (it did on macOS CI)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = str(self.server_address[0]), self.server_address[1]


if __name__ == "__main__":
    Server(("127.0.0.1", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
