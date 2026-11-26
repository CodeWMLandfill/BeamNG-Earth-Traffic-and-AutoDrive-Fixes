-- Route handoffs through the stock AI API. Keep this state in the vehicle VM: GE's graph acknowledgement can
-- arrive after another rebuild, whereas mapmgr.mapData identifies the graph the AI actually has.
local M = {}
local lastPath, lastMap, lastVersion, lastSpeed, lastAggression, lastMode, lastAwareness, lastSpeedMode
local lastBypass = false

local function resetPath()
  lastPath, lastMap, lastVersion, lastSpeed, lastAggression, lastMode, lastAwareness, lastSpeedMode =
    nil, nil, nil, nil, nil, nil, nil, nil
  if beamngEarth_staticObstacles then beamngEarth_staticObstacles.reset() end
  lastBypass = false
end

function M.reset()
  resetPath()
  if beamngEarth_oncomingPass then beamngEarth_oncomingPass.reset() end
  if beamngEarth_maneuverClearance then beamngEarth_maneuverClearance.onReset() end
end
M.onReset = M.reset
M.onExtensionUnloaded = M.reset

-- A remaining suffix of the old path followed by new nodes. setPath appends ONLY when its first node is the
-- old path's last node. Identical remaining paths need no command; forks, loops and shorter endpoints restart.
function M.extension(old, new)
  if not old or #old == 0 or #new == 0 then return nil end
  local start, seen = nil, {}
  for i, id in ipairs(old) do
    if seen[id] then return nil end
    seen[id] = true
    if id == new[1] then start = i end
  end
  if not start then return nil end
  local oldNodes = seen
  seen = {}
  for _, id in ipairs(new) do
    if seen[id] then return nil end
    seen[id] = true
  end
  local overlap = #old - start + 1
  if #new < overlap then return nil end
  for i = 1, overlap do if new[i] ~= old[start + i - 1] then return nil end end
  for i = overlap + 1, #new do if oldNodes[new[i]] then return nil end end
  local suffix = {}
  for i = overlap, #new do suffix[#suffix + 1] = new[i] end
  return suffix
end

local function valid(ids, md)
  -- One forward waypoint is sufficient: the stock waypoint API builds the approach from the car's position.
  if not md or not md.graph or #ids == 0 then return false end
  for i, id in ipairs(ids) do
    if not md.graph[id] then return false end
    if i > 1 then
      local from = ids[i - 1]
      local e = md.graph[from][id]
      if not e or (e.oneWay and e.inNode ~= nil and e.inNode ~= from) then return false end
    end
  end
  return true
end

local function applyLive(speed, speedMode, aggression, awareness)
  if aggression ~= nil and ai.setAggression then ai.setAggression(aggression) end
  if ai.setSpeed then ai.setSpeed(speed) end
  if speedMode and ai.setSpeedMode then ai.setSpeedMode(speedMode) end
  if awareness ~= nil and ai.setParameters then ai.setParameters({ awarenessForceCoef = awareness }) end
  lastSpeed = speed
  if speedMode then lastSpeedMode = speedMode end
  if aggression ~= nil then lastAggression = aggression end
  if awareness ~= nil then lastAwareness = awareness end
end

function M.stop(mode)
  local wasRace = lastMode == 'race'
  local hadAwareness = lastAwareness
  M.reset()
  if ai.setRacing then ai.setRacing(false) end
  if (wasRace or hadAwareness ~= nil) and ai.setParameters then
    ai.setParameters({ awarenessForceCoef = 0.25 })
  end
  ai.setMode(mode or 'stop')
end

-- A streamed-route hold can resume the same manoeuvre. User cancellation,
-- physical recovery and arrival still discard it through reset/stop/arrive.
function M.hold()
  resetPath()
  if beamngEarth_oncomingPass then beamngEarth_oncomingPass.suspend() end
  if beamngEarth_maneuverClearance then beamngEarth_maneuverClearance.suspend() end
  ai.setMode('stop')
end

-- GE plans from the vehicle reference point; the stock AI drives from obj:getFrontPosition(). A queued restart
-- can therefore target a node already behind the AI. Drop only the passed prefix on its outgoing road.
M.SURFACE_DZ_M = 3.5
function M.forwardStart(ids, md, p, heading)
  if not md.positions then return ids end
  local first = 1
  local maxDz = M.SURFACE_DZ_M or 3.5
  while first < #ids do
    local a, b = md.positions[ids[first]], md.positions[ids[first + 1]]
    if not a or not b then break end
    local dx, dy = b.x - a.x, b.y - a.y
    local lengthSq = dx * dx + dy * dy
    if lengthSq < 1e-8 then break end
    if dx * heading.x + dy * heading.y <= 0.5 * math.sqrt(lengthSq) then break end
    local t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / lengthSq
    if t < 0 then break end
    local edge = md.graph[ids[first]][ids[first + 1]]
    local ra, rb = edge.inRadius, edge.outRadius
    if edge.inNode == ids[first + 1] then ra, rb = rb, ra end
    ra, rb = ra or (md.radius and md.radius[ids[first]]) or 8, rb or (md.radius and md.radius[ids[first + 1]]) or 8
    local reach = ra + math.min(1, t) * (rb - ra)
    local ex, ey = p.x - a.x - t * dx, p.y - a.y - t * dy
    if ex * ex + ey * ey > reach * reach or math.abs(p.z - a.z - t * (b.z - a.z)) > maxDz then break end
    first = first + 1
  end
  if first == 1 then return ids end
  local out = {}
  for i = first, #ids do out[#out + 1] = ids[i] end
  return out
