"""The host runner restarts kong1 between prepare and verify."""
import json
import sys
import time
import uuid
from common import PROXIES, admin, http, redis

key = "rlsw:demo-restart-check"
if sys.argv[1] == "prepare":
    name = "window-restart-" + uuid.uuid4().hex[:10]
    service = admin("/services", "POST", {"name": name, "url": "http://echo:8080"})
    route = admin("/routes", "POST", {"paths": ["/" + name], "strip_path": False, "service": {"id": service["id"]}})
    plugin = admin('/services/' + service["id"] + '/plugins', "POST", {
        "name": "rate-limiting-sliding-window", "config": {
            "redis": {"host": "redis", "timeout": 150},
            "limits": [{"algorithm": "sliding_window", "limit": 2,
                        "window_ms": 86400000, "dimensions": ["client_id"]}]}})
    fixture = {"service": service["id"], "route": route["id"], "plugin": plugin["id"],
               "path": "/" + name, "client": name}
    redis("SET", key, json.dumps(fixture), "EX", 180)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        responses = [http(url + fixture["path"], headers={"X-Client-Id": uuid.uuid4().hex}) for url in PROXIES]
        if all(r[0] == 200 and r[1].get("X-RateLimit-Remaining") == "1" for r in responses):
            break
        time.sleep(0.1)
    assert all(r[1].get("X-RateLimit-Remaining") == "1" for r in responses), responses
    assert http(PROXIES[0]+fixture["path"], headers={"X-Client-Id": fixture["client"]})[0] == 200
    print("PASS window restart fixture consumed one of two admissions")
else:
    raw = redis("GET", key)
    assert raw, "restart fixture expired"
    fixture = json.loads(raw)
    try:
        headers = {"X-Client-Id": fixture["client"]}
        assert http(PROXIES[0]+fixture["path"], headers=headers)[0] == 200
        assert all(http(url+fixture["path"], headers=headers)[0] == 429 for url in PROXIES)
        print("PASS Kong restart preserved sliding-window Redis budget")
    finally:
        admin('/plugins/' + fixture["plugin"], "DELETE")
        admin('/routes/' + fixture["route"], "DELETE")
        admin('/services/' + fixture["service"], "DELETE")
        redis("DEL", key)
