local resty_lock = require "resty.lock"
local algorithms = require "kong.plugins.rate-limiting-sliding-window.algorithms"
local identity = require "kong.plugins.rate-limiting-sliding-window.identity"
local _M = {}

function _M.admit(scope, keys, rules, dict, clock, request_time)
  dict = dict or ngx.shared.sliding_ratelimit
  if not dict then return nil, "sliding_ratelimit dictionary missing" end
  local now = request_time or (clock and clock() or math.floor(ngx.now()*1000))
  local lock, err = resty_lock:new("kong_locks", {timeout=0.2, exptime=5})
  if not lock then return nil, err end
  local elapsed
  elapsed, err = lock:lock("rlsw:admit:" .. identity.hex(scope))
  if not elapsed then return nil, err end
  local ok, result = pcall(function()
    local counters = identity.counters(keys, rules, now)
    local retry, remaining = 0, 1000000
    for _, counter in ipairs(counters) do
      local current = algorithms.counter(dict:get(counter.local_key))+1
      local previous = counter.previous_local_key and algorithms.counter(dict:get(counter.previous_local_key)) or 0
      -- Count each attempt, including denials, just like the Redis path.
      local stored, store_err = dict:safe_set(counter.local_key, current, counter.ttl_ms/1000)
      assert(stored, store_err)
      local value = algorithms.calculate(current, previous, counter.rule, now)
      retry = math.max(retry, value.retry_ms)
      remaining = math.min(remaining, value.remaining)
    end
    return {retry == 0 and "allow" or "deny", retry, now, retry == 0 and remaining or 0}
  end)
  lock:unlock()
  if not ok then return nil, tostring(result) end
  return result
end

return _M
