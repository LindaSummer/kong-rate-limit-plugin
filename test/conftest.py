"""Per-test Kong entities are removed even when an assertion fails."""
import time
import pytest
from common import admin
from window_support import NAME, RUN, config, wait_policy


class PolicyFactory:
    def __init__(self):
        self.services = []
        self.routes = []
        self.plugins = []

    def __call__(self, case, limits, paths=None, warm=True, warm_status=200):
        name = f"window-{RUN}-{case}"
        service = admin("/services", "POST", {"name": name, "url": "http://echo:8080"})
        self.services.append(service["id"])
        paths = paths or [f"/{name}/a", f"/{name}/b"]
        route_ids = []
        for path in paths:
            route = admin("/routes", "POST", {"paths": [path], "strip_path": False,
                                              "service": {"id": service["id"]}})
            self.routes.append(route["id"])
            route_ids.append(route["id"])
        plugin = admin(f'/services/{service["id"]}/plugins', "POST", {"name": NAME, "config": config(limits)})
        self.plugins.append(plugin["id"])
        if warm:
            wait_policy(paths[0], min(r["limit"] for r in limits) - 1, warm_status)
        else:
            time.sleep(2)
        return paths, plugin, route_ids


    def close(self):
        errors = []
        for kind, ids in (("plugins", self.plugins), ("routes", self.routes), ("services", self.services)):
            for entity_id in reversed(ids):
                try:
                    admin(f"/{kind}/{entity_id}", "DELETE")
                except Exception as exc:
                    errors.append(str(exc))
        if errors:
            raise AssertionError("Kong cleanup failed: " + "; ".join(errors))


@pytest.fixture
def policy():
    factory = PolicyFactory()
    try:
        yield factory
    finally:
        factory.close()
