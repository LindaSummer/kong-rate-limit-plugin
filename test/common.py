"""Standard-library HTTP and RESP helpers for setup and pytest."""
import json
import os
import socket
import time
import urllib.error
import urllib.request
import uuid

PROXIES = os.environ.get("PROXIES", "http://kong1:8000,http://kong2:8000,http://kong3:8000").split(",")
ADMIN = os.environ.get("ADMIN", "http://kong1:8001")


def http(url, method="GET", data=None, headers=None):
    headers = dict(headers or {})
    if data is not None:
        data = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=8)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            body = raw.decode(errors="replace")
        return response.status, dict(response.headers), body


def admin(path, method="GET", data=None):
    status, _, body = http(ADMIN + path, method, data)
    if not 200 <= status < 300:
        raise AssertionError(f"Admin API {method} {path}: {status} {body}")
    return body


def redis(*args):
    with socket.create_connection((os.environ.get("REDIS_HOST", "redis"), 6379), timeout=10) as conn:
        parts = [str(arg).encode() if not isinstance(arg, bytes) else arg for arg in args]
        conn.sendall(b"*%d\r\n" % len(parts) + b"".join(b"$%d\r\n" % len(part) + part + b"\r\n" for part in parts))
        reader = conn.makefile("rb")

        def read():
            line = reader.readline()
            kind, value = line[:1], line[1:-2]
            if kind == b"-":
                raise AssertionError(value.decode())
            if kind == b"+":
                return value.decode()
            if kind == b":":
                return int(value)
            if kind == b"$":
                size = int(value)
                if size == -1:
                    return None
                result = reader.read(size)
                reader.read(2)
                return result.decode()
            if kind == b"*":
                return [read() for _ in range(int(value))]
            raise AssertionError(f"invalid RESP: {line!r}")

        return read()


def bootstrap():
    """Install the default window demo and wait for every gateway to adopt it."""
    name = "rate-limiting-sliding-window"
    service = admin("/services/window-demo", "PUT", {"url": "http://echo:8080"})
    plugins = admin('/services/' + service["id"] + '/plugins')["data"]
    existing = next((plugin for plugin in plugins if plugin["name"] == name), None)
    payload = {"name": name, "config": {
        "redis": {"host": "redis", "timeout": 150},
        "limits": [
            {"algorithm": "fixed_window", "window_ms": 1000, "limit": 5, "dimensions": ["client_id"]},
            {"algorithm": "sliding_window", "window_ms": 60000, "limit": 30, "dimensions": ["client_id"]},
        ]}}
    plugin = admin('/plugins/' + existing["id"], "PATCH", payload) if existing else admin(
        '/services/' + service["id"] + '/plugins', "POST", payload)
    for suffix in ("a", "b"):
        admin('/routes/window-demo-' + suffix, "PUT", {
            "paths": ['/window/' + suffix], "strip_path": False,
            "service": {"id": service["id"]}})
    started = time.monotonic()
    stable = 0
    while time.monotonic() - started < 30:
        responses = [http(url + "/window/a", headers={"X-Client-Id": "bootstrap-" + uuid.uuid4().hex})
                     for url in PROXIES]
        if all(status == 200 and headers.get("X-RateLimit-Mode") == "redis"
               and headers.get("X-RateLimit-Remaining") == "4" for status, headers, _ in responses):
            stable += 1
            if stable >= 5 and time.monotonic() - started >= 1.5:
                return plugin
        else:
            stable = 0
        time.sleep(0.1)
    raise AssertionError(f"initial route/plugin propagation timed out: {responses}")
