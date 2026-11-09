"""Experiment-only upstream: record actual arrivals, excluding readiness probes."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Observer(ThreadingHTTPServer):
    request_queue_size = 256

    def __init__(self, address):
        super().__init__(address, Handler)
        self.events = []
        self.event_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        received_ns = time.time_ns()
        if self.path == "/events":
            with self.server.event_lock:
                payload = list(self.server.events)
        elif self.path == "/health":
            payload = {"ok": True}
        else:
            request_id = self.headers.get("X-Experiment-Request-Id")
            if request_id:
                with self.server.event_lock:
                    self.server.events.append({"request_id": request_id,
                                               "received_ns": received_ns})
            payload = {"ok": True}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    Observer(("0.0.0.0", 8080)).serve_forever()
