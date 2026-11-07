"""Serial HTTP scenarios across all three Kong gateways."""
from http.client import HTTPConnection
import time
from urllib.parse import urlsplit
import uuid
import pytest
from common import ADMIN, PROXIES, admin, http, redis
from window_support import (NAME, RUN, CONFIG_TIMEOUT_SECONDS, limit, request,
                            burst, config, wait_policy)

@pytest.mark.parametrize("algorithm", ["fixed_window", "sliding_window"])
def test_shared_budgets(policy, algorithm):
    paths, plugin, route_ids = policy(algorithm, [limit(algorithm, 12)])
    responses = burst(paths[0])
    assert sum((r[0] == 200 for r in responses)) == 12, f'{algorithm}: shared capacity: ' + str([(r[0], r[1].get('X-RateLimit-Mode'), r[1].get('X-RateLimit-Remaining')) for r in responses])
    assert all((r[0] in (200, 429) for r in responses)), 'unexpected admission status'
    assert {r[1]['X-Kong-Instance'] for r in responses} == {'kong1', 'kong2', 'kong3'}, 'all nodes exercised'
    assert request(paths[1], client='alice')[0] == 429, 'routes must share service budget'
    assert request(paths[1], client='bob')[0] == 200, 'clients must be isolated'
    status, headers, _ = request(paths[0])
    assert status == 429 and headers['Retry-After'].isdigit() and (headers['Cache-Control'] == 'no-store'), 'denial headers'
    assert request(paths[0], client=None)[0] == 400, 'ClientId is required'

def test_composite_dimensions(policy):
    paths, plugin, route_ids = policy('composite', [limit(count=1, dimensions=['client_id', 'path'])])
    assert request(paths[0] + '?x=1')[0] == 200, 'composite first'
    assert request(paths[0] + '?x=2', 1)[0] == 429, 'queries share normalized path'
    normalized = paths[0].rsplit('/', 1)[0] + '/./a'
    assert request(normalized, 2)[0] == 429, 'dot segments share normalized path'
    assert request(paths[1], 2)[0] == 200, 'different paths have independent budgets'
    assert request(paths[0], client='bob')[0] == 200, 'different clients isolated'
    node = urlsplit(PROXIES[0])
    conn = HTTPConnection(node.hostname, node.port)
    conn.putrequest('GET', paths[0])
    conn.putheader('X-Client-Id', 'a')
    conn.putheader('X-Client-Id', 'b')
    conn.endheaders()
    response = conn.getresponse()
    assert response.status == 400, 'duplicate ClientId rejected'
    response.read()
    conn.close()
    assert request(paths[0], client='x' * 257)[0] == 400, 'oversized ClientId rejected'

def test_method_route_service(policy):
    paths, _, _ = policy('route-method', [limit(count=1, dimensions=['client_id', 'method', 'route', 'service'])])
    assert request(paths[0])[0] == 200, 'method/route/service first'
    assert request(paths[0] + '/child', 1)[0] == 429, 'same route shares counter without path dimension'
    assert http(PROXIES[1] + paths[0], 'POST', headers={'X-Client-Id': RUN + 'alice'})[0] == 200, 'methods split counters'
    assert request(paths[1], 2)[0] == 200, 'route dimension splits service budget'

def test_simultaneous_limits(policy):
    paths, plugin, route_ids = policy('attempts', [limit(count=1, dimensions=['client_id', 'path']), limit(count=2)])
    assert request(paths[0])[0] == 200, 'initial all-limit admission'
    assert request(paths[0], 1)[0] == 429, 'tight path exhausted'
    assert request(paths[1], 2)[0] == 429, 'denied attempt also consumes the broader budget'
    assert request(paths[1])[0] == 429, 'broad budget exhausted'

def test_route_precedence(policy):
    paths, plugin, route_ids = policy('precedence', [limit(count=1)])
    override = admin(f'/routes/{route_ids[0]}/plugins', 'POST', {'name': NAME, 'config': config([limit(count=3)])})
    policy.plugins.append(override['id'])
    wait_policy(paths[0], 2)
    assert [request(paths[0], i % 3)[0] for i in range(4)] == [200, 200, 200, 429], 'route override must replace service limit'
    assert request(paths[1])[0] == 200 and request(paths[1], 1)[0] == 429, 'service counter separate from route scope'

def test_live_updates(policy):
    paths, plugin, _ = policy('update', [limit(count=2)])
    assert request(paths[0])[0] == 200, 'initial before update'
    admin('/plugins/' + plugin['id'], 'PATCH', {'config': config([limit(count=3)])})
    wait_policy(paths[0], 2)
    assert [request(paths[0], i % 3)[0] for i in range(3)] == [200, 200, 429], 'threshold change preserves counts'
    admin('/plugins/' + plugin['id'], 'PATCH', {'config': config([limit(count=4, window=43200000)])})
    wait_policy(paths[0], 3)
    assert request(paths[0])[0] == 200, 'window shape change creates fresh state'

