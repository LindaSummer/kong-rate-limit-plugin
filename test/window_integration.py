"""New-plugin integration suite; serial execution against the demo only."""
import concurrent.futures
from http.client import HTTPConnection
import json
import time
from urllib.parse import urlsplit
import uuid

from common import ADMIN, PROXIES, admin, http, redis

NAME = "rate-limiting-sliding-window"
RUN = uuid.uuid4().hex[:10]
CONFIG_TIMEOUT_SECONDS = 30
services, routes, plugins = [], [], []
passed = 0


def check(value, message):
    if not value:
        raise AssertionError(message)


def done(message):
    global passed
    passed += 1
    print("PASS", message, flush=True)


def limit(algorithm="fixed_window", count=5, window=86400000, dimensions=None):
    return {"algorithm": algorithm, "limit": count, "window_ms": window,
            "dimensions": dimensions or ["client_id"]}


def request(path, node=0, client="alice"):
    return http(PROXIES[node] + path, headers={} if client is None else {"X-Client-Id": RUN + client})


def burst(path, count=60, client="alice"):
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
        return list(pool.map(lambda i: request(path, i % 3, client), range(count)))


def config(limits):
    return {"redis": {"host": "redis", "timeout": 150}, "limits": limits}


def setup(case, limits, paths=None, warm=True, warm_status=200):
    name = f"window-{RUN}-{case}"
    service = admin("/services", "POST", {"name": name, "url": "http://echo:8080"})
    services.append(service["id"])
    paths = paths or [f"/{name}/a", f"/{name}/b"]
    route_ids = []
    for path in paths:
        route = admin("/routes", "POST", {"paths": [path], "strip_path": False,
                                          "service": {"id": service["id"]}})
        routes.append(route["id"])
        route_ids.append(route["id"])
    plugin = admin(f'/services/{service["id"]}/plugins', "POST", {"name": NAME, "config": config(limits)})
    plugins.append(plugin["id"])
    if warm:
        wait_policy(paths[0], min(r["limit"] for r in limits) - 1, warm_status)
    else:
        time.sleep(2)
    return paths, plugin, route_ids


def wait_policy(path, expected_remaining, expected_status=200):
    started = time.monotonic()
    # Route and plugin caches converge independently on different gateways.
    # Match the initial bootstrap allowance without relaxing quota assertions.
    deadline = started + CONFIG_TIMEOUT_SECONDS
    stable = 0
    while time.monotonic() < deadline:
        client = "warm-" + uuid.uuid4().hex
        # Distinct callers per node keep convergence probes independent.
        results = [request(path, i, client + str(i)) for i in range(3)]
        if all(r[0] == expected_status and int(r[1].get("X-RateLimit-Remaining", -1)) == expected_remaining for r in results):
            stable += 1
            if stable >= 5 and time.monotonic() - started >= 1.5:
                return
        else:
            stable = 0
        time.sleep(0.1)
    raise AssertionError(f"policy propagation failed: {results}")


