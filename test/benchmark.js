import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';

const target = __ENV.BENCH_URL || 'http://gateway:8000/window/a';
const clientMode = __ENV.BENCH_CLIENT_MODE || 'shared';
const keepAlive = (__ENV.BENCH_KEEPALIVE || 'false') === 'true';
if (!['shared', 'per-vu', 'unique'].includes(clientMode)) {
  throw new Error('BENCH_CLIENT_MODE must be shared, per-vu, or unique');
}
if (!['true', 'false'].includes(__ENV.BENCH_KEEPALIVE || 'false')) {
  throw new Error('BENCH_KEEPALIVE must be true or false');
}

const admitted = new Counter('admitted_requests');
const throttled = new Counter('throttled_requests');
const unexpected = new Counter('unexpected_responses');
const redis = new Counter('redis_requests');
const local = new Counter('local_requests');
const nodes = {
  kong1: new Counter('kong1_requests'),
  kong2: new Counter('kong2_requests'),
  kong3: new Counter('kong3_requests'),
};

// 429 is an expected limiter decision, reported separately from HTTP errors.
http.setResponseCallback(http.expectedStatuses(200, 429));

export const options = {
  vus: Number(__ENV.BENCH_VUS || 20),
  duration: __ENV.BENCH_DURATION || '30s',
  discardResponseBodies: true,
  dns: { ttl: 'inf', select: 'roundRobin', policy: 'onlyIPv4' },
  noConnectionReuse: !keepAlive,
  maxRedirects: 0,
  summaryTrendStats: ['avg', 'med', 'p(95)', 'p(99)', 'max'],
  thresholds: {
    http_req_failed: ['rate==0'],
    checks: ['rate==1'],
    kong1_requests: ['count>0'],
    kong2_requests: ['count>0'],
    kong3_requests: ['count>0'],
    local_requests: ['count==0'],
  },
};

export function setup() {
  return { client: __ENV.BENCH_CLIENT_ID || `benchmark-${Date.now()}` };
}

function header(response, name) {
  // Go/k6 canonicalizes header names differently from Kong's original casing.
  const wanted = name.toLowerCase();
  for (const key of Object.keys(response.headers || {})) {
    if (key.toLowerCase() === wanted) return response.headers[key];
  }
  return undefined;
}

export default function (data) {
  let client = data.client;
  if (clientMode === 'per-vu') client += `-vu-${__VU}`;
  if (clientMode === 'unique') client += `-vu-${__VU}-request-${__ITER}`;
  const response = http.get(target, {
    headers: { 'X-Client-Id': client },
    timeout: '5s',
    tags: { name: 'rate-limit-benchmark' },
  });
  const instance = header(response, 'X-Kong-Instance');
  const mode = header(response, 'X-RateLimit-Mode');
  if (nodes[instance]) nodes[instance].add(1);
  admitted.add(response.status === 200 ? 1 : 0);
  throttled.add(response.status === 429 ? 1 : 0);
  unexpected.add(response.status !== 200 && response.status !== 429 ? 1 : 0);
  redis.add(mode === 'redis' ? 1 : 0);
  local.add(mode === 'local' ? 1 : 0);
  check(response, {
    'status is 200 or 429': (r) => r.status === 200 || r.status === 429,
    'gateway instance identified': () => Boolean(nodes[instance]),
    'Redis enforcement active': () => mode === 'redis',
  });
}

export function handleSummary(data) {
  const values = (name) => data.metrics[name] ? data.metrics[name].values : {};
  const count = (name) => values(name).count || 0;
  const requests = count('http_reqs');
  const distribution = {};
  for (const name of Object.keys(nodes)) {
    const total = count(`${name}_requests`);
    distribution[name] = {
      requests: total,
      percent: requests ? Math.round(total / requests * 10000) / 100 : 0,
    };
  }
  const report = {
    target,
    client_mode: clientMode,
    keep_alive: keepAlive,
    dns: options.dns,
    duration_seconds: data.state.testRunDurationMs / 1000,
    virtual_users: values('vus_max').max || 0,
    requests,
    requests_per_second: values('http_reqs').rate || 0,
    admitted_200: count('admitted_requests'),
    throttled_429: count('throttled_requests'),
    unexpected: count('unexpected_responses'),
    redis_requests: count('redis_requests'),
    local_requests: count('local_requests'),
    gateways: distribution,
    latency_ms: values('http_req_duration'),
    connect_ms: values('http_req_connecting'),
    iteration_ms: values('iteration_duration'),
    failed_checks: values('checks').fails || 0,
    thresholds_passed: Object.values(data.metrics).every((metric) =>
      Object.values(metric.thresholds || {}).every((threshold) => threshold.ok)),
    note: 'Includes all responses and all applicable plugins. Small quotas throttle repeated client identities. ' +
      (keepAlive ? 'Connections are reused; DNS selection happens on new connections. ' : 'A TCP connection is opened for each request. ') +
      'latency_ms excludes connection setup; iteration_ms includes it.',
  };
  return { stdout: JSON.stringify(report, null, 2) + '\n' };
}
