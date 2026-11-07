local identity = require "kong.plugins.rate-limiting-sliding-window.identity"
local redis = require "kong.plugins.rate-limiting-sliding-window.policies.redis"
local local_policy = require "kong.plugins.rate-limiting-sliding-window.policies.local"
local breaker = require "kong.plugins.rate-limiting-sliding-window.circuit_breaker"

local Handler = {VERSION = "0.2.0", PRIORITY = 910}

local function unavailable(err)
  kong.log.err("window rate limiter: ", tostring(err))
  return kong.response.exit(503, {message = "Rate limiter unavailable"}, {["Cache-Control"] = "no-store"})
end

function Handler:access(conf)
  local now = math.floor(ngx.now()*1000)
  local keys, err = identity.buckets(conf)
  if not keys then return kong.response.exit(400, {message = err}) end
  local scope = identity.scope(conf)
  -- Backend changes must not inherit a breaker opened for a different Redis.
  local circuit_key = identity.hex(scope) .. ":" .. redis.backend(conf.redis)
  local use_redis, ticket = breaker.select(circuit_key, conf.redis.timeout)
  if use_redis == nil then
    kong.log.warn("window rate limiter circuit unavailable; using local limiter: ", tostring(ticket))
    use_redis = false
  end
  local result, mode
  if use_redis then
    result, err = redis.admit(conf.redis, keys, conf.limits, now)
    if result then
      local ok, report_err = breaker.report(circuit_key, ticket, true)
      if not ok then kong.log.warn("window rate limiter circuit update failed: ", tostring(report_err)) end
      mode = "redis"
    else
      kong.log.warn("window rate limiter using local fallback: ", tostring(err))
      local ok, report_err = breaker.report(circuit_key, ticket, false)
      if not ok then kong.log.warn("window rate limiter circuit update failed: ", tostring(report_err)) end
    end
  end
  if not result then
    result, err = local_policy.admit(scope, keys, conf.limits, nil, nil, now)
    if not result then return unavailable(err) end
    mode = "local"
  end
  kong.response.set_header("X-Kong-Instance", ngx.var.hostname)
  kong.response.set_header("X-RateLimit-Mode", mode)
  kong.response.set_header("X-RateLimit-Time-Ms", result[3])
  kong.response.set_header("X-RateLimit-Remaining", result[4])
  if result[1] == "deny" then
    return kong.response.exit(429, {message = "API rate limit exceeded"}, {
      ["Retry-After"] = tostring(math.max(1, math.ceil(tonumber(result[2])/1000))),
      ["Cache-Control"] = "no-store",
    })
  end
end

return Handler
