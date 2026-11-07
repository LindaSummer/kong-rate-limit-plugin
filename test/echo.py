import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Echo(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True

    def respond(self):
        status = 429 if self.path.startswith("/upstream429") else 503 if self.path.startswith("/upstream503") else 200
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode(errors="replace")
        payload = json.dumps({"method": self.command, "path": self.path,
                              "headers": dict(self.headers), "body": body}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if status != 200:
            self.send_header("Retry-After", "Wed, 21 Oct 2037 07:28:00 GMT" if "date" in self.path else "7")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = respond


ThreadingHTTPServer(("0.0.0.0", 8080), Echo).serve_forever()