def test_schema_validation(policy):
    paths, plugin, _ = policy('schema', [limit()])
    for invalid in ([limit(count=0)], [limit(algorithm='unknown')], [limit(dimensions=['ip', 'ip'])], [limit(dimensions=['ip', 'client_id']), limit(dimensions=['client_id', 'ip'])], [limit(window=86400001)]):
        assert http(ADMIN + '/plugins/' + plugin['id'], 'PATCH', {'config': config(invalid)})[0] == 400, 'invalid configuration accepted'
    assert http(ADMIN + '/plugins/' + plugin['id'], 'PATCH', {'consumer': {'id': str(uuid.uuid4())}})[0] == 400, 'consumer scope accepted'

def test_ip_budget(policy):
    paths, _, _ = policy('ip', [limit(count=4, window=1000, dimensions=['ip'])], warm=False)
    deadline = time.monotonic() + CONFIG_TIMEOUT_SECONDS
    stable = 0
    while time.monotonic() < deadline:
        responses = [request(paths[0], i, client=None) for i in range(3)]
        if all(('X-RateLimit-Remaining' in r[1] for r in responses)):
            stable += 1
            if stable >= 15:
                break
        else:
            stable = 0
        time.sleep(0.05)
    assert stable >= 15, 'IP policy did not propagate: ' + str(responses)
    server_time = redis('TIME')
    now_ms = int(server_time[0]) * 1000 + int(server_time[1]) // 1000
    time.sleep((1000 - now_ms % 1000 + 30) / 1000)
    responses = burst(paths[0], count=30)
    assert sum((r[0] == 200 for r in responses)) == 4, 'IP budget shared across gateways: ' + str([(r[0], r[1].get('X-RateLimit-Remaining'), r[2]) for r in responses])
    assert request(paths[1], client=None)[0] == 429, 'IP limit needs no ClientId'

@pytest.mark.parametrize("status", [429, 503])
def test_upstream_headers(policy, status):
    paths, _, _ = policy('upstream' + str(status), [limit(count=10)], paths=[f'/upstream{status}/window-{RUN}'], warm_status=status)
    for suffix, retry in (('', '7'), ('/date', 'Wed, 21 Oct 2037 07:28:00 GMT')):
        response = request(paths[0] + suffix)
        assert response[0] == status and response[1]['Retry-After'] == retry, 'upstream response changed'

@pytest.mark.parametrize("algorithm", ["fixed_window", "sliding_window"])
def test_redis_outage_recovery(policy, algorithm):
    paths, _, _ = policy('outage-' + algorithm, [limit(algorithm, count=4)])
    redis('CLIENT', 'PAUSE', 2200, 'ALL')
    responses = burst(paths[0], count=60, client='outage')
    assert all((r[1].get('X-RateLimit-Mode') == 'local' for r in responses)), 'outage must activate local enforcement'
    for node in ('kong1', 'kong2', 'kong3'):
        count = sum((r[0] == 200 and r[1]['X-Kong-Instance'] == node for r in responses))
        assert count == 4, f'{algorithm}: node {node} admitted {count}, expected 4'
    deadline = time.monotonic() + 16
    recovered = False
    while time.monotonic() < deadline:
        responses = [request(paths[0], i, 'recovery' + uuid.uuid4().hex) for i in range(3)]
        if all((r[1].get('X-RateLimit-Mode') == 'redis' for r in responses)):
            end = time.monotonic() + 4.5
            while time.monotonic() < end:
                responses = [request(paths[0], i, 'ramp' + uuid.uuid4().hex) for i in range(3)]
                time.sleep(0.1)
            responses = [request(paths[0], i, 'closed' + uuid.uuid4().hex) for i in range(3)]
            recovered = all((r[1].get('X-RateLimit-Mode') == 'redis' for r in responses))
            if recovered:
                break
        time.sleep(0.1)
    assert recovered, 'Redis recovery did not complete'

def test_redis_authentication_fallback(policy):
    paths, plugin, _ = policy('auth', [limit()])
    invalid_auth = config([limit()])
    invalid_auth['redis']['password'] = 'incorrect-password'
    admin('/plugins/' + plugin['id'], 'PATCH', {'config': invalid_auth})
    deadline = time.monotonic() + CONFIG_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        responses = [request(paths[0], i, uuid.uuid4().hex) for i in range(3)]
        if all((r[0] == 200 and r[1].get('X-RateLimit-Mode') == 'local' for r in responses)):
            break
        time.sleep(0.1)
    assert all((r[0] == 200 and r[1].get('X-RateLimit-Mode') == 'local' for r in responses)), 'authentication failure must activate local enforcement'
    time.sleep(1.5)
    responses = burst(paths[0], count=60, client='auth-fallback')
    assert all((r[1].get('X-RateLimit-Mode') == 'local' for r in responses)), 'authentication failure bypassed local mode'
    for node in ('kong1', 'kong2', 'kong3'):
        count = sum((r[0] == 200 and r[1]['X-Kong-Instance'] == node for r in responses))
        assert count == 5, f'authentication fallback on {node} admitted {count}, expected 5'
    assert all((r[0] in (200, 429) for r in responses)), 'authentication failure returned unavailable instead of enforcing locally'
