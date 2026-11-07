local cjson = require "cjson.safe"
local sha256 = require "resty.sha256"
local algorithms = require "kong.plugins.rate-limiting-sliding-window.algorithms"
local _M = {}

function _M.digest(value)
  local hash = sha256:new()
  assert(hash:update(value))
  return assert(hash:final())
end

function _M.hex(value)
  return (value:gsub(".", function(c) return string.format("%02x", c:byte()) end))
end

function _M.uuid(value)
  if value == nil or value == ngx.null then return string.rep("\0", 16) end
  local hex = value:gsub("-", "")
  assert(#hex == 32 and not hex:find("[^%x]"), "invalid scope UUID")
  return (hex:gsub("..", function(pair) return string.char(tonumber(pair, 16)) end))
end

function _M.dimensions(dimensions)
  local names = {}
  for i, name in ipairs(dimensions) do names[i] = name end
  table.sort(names)
  return names
end

function _M.signature(rule)
  return _M.digest(assert(cjson.encode({rule.algorithm, rule.window_ms, _M.dimensions(rule.dimensions)})))
end

function _M.scope(conf)
  return _M.uuid(conf.service_id) .. _M.uuid(conf.route_id)
end

function _M.key(conf, rule, values)
  local tuple = {}
  for _, name in ipairs(_M.dimensions(rule.dimensions)) do
    if not values[name] then return nil, "missing dimension: " .. name end
    tuple[#tuple+1] = {name, values[name]}
  end
  return "rlsw:v2:" .. _M.scope(conf) .. _M.signature(rule)
    .. _M.digest(assert(cjson.encode(tuple)))
end

-- Local shared dictionaries have scalar values, so emulate hash fields internally.
function _M.window_key(base, start)
  assert(type(start) == "number" and start >= 0 and start == math.floor(start)
         and start <= 9007199250000000, "invalid window start")
  local bytes = {}
  for i=8,1,-1 do
    bytes[i] = string.char(start % 256)
    start = math.floor(start/256)
  end
  return base .. table.concat(bytes)
end

function _M.counters(keys, rules, now)
  assert(#keys > 0 and #keys <= 16 and #keys == #rules, "invalid limit count")
  local counters, seen = {}, {}
  for i, rule in ipairs(rules) do
    assert(type(keys[i]) == "string" and not seen[keys[i]], "duplicate or invalid counter key")
    seen[keys[i]] = true
    local start, _, ttl = algorithms.window(rule, now)
    counters[i] = {key=keys[i], current_field=string.format("%.0f",start), previous_field="",
                   local_key=_M.window_key(keys[i],start), ttl_ms=ttl, rule=rule}
    if rule.algorithm == "sliding_window" and start >= rule.window_ms then
      counters[i].previous_field = string.format("%.0f",start-rule.window_ms)
      counters[i].previous_local_key = _M.window_key(keys[i],start-rule.window_ms)
    end
  end
  return counters
end

function _M.buckets(conf)
  local values = {
    ip = kong.client.get_forwarded_ip(), path = kong.request.get_path(),
    method = kong.request.get_method(), route = (kong.router.get_route() or {}).id,
    service = (kong.router.get_service() or {}).id,
  }
  local keys = {}
  for i, rule in ipairs(conf.limits) do
    for _, name in ipairs(rule.dimensions) do
      if name == "client_id" and not values.client_id then
        local headers, err = kong.request.get_headers(1000)
        if err then return nil, "request headers were truncated" end
        local value = headers[conf.client_id_header:lower()]
        if type(value) ~= "string" or value == "" or #value > 256 then
          return nil, "a single nonempty ClientId header of at most 256 bytes is required"
        end
        values.client_id = value
      end
    end
    local err
    keys[i], err = _M.key(conf, rule, values)
    if not keys[i] then return nil, err end
  end
  return keys
end

return _M
