"""Run only after window_restart.py prepare and a kong1 restart."""
import json
import pytest
from common import PROXIES, admin, http, redis


@pytest.fixture
def restart_budget():
    key = "rlsw:demo-restart-check"
    raw = redis("GET", key)
    assert raw, "restart fixture missing; use ./run.sh test for prepare/restart/verify"
    fixture = json.loads(raw)
    try:
        yield fixture
    finally:
        errors = []
        for kind in ("plugin", "route", "service"):
            try:
                admin(f"/{kind}s/{fixture[kind]}", "DELETE")
            except Exception as exc:
                errors.append(str(exc))
        redis("DEL", key)
        assert not errors, "; ".join(errors)


@pytest.mark.restart
def test_redis_budget_survives_gateway_restart(restart_budget):
    fixture = restart_budget
    headers = {"X-Client-Id": fixture["client"]}
    assert http(PROXIES[0] + fixture["path"], headers=headers)[0] == 200
    assert all(http(url + fixture["path"], headers=headers)[0] == 429 for url in PROXIES)
