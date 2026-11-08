-- All window calculations run in Kong; Redis only stores attempt counters.
local _M = {}
local MAX_INTEGER = 9007199250000000
local function integer(n, lo, hi)
  return type(n) == "number" and n == math.floor(n) and n >= lo and n <= hi
end

function _M.counter(value)
  if value == nil or value == ngx.null then return 0 end
  local count = tonumber(value)
  assert(integer(count, 0, MAX_INTEGER), "invalid counter value")
  return count
end

function _M.window(rule, now)
  local w, limit = rule.window_ms, rule.limit
  assert(integer(w, 1, 86400000) and integer(limit, 1, 1000000), "invalid limit")
  assert(rule.algorithm == "fixed_window" or rule.algorithm == "sliding_window", "invalid algorithm")
  assert(integer(now, 0, MAX_INTEGER), "invalid request timestamp")
  local start = math.floor(now/w)*w
  local elapsed = now-start
  return start, elapsed, (rule.algorithm == "sliding_window" and 2*w or w)-elapsed
end

-- current already includes this attempt, even when it will be denied.
function _M.calculate(current, previous, rule, now)
  current, previous = _M.counter(current), _M.counter(previous)
  local _, elapsed = _M.window(rule, now)
  local w, limit = rule.window_ms, rule.limit
  if rule.algorithm == "fixed_window" then previous = 0 end
  -- Weighted usage = current + previous * (1 - elapsed / w).
  -- Multiply the remaining budget by w to avoid division in the admission check:
  -- available = (limit - weighted usage) * w; >= 0 means this attempt fits.
  -- Near an admission boundary these integer products are within 2^53.
  -- Very large attempt counts unambiguously deny and never become negative.
  local available = (limit-current)*w-previous*(w-elapsed)
  local allowed, retry = available >= 0, 0
  if not allowed then
    if rule.algorithm == "fixed_window" then
      retry = w-elapsed
    elseif current < limit then
      retry = w-math.floor((limit-1-current)*w/previous)-elapsed
    else
      retry = w-elapsed+w-math.floor((limit-1)*w/current)
    end
  end
  return {allowed=allowed, retry_ms=retry,
          remaining=math.max(0, math.floor(available/w))}
end

return _M