end

-- Use the stock speed planner to brake before the real destination. A user speed setting is a ceiling
-- here: 'set' otherwise overrides the planner's stopping profile, even at the end of the road.
function M.approach(distance)
  if ai.mode ~= 'manual' then return end
  if (lastSpeed or lastSpeedMode == 'off') and ai.setSpeedMode then ai.setSpeedMode('limit') end
  if ai.setStopPoint then ai.setStopPoint(nil, math.max(0, distance - 5)) end
end

function M.arrive()
  M.reset()
  -- Discard the finished target so stop mode cannot turn back toward it after an overshoot. Entering
  -- manual also enables the stock update callback if an earlier local-path stop already disabled it.
  ai.setMode('manual')
  ai.setMode('stop') -- service brakes first; the stock controller applies the parking brake at rest
end

-- Push speed / aggression / avoidance onto the active manual plan. No path rebuild.
function M.tune(speed, speedMode, aggression, awareness)
  if ai.mode ~= 'manual' or not lastPath or not (ai.setSpeed and ai.setSpeedMode and ai.setAggression) then
    return 'idle'
  end
  applyLive(speed, speedMode, aggression, awareness)
  return 'tuned'
end

function M.drive(ids, version, force, speed, aggression, mode, awareness, speedMode)
  local md = mapmgr.mapData
  if not valid(ids, md) then
    M.hold()
    mapmgr.requestMap()
    return 'waiting'
  end
  if obj and obj.getFrontPosition and obj.getPosition and obj.getDirectionVector then
    local front, ref = obj:getFrontPosition(), obj:getPosition()
    ids = M.forwardStart(ids, md, { x = front.x, y = front.y, z = ref.z }, obj:getDirectionVector())
  end
  local bypass = beamngEarth_oncomingPass and beamngEarth_oncomingPass.offLane() or false
  local racing = mode == 'race'
  if racing and aggression == nil then aggression = 0.72 end
  -- BeamNG ai.lua: 'limit' caps routeSpeed and still uses curve physics (vx_possible_next).
  -- 'set' forces routeSpeed on every node. A bare speed number keeps the old 'set' contract.
  if speedMode == nil then
    speedMode = (speed and 'set') or (racing and 'off' or 'legal')
  end
  local appliedAwareness = awareness
  if appliedAwareness == nil and (racing or lastMode == 'race') then
    appliedAwareness = racing and 0.55 or 0.25
  end
  local sameGraph = not force and bypass == lastBypass and ai.mode == 'manual' and md == lastMap and version == lastVersion
  local suffix = sameGraph and M.extension(lastPath, ids) or nil
  local paramsSame = speed == lastSpeed and aggression == lastAggression and mode == lastMode
    and speedMode == lastSpeedMode and appliedAwareness == lastAwareness
  -- In-place only when the vehicle AI exposes the setters. Older callers (and the handoff test's mock)
  -- still restart so a speed change is applied through driveUsingPath.
  local canLive = suffix and ai.setSpeed and ai.setSpeedMode and ai.setAggression
  local action
  if suffix and (paramsSame or canLive) then
    if not paramsSame then
      applyLive(speed, speedMode, aggression, appliedAwareness)
      action = 'tune'
    end
    if #suffix > 1 then
      ai.setPath(suffix)
      action = action or 'extend'
    else
      action = action or 'keep'
    end
  else
    -- In BeamNG 0.40, explicit path input leaves ego.currentSegment empty. The first plan then aims at the
    -- road centre instead of the occupied lane. Waypoint input runs getPointNodePath and seeds that segment;
    -- every adjacent node remains a waypoint so the local route still follows the GPS's chosen turns.
    -- setRacing stays false so the 3D capsule avoidance loop keeps running.
    if ai.setRacing then ai.setRacing(false) end
    local args = { wpTargetList = ids, driveInLane = 'on', avoidCars = 'on', aggression = aggression,
      routeSpeedMode = speedMode, routeSpeed = speed }
    if bypass then
      args.path = ids; args.wpTargetList = nil; args.driveInLane = 'off'
      args.routeSpeed = math.min(3, speed or 3); args.routeSpeedMode = 'limit'
    end
    ai.driveUsingPath(args)
    if appliedAwareness ~= nil and ai.setParameters then
      ai.setParameters({ awarenessForceCoef = appliedAwareness })
    end
    action = 'start'
  end
  -- The engine may extend its own path table in place; retain an independent copy of the requested suffix.
  lastPath = {}
  for i, id in ipairs(ids) do lastPath[i] = id end
  lastMap, lastVersion, lastSpeed, lastAggression, lastMode = md, version, speed, aggression, mode
  lastAwareness, lastSpeedMode = appliedAwareness, speedMode
  if not beamngEarth_staticObstacles and extensions and extensions.load then
    extensions.load('beamngEarth_staticObstacles')
  end
  if beamngEarth_staticObstacles then beamngEarth_staticObstacles.bindPath(lastPath, md, action == 'start') end
  if beamngEarth_oncomingPass then beamngEarth_oncomingPass.bindPath(md, action == 'start') end
  lastBypass = bypass
  return action
end

return M
