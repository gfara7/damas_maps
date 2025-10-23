-- Custom car profile overlay that delegates to the upstream /opt/car.lua
-- and scales speed for selected OSM way IDs.

local SPEED_FACTOR = 0.2 -- 20% of the default speed

-- Streets to slow down (OSM Way IDs)
local SLOW_WAYS = {
  [307615587]   = true, -- Murshed Khater Street
  [1433788968]  = true, -- Murshed Khater Street (segment)
  [28716906]    = true, -- Baghdad Street
  [1087028779]  = true, -- Baghdad Street (segment)
  [151748237]   = true, -- Al Thawra Street
  [151748244]   = true, -- Al Thawra Street (segment)
  [46787138]    = true, -- Al Thawra Street (segment)
}

-- Load the upstream default car profile bundled in the OSRM image
local base = dofile('/opt/car.lua')

local function process_way(profile, way, result, relations)
  -- Run the default logic first
  base.process_way(profile, way, result, relations)

  -- Then scale speeds for selected ways
  local wid = way:id()
  if SLOW_WAYS[wid] then
    if result.forward_mode ~= 0 and result.forward_speed and result.forward_speed > 0 then
      result.forward_speed = result.forward_speed * SPEED_FACTOR
    end
    if result.backward_mode ~= 0 and result.backward_speed and result.backward_speed > 0 then
      result.backward_speed = result.backward_speed * SPEED_FACTOR
    end
  end
end

return {
  setup = base.setup,
  process_node = base.process_node,
  process_turn = base.process_turn,
  process_way = process_way,
}