try:
    for algorithm in ("fixed_window", "sliding_window"):
        paths, plugin, route_ids = setup(algorithm, [limit(algorithm, 12)])
        responses = burst(paths[0])
        check(sum(r[0] == 200 for r in responses) == 12,
              f"{algorithm}: shared capacity: " + str([(r[0], r[1].get('X-RateLimit-Mode'), r[1].get('X-RateLimit-Remaining')) for r in responses]))
        check(all(r[0] in (200, 429) for r in responses), "unexpected admission status")
        check({r[1]["X-Kong-Instance"] for r in responses} == {"kong1", "kong2", "kong3"}, "all nodes exercised")
        check(request(paths[1], client="alice")[0] == 429, "routes must share service budget")
        check(request(paths[1], client="bob")[0] == 200, "clients must be isolated")
        status, headers, _ = request(paths[0])
        check(status == 429 and headers["Retry-After"].isdigit() and headers["Cache-Control"] == "no-store", "denial headers")
        check(request(paths[0], client=None)[0] == 400, "ClientId is required")
        done(algorithm + " shared concurrency, service grouping, client isolation and denial headers")

    paths, plugin, route_ids = setup("composite", [limit(count=1, dimensions=["client_id", "path"])])
    check(request(paths[0] + "?x=1")[0] == 200, "composite first")
    check(request(paths[0] + "?x=2", 1)[0] == 429, "queries share normalized path")
    normalized = paths[0].rsplit("/", 1)[0] + "/./a"
    check(request(normalized, 2)[0] == 429, "dot segments share normalized path")
    check(request(paths[1], 2)[0] == 200, "different paths have independent budgets")
    check(request(paths[0], client="bob")[0] == 200, "different clients isolated")
    node = urlsplit(PROXIES[0])
    conn = HTTPConnection(node.hostname, node.port)
    conn.putrequest("GET", paths[0])
    conn.putheader("X-Client-Id", "a")
    conn.putheader("X-Client-Id", "b")
    conn.endheaders()
    response = conn.getresponse()
    check(response.status == 400, "duplicate ClientId rejected")
    response.read()
    conn.close()
    check(request(paths[0], client="x" * 257)[0] == 400, "oversized ClientId rejected")
    done("composite dimensions, query normalization and ClientId validation")

    paths, _, _ = setup("route-method", [limit(count=1, dimensions=["client_id", "method", "route", "service"])])
    check(request(paths[0])[0] == 200, "method/route/service first")
    check(request(paths[0] + "/child", 1)[0] == 429, "same route shares counter without path dimension")
    check(http(PROXIES[1] + paths[0], "POST", headers={"X-Client-Id": RUN + "alice"})[0] == 200, "methods split counters")
    check(request(paths[1], 2)[0] == 200, "route dimension splits service budget")
    done("method, route and service dimensions")

    paths, plugin, route_ids = setup("attempts", [limit(count=1, dimensions=["client_id", "path"]), limit(count=2)])
    check(request(paths[0])[0] == 200, "initial all-limit admission")
    check(request(paths[0], 1)[0] == 429, "tight path exhausted")
    check(request(paths[1], 2)[0] == 429, "denied attempt also consumes the broader budget")
    check(request(paths[1])[0] == 429, "broad budget exhausted")
    done("independent attempt accounting across simultaneous limits, including denials")

    paths, plugin, route_ids = setup("precedence", [limit(count=1)])
    override = admin(f'/routes/{route_ids[0]}/plugins', "POST", {"name": NAME, "config": config([limit(count=3)])})
    plugins.append(override["id"])
    wait_policy(paths[0], 2)
    check([request(paths[0], i % 3)[0] for i in range(4)] == [200, 200, 200, 429], "route override must replace service limit")
    check(request(paths[1])[0] == 200 and request(paths[1], 1)[0] == 429, "service counter separate from route scope")
    done("route precedence and configured scope isolation")

    paths, plugin, _ = setup("update", [limit(count=2)])
    check(request(paths[0])[0] == 200, "initial before update")
    admin('/plugins/' + plugin["id"], "PATCH", {"config": config([limit(count=3)])})
    wait_policy(paths[0], 2)
    check([request(paths[0], i % 3)[0] for i in range(3)] == [200, 200, 429], "threshold change preserves counts")
    admin('/plugins/' + plugin["id"], "PATCH", {"config": config([limit(count=4, window=43200000)])})
    wait_policy(paths[0], 3)
    check(request(paths[0])[0] == 200, "window shape change creates fresh state")
    done("live threshold retention and window-shape isolation")

    for invalid in ([limit(count=0)], [limit(algorithm="unknown")],
                    [limit(dimensions=["ip", "ip"])],
                    [limit(dimensions=["ip", "client_id"]), limit(dimensions=["client_id", "ip"])],
                    [limit(window=86400001)]):
        check(http(ADMIN + '/plugins/' + plugin["id"], "PATCH", {"config": config(invalid)})[0] == 400, "invalid configuration accepted")
    check(http(ADMIN + '/plugins/' + plugin["id"], "PATCH", {"consumer": {"id": str(uuid.uuid4())}})[0] == 400, "consumer scope accepted")
    done("schema rejects invalid limits, duplicates and Consumer scope")

    paths, _, _ = setup("ip", [limit(count=4, window=1000, dimensions=["ip"])], warm=False)
    # IP-only warmups necessarily spend the shared budget. Exercise all workers,
    # then start the measured burst just after the next Redis window boundary.
    deadline = time.monotonic() + CONFIG_TIMEOUT_SECONDS
    stable = 0
    while time.monotonic() < deadline:
        responses = [request(paths[0], i, client=None) for i in range(3)]
        if all("X-RateLimit-Remaining" in r[1] for r in responses):
            stable += 1
            if stable >= 15:
                break
        else:
            stable = 0
        time.sleep(0.05)
    check(stable >= 15, "IP policy did not propagate: " + str(responses))
    server_time = redis("TIME")
    now_ms = int(server_time[0])*1000 + int(server_time[1])//1000
    time.sleep((1000 - now_ms % 1000 + 30)/1000)
    responses = burst(paths[0], count=30)
    check(sum(r[0] == 200 for r in responses) == 4,
          "IP budget shared across gateways: " + str([(r[0], r[1].get("X-RateLimit-Remaining"), r[2]) for r in responses]))
    check(request(paths[1], client=None)[0] == 429, "IP limit needs no ClientId")
    done("IP budgets shared across gateways without ClientId")

    for status in (429, 503):
        paths, _, _ = setup("upstream" + str(status), [limit(count=10)],
                            paths=[f"/upstream{status}/window-{RUN}"], warm_status=status)
        for suffix, retry in (("", "7"), ("/date", "Wed, 21 Oct 2037 07:28:00 GMT")):
            response = request(paths[0] + suffix)
            check(response[0] == status and response[1]["Retry-After"] == retry, "upstream response changed")
    done("upstream 429/503 and Retry-After preserved")

    for algorithm in ("fixed_window", "sliding_window"):
        paths, _, _ = setup("outage-" + algorithm, [limit(algorithm, count=4)])
        redis("CLIENT", "PAUSE", 2200, "ALL")
        responses = burst(paths[0], count=60, client="outage")
        check(all(r[1].get("X-RateLimit-Mode") == "local" for r in responses), "outage must activate local enforcement")
        for node in ("kong1", "kong2", "kong3"):
            count = sum(r[0] == 200 and r[1]["X-Kong-Instance"] == node for r in responses)
            check(count == 4, f"{algorithm}: node {node} admitted {count}, expected 4")
        deadline = time.monotonic() + 16
        recovered = False
        while time.monotonic() < deadline:
            responses = [request(paths[0], i, "recovery" + uuid.uuid4().hex) for i in range(3)]
            if all(r[1].get("X-RateLimit-Mode") == "redis" for r in responses):
                # Stay active through the full ramp, then check stable Redis mode.
                end = time.monotonic() + 4.5
                while time.monotonic() < end:
                    responses = [request(paths[0], i, "ramp" + uuid.uuid4().hex) for i in range(3)]
                    time.sleep(0.1)
                responses = [request(paths[0], i, "closed" + uuid.uuid4().hex) for i in range(3)]
                recovered = all(r[1].get("X-RateLimit-Mode") == "redis" for r in responses)
                if recovered:
                    break
            time.sleep(0.1)
        check(recovered, "Redis recovery did not complete")
        done(algorithm + " cross-worker local fallback and gradual Redis recovery")

    paths, plugin, _ = setup("auth", [limit()])
    invalid_auth = config([limit()])
    invalid_auth["redis"]["password"] = "incorrect-password"
    admin('/plugins/' + plugin["id"], "PATCH", {"config": invalid_auth})
    deadline = time.monotonic() + CONFIG_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        responses = [request(paths[0], i, uuid.uuid4().hex) for i in range(3)]
        if all(r[0] == 200 and r[1].get("X-RateLimit-Mode") == "local" for r in responses):
            break
        time.sleep(0.1)
    check(all(r[0] == 200 and r[1].get("X-RateLimit-Mode") == "local" for r in responses),
          "authentication failure must activate local enforcement")
    time.sleep(1.5)
    responses = burst(paths[0], count=60, client="auth-fallback")
    check(all(r[1].get("X-RateLimit-Mode") == "local" for r in responses), "authentication failure bypassed local mode")
    for node in ("kong1", "kong2", "kong3"):
        count = sum(r[0] == 200 and r[1]["X-Kong-Instance"] == node for r in responses)
        check(count == 5, f"authentication fallback on {node} admitted {count}, expected 5")
    check(all(r[0] in (200,429) for r in responses), "authentication failure returned unavailable instead of enforcing locally")
    done("Redis authentication failures use per-node local budgets")

finally:
    for plugin_id in reversed(plugins):
        admin('/plugins/' + plugin_id, "DELETE")
    for route_id in reversed(routes):
        admin('/routes/' + route_id, "DELETE")
    for service_id in reversed(services):
        admin('/services/' + service_id, "DELETE")
print(json.dumps({"window_integration_groups": passed}))
