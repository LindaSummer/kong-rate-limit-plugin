local typedefs = require "kong.db.schema.typedefs"
local redis_schema = require "kong.tools.redis.schema"
local identity = require "kong.plugins.rate-limiting-sliding-window.identity"

local function validate(conf)
  local signatures = {}
  for _, rule in ipairs(conf.limits or {}) do
    local names = {}
    for _, name in ipairs(rule.dimensions) do
      if names[name] then return nil, "dimensions must be unique" end
      names[name] = true
    end
    local signature = identity.signature(rule)
    if signatures[signature] then return nil, "duplicate algorithm/window/dimensions" end
    signatures[signature] = true
  end
  return true
end

return {
  name = "rate-limiting-sliding-window",
  fields = {
    {consumer = typedefs.no_consumer},
    {protocols = typedefs.protocols_http},
    {config = {type = "record", custom_validator = validate, fields = {
      {client_id_header = typedefs.header_name {default = "X-Client-Id"}},
      {redis = redis_schema.config_schema},
      {limits = {type = "array", required = true, len_min = 1, len_max = 16,
        elements = {type = "record", fields = {
          {algorithm = {type = "string", required = true, one_of = {"fixed_window", "sliding_window"}}},
          {window_ms = {type = "integer", required = true, between = {1, 86400000}}},
          {limit = {type = "integer", required = true, between = {1, 1000000}}},
          {dimensions = {type = "array", required = true, len_min = 1, len_max = 6,
            elements = {type = "string", one_of = {"ip", "client_id", "path", "method", "route", "service"}}}},
        }}}},
    }}},
  },
  entity_checks = {
    {conditional = {if_field = "config.limits", if_match = {required = true},
      then_field = "config.redis.host", then_match = {required = true}}},
  },
}
