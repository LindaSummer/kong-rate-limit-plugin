"""Request and policy helpers shared by the gateway tests."""
import concurrent.futures
import time
import uuid
from common import PROXIES, http

NAME = "rate-limiting-sliding-window"
RUN = uuid.uuid4().hex[:10]
CONFIG_TIMEOUT_SECONDS = 30

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
