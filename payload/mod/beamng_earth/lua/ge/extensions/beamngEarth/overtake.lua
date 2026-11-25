-- Overtaking on multi-lane highways (Tann2019, 2026-11-05, I-94 in Montana: "the slow thing is a truck. can traffic pass
-- these guys?"). The stock AI never overtakes: it changes lanes only for a turn or an exit, so traffic (and the map's
-- autodrive, the same AI) queued for miles behind a truck in the right lane while the left lane was empty.
--
-- Every SCAN_S, for each traffic car and the player's car while it autodrives (gps.lua): on a one-way carriageway of 2+
-- lanes, held HELD_S behind a slower vehicle in its lane (within LOOK_M, slower than HELD_SHARE of the limit), with the
-- lane to its left clear (CLEAR_BACK_M behind to CLEAR_AHEAD_M ahead) and no junction within JUNCTION_M: the car's AI
-- is told to move one lane left (vehicle ai.laneChange: its plan's nodes shifted sideways, kept within the lanes going its
-- way); its plan's later nodes come in its own lane again, so it drifts back right by itself (keep right except to
-- pass). One shift per pass: a second one is added to the first (laneChange moves the plan's nodes from where they are,
-- clamped only at the lanes' outer edge) and in the live test (I-90 at Billings, 2026-11-05) the repeated shifts took a
-- car past its lane; one that drifts back too soon is held again and passes after COOLDOWN_S. Right-hand traffic: left
-- to pass; left-hand: right.
-- Not the player's car in normal Autodrive (PLAYER = false): a lane shift on its given path ran into the median
-- (I-90 at Billings, 2026-11-05). Race mode sets playerRace and Pass slow cars sets playerPass, both clamped to the
-- carriageway. A stopped vehicle (under WRECK_SPEED_MS), including one spun sideways, is an obstacle.
local M = {}

M.enabled = true
M.PLAYER = false
M.playerRace = false
M.SCAN_S = 0.5
M.LOOK_M = 50
M.RACE_LOOK_M = 110
M.HELD_S = 2.0
M.RACE_HELD_S = 0.20    -- Race Autodrive moves over to an open lane well before braking for slower traffic
M.RACE_COOLDOWN_S = 1.8
M.playerPassCoolS = 5.0  -- Pass slow cars waits this long between lane changes (not the race cadence)
M.WRECK_HELD_S = 0.6    -- steer around a stopped / wrecked obstacle quickly before coming to a dead stop
M.WRECK_SPEED_MS = 3.0  -- a vehicle ahead below this speed (even spun sideways) is a stopped obstacle / wreck
M.HELD_SHARE = 0.85
M.MIN_MS = 8            -- the passer at least this fast (no passing in a queue at a standstill)
M.LANE_TOL_M = 1.6      -- the vehicle ahead counts as in the lane within this sideways
M.CLEAR_BACK_M = 25
M.CLEAR_AHEAD_M = 70
M.JUNCTION_M = 120
M.SHIFT_S = 2.5         -- the move over takes this long at the car's speed (ai.laneChange's distance), at least SHIFT_MIN_M
M.SHIFT_MIN_M = 30
M.PAST_M = 15
M.COOLDOWN_S = 8
M.GIVE_UP_S = 25
M.RETURN_COOLDOWN_S = 6.0  -- no lane change back until this long after the last shift
M.RETURN_HELD_S = 2.5      -- sit in the empty passing lane this long, then move back out
M.RETURN_BACK_M = 22
M.RETURN_AHEAD_M = 65
M.RETURN_SLOW_AHEAD_M = 75 -- a slower vehicle this far ahead in the travel lane keeps us over

local held = {}         -- car id -> os.clock() it started being held
local passing = {}      -- car id -> { lead, at }: passing it
local lastPass = {}     -- car id -> os.clock() of its last pass
local lastShift = {}    -- car id -> os.clock() of its last lane-change command
local returnSince = {}  -- car id -> os.clock() the travel lane first looked clear
local lastScan = -math.huge
local stats = { passes = 0 }

-- Pure: the lanes going the car's way on an edge (lanes string: '+' along in -> out, '-' against; research sec. 2.2),
-- whether it drives along in -> out.
function M.lanesMyWay(lanes, along)
  if type(lanes) ~= 'string' then return 0 end
  local n = 0
  for ch in lanes:gmatch(along and '%+' or '%-') do n = n + 1 end
  return n
end

-- Pure: the car's lane index (0 = leftmost going its way) and the lane width on a one-way carriageway of half width
-- radius with n lanes, the car at lat m right of its centre line (looking the car's way).
function M.laneOf(lat, radius, n)
  if not lat or not radius or radius <= 0 or not n or n < 1 then return nil end
  local w = 2 * radius / n
  local k = math.floor((lat + radius) / w)
  return math.max(0, math.min(n - 1, k)), w
end

-- Pure: the sideways shift (m, + right) that takes a car in lane k of n one lane over to pass (left in right-hand
-- traffic, right in left-hand traffic), or nil when there is no lane that side.
function M.passShift(k, n, w, leftHand)
  if not k or not n or n < 2 then return nil end
  if leftHand then return k < n - 1 and w or nil end
  return k > 0 and -w or nil
end

-- Pure: sideways shift (m, + right) around a stopped obstacle / wreck in lane k of n: tries the normal passing side
-- first, then the opposite adjacent lane (e.g. right lane when a wreck blocks the left lane), if laneClear allows it.
function M.avoidShift(k, n, w, leftHand, others, back, ahead, v)
  if not k or not n or n < 2 then return nil end
  local first = M.passShift(k, n, w, leftHand)
  if first and M.laneClear(others or {}, first, back, ahead, v) then return first end
  local alt = leftHand and (k > 0 and -w or nil) or (k < n - 1 and w or nil)
  if alt and M.laneClear(others or {}, alt, back, ahead, v) then return alt end
  return nil
end

-- Pure: Race Autodrive lane choice. Same order as avoidShift: legal passing side, then the other open lane.
function M.raceShift(k, n, w, leftHand, others, back, ahead, v)
  return M.avoidShift(k, n, w, leftHand, others, back, ahead, v)
end

-- Pure: meters of room from lane k toward the left edge and the right edge of an n-lane carriageway.
-- A one-lane pass is width w. On two lanes the old (n-1)*w/2 cap was only half a lane and cancelled the pass.
function M.passRoom(k, n, w)
  if not k or not n or not w or n < 1 or w <= 0 then return 0, 0 end
  return (k + 0.5) * w, (n - k - 0.5) * w
end

function M.setPlayerRace(on)
  M.playerRace = on and true or false
  return M.playerRace
end

-- 1.4 s when the player is already brisk, otherwise 2.0 s so a legal pass does not chatter across lanes.
function M.playerHoldS(speedMph, aggression)
  if (tonumber(speedMph) or 0) >= 75 or (tonumber(aggression) or 0) >= 0.50 then return 1.4 end
  return 2.0
end

function M.holdSeconds(wreck, race, playerPass, speedMph, aggression)
  if wreck then return M.WRECK_HELD_S end
  if race then return M.RACE_HELD_S end
  if playerPass then return M.playerHoldS(speedMph, aggression) end
  return M.HELD_S
end

-- Autodrive "Pass slow cars". Off by default until gps.lua turns it on with the player.
function M.setPlayerPass(on, speedMph, aggression)
  M.playerPass = on and true or false
  M.playerSpeedMph = tonumber(speedMph) or 0
  M.playerAggression = tonumber(aggression) or 0
  M.playerHeldS = M.playerHoldS(M.playerSpeedMph, M.playerAggression)
  return M.playerPass
end

-- The player's car is shifted when PLAYER is on, Race asked for it, or Pass slow cars is on.
function M.passesPlayer(race)
  return M.PLAYER or race or M.playerRace or M.playerPass or false
end

-- Pure: is a car (speed v m/s, road limit m/s) held by a vehicle ahead in its lane at gap m doing leadV m/s?
-- A vehicle slower than WRECK_SPEED_MS counts as a stopped obstacle even when the passer has crawled under MIN_MS.
function M.isHeld(v, limit, gap, leadV, race)
  if not v or not limit or not gap or not leadV then return false end
  local look = race and M.RACE_LOOK_M or M.LOOK_M
  if leadV < M.WRECK_SPEED_MS and gap <= look and v >= 0.5 then return true end
  if race then
    local close = math.max(0, v - leadV)
    return gap <= math.max(M.RACE_LOOK_M, close * 4.5) and v >= M.MIN_MS * 0.5 and (leadV < v + 2.5 or leadV < limit * 1.15)
  end
  return gap <= M.LOOK_M and v >= M.MIN_MS * 0.5 and leadV < limit * M.HELD_SHARE and leadV < v + 1.5
end

-- Pure: others [{ahead = m along the car, side = m right of it, v = m/s}]: is the band [lat - tol, lat + tol] sideways
-- (relative to the car) clear from back m behind to ahead m ahead? A car closing fast from behind needs more room.
function M.laneClear(others, shift, back, ahead, v)
  for _, o in ipairs(others) do
    if math.abs((o.side or 0) - shift) < M.LANE_TOL_M then
      local ov = o.v or 0
      local room = back + math.max(0, ov - (v or 0)) * 3
      local front = ahead
      -- A crawling car (under MIN_MS, stuck behind a wreck) may tuck in behind traffic that is
      -- already pulling away. Highway speed keeps the full ahead window (a 19 m/s pass still
      -- needs the car 40 m up the next lane to be gone).
      if (v or 0) < M.MIN_MS and (o.ahead or 0) > 0 and ov > (v or 0) + 4 then
        front = math.min(ahead, 18)
      end
      if o.ahead > -room and o.ahead < front then return false end
    end
  end
  return true
end

-- Pure: shift (m, + right) that moves an AI car from an inner lane back toward the outer travel lane,
-- or nil. Right-hand traffic: the travel lane is the rightmost, so the shift is +w. Left-hand traffic:
-- the travel lane is the leftmost, so the shift is -w. The caller applies RETURN_HELD_S, the lane-change
-- cooldown, the junction check, and the fast-road speed limit before asking.
function M.returnShift(k, n, w, leftHand, others, back, ahead, v)
  if not k or not n or n < 2 or not w or w == 0 then return nil end
  if not v or v < M.MIN_MS then return nil end
  local shift
  if leftHand then
    if k <= 0 then return nil end
    shift = -w
  else
    if k >= n - 1 then return nil end
    shift = w
  end
  back = tonumber(back) or M.RETURN_BACK_M
  ahead = tonumber(ahead) or M.RETURN_AHEAD_M
  if not M.laneClear(others or {}, shift, back, ahead, v) then return nil end
  for _, o in ipairs(others or {}) do
    if math.abs((o.side or 0) - shift) < M.LANE_TOL_M then
      local leadV = o.v or 0
      if o.ahead and o.ahead > 0 and o.ahead < M.RETURN_SLOW_AHEAD_M and leadV < v - 0.5 then
        return nil
      end
    end
  end
  return shift
end

-- Pure: vehicle Lua command that calls ai.laneChange and locks laneLimLeft/laneLimRight on nodes past the transition
-- ramp to the target lane so ai.lua's uniformPlanErrorDistribution (tau=0.5s) cannot pull the shifted plan back
-- behind the lead car, and buildNextRoute keeps appending new plan nodes in the committed passing lane.
local function finite(x) return type(x) == 'number' and x == x and x ~= math.huge and x ~= -math.huge end

function M.laneChangeCmd(dist, shift, laneW, clampToRoad)
  if not finite(dist) or not finite(shift) then return nil end
  local hb = math.max((tonumber(laneW) or 3.7) * 0.52, 1.55)
  local lockAt = math.max(12.0, (tonumber(dist) or 30.0) * 0.75)
  local cap = ''
  if type(clampToRoad) == 'number' then
    cap = string.format(
      'if x < -%.2f then x = -%.2f elseif x > %.2f then x = %.2f end; ',
      clampToRoad, clampToRoad, clampToRoad, clampToRoad)
  elseif clampToRoad then
    cap = 'if n.halfWidth then local lim = math.max(0.5, n.halfWidth - 0.25); if x < -lim then x = -lim elseif x > lim then x = lim end end; '
  end
  return string.format(
    'ai.laneChange(nil, %.1f, %.2f); local r = ai.getCurrentRoute and ai.getCurrentRoute(); '
    .. 'local p = r and r.plan; if p then local cd = 0; for i = 2, (p.planCount or 0) do '
    .. 'local prev = p[i-1]; cd = cd + ((prev and prev.length) or 0); local n = p[i]; '
    .. 'if n and n.lateralXnorm then local x = n.lateralXnorm; %s'
    .. 'n.lateralXnorm = x; if n.posOrig and n.normal and n.pos and n.pos.setAddScaled then n.pos:setAddScaled(n.posOrig, n.normal, x) end; '
    .. 'if cd >= %.1f and n.halfWidth then '
    .. 'local rl = n.rangeLeft and ((n.rangeLeft * 2 - 1) * n.halfWidth) or -n.halfWidth; '
    .. 'local rr = n.rangeRight and ((n.rangeRight * 2 - 1) * n.halfWidth) or n.halfWidth; '
    .. 'n.laneLimLeft = math.max(rl, math.min(rr - 0.5, x - %.2f)); '
    .. 'n.laneLimRight = math.min(rr, math.max(n.laneLimLeft + 0.5, x + %.2f)) '
    .. 'end end end end',
    dist, shift, cap, lockAt, hb, hb)
end

local function stockGraphNode(m, name) return m and m.nodes and m.nodes[name] end

-- The car's edge on the game's map: lanes its way, its lane, lane width, the edge's limit, its heading along the edge.
local function onRoad(m, o, p, f)
  local n1, n2 = map.findClosestRoad(p)
  local a, c = stockGraphNode(m, n1), stockGraphNode(m, n2)
  local e = a and c and (a.links[n2] or c.links[n1])
  if not e or not e.oneWay then return nil end
  local inN, outN = m.nodes[e.inNode or n1], m.nodes[e.outNode or n2]
  if not inN or not outN then return nil end
  local d = outN.pos - inN.pos
  d.z = 0
  if d:length() < 0.1 then return nil end
  d = d:normalized()
  local along = d:dot(f) > 0
  if not along then return nil end           -- (a one-way road driven the wrong way: not ours to sort out)
  local n = M.lanesMyWay(e.lanes, true)
  if n < 2 then return nil end
  local r = vec3(d.y, -d.x, 0)
  local lat = (p - inN.pos):dot(r)
  local radius = math.min(inN.radius or e.inRadius or 0, outN.radius or e.outRadius or 0)
  if radius <= 0 then radius = e.inRadius or 0 end
  local k, w = M.laneOf(lat, radius, n)
  if not k then return nil end
  return { n = n, k = k, w = w, lat = lat, radius = radius, limit = e.speedLimit or 0, dir = d, right = r }
end

-- Drop every entry of a per-car table whose car is no longer in carIds (and is not the player's car).
local function forget(tbl, carIds, keep)
  for id in pairs(tbl) do
    if not carIds[id] and id ~= keep then tbl[id] = nil end
  end
end

-- Is a scan due (navgraph's overtakeScan asks first: the traffic list and the snapshots it builds for a scan are
-- worth making only every SCAN_S, not every frame)?
function M.due(now)
  return M.enabled and now - lastScan >= M.SCAN_S
end

-- One scan (navgraph.update calls it every frame; it runs every SCAN_S). cars: {id = true} of the traffic cars; player:
-- the player's car id while it autodrives, else nil. nearJunction(x, y, r) -> bool. leftHand: traffic on the left.
function M.update(now, carIds, playerAuto, nearJunction, leftHand, controlledFn, playerRace)
  if not M.enabled or now - lastScan < M.SCAN_S then return end
  lastScan = now
  if simTimeAuthority and simTimeAuthority.getPause and simTimeAuthority.getPause() then return end
  local m = map and map.getMap and map.getMap()
  if not m or not m.nodes then return end
  -- every vehicle that may block or be passed: positions, headings, speeds
  local all = {}
  local function add(id, passer)
    local o = getObjectByID(id)
    if o and o:getActive() then
      local f = o:getDirectionVector()
      f.z = 0
      if f:length() > 0.1 then
        all[#all + 1] = { id = id, o = o, p = o:getPosition(), f = f:normalized(), v = o:getVelocity():length(),
                          passer = passer, race = (id == playerAuto and (playerRace or M.playerRace)) or false }
      end
    end
  end
  for id in pairs(carIds) do add(id, not (controlledFn and controlledFn(id))) end
  if playerAuto then add(playerAuto, M.passesPlayer(playerRace)) end
  for _, c in ipairs(all) do
    local ok, err = pcall(function()
      if not c.passer then return end
      local road = onRoad(m, c.o, c.p, c.f)
      if not road then held[c.id], passing[c.id] = nil, nil return end
      -- the others round it, in its frame
      local others, lead, gap = {}, nil, nil
      local maxLook = c.race and 160 or 120
      for _, o in ipairs(all) do
        if o ~= c then
          local d = o.p - c.p
          local ahead = d:dot(road.dir)
          local side = d:dot(road.right)
          if math.abs(ahead) < maxLook and (o.f:dot(c.f) > 0.5 or o.v < M.WRECK_SPEED_MS) then
            others[#others + 1] = { ahead = ahead, side = side, v = o.v, id = o.id }
            if ahead > 0 and math.abs(side) < M.LANE_TOL_M and (not gap or ahead < gap) then lead, gap = o, ahead end
          end
        end
      end
      local p = passing[c.id]
      if p then
        returnSince[c.id] = nil
        local l
        for _, o in ipairs(others) do if o.id == p.lead then l = o end end
        if not l or l.ahead < -M.PAST_M or now - p.at > M.GIVE_UP_S then
          passing[c.id] = nil                 -- past it (or it is gone)
        else
          if p.targetK and road.k ~= p.targetK and p.targetK >= 0 and p.targetK < road.n
             and now - (p.lastCmd or p.at) >= 1.8 and (p.retries or 0) < 2 then
            local corr = (p.targetK - road.k) * road.w
            if M.laneClear(others, corr, M.CLEAR_BACK_M * 0.7, M.CLEAR_AHEAD_M * 0.7, c.v) then
              local dist = math.max(M.SHIFT_MIN_M, c.v * (c.race and 1.8 or M.SHIFT_S))
              local cmd = M.laneChangeCmd(dist, corr, road.w, c.id == playerAuto)
              if cmd then c.o:queueLuaCommand(cmd) end
              lastShift[c.id] = now
              p.lastCmd, p.retries = now, (p.retries or 0) + 1
            end
          end
          return
        end
      end
      local wreck = lead and lead.v < M.WRECK_SPEED_MS
      local effLimit = (c.id == playerAuto and (M.playerSpeedMph or 0) > 0)
        and math.max(road.limit, M.playerSpeedMph * 0.44704) or road.limit
      if not lead or not M.isHeld(c.v, effLimit, gap, lead.v, c.race) or (not wreck and c.v < M.MIN_MS * 0.5) then
        held[c.id] = nil
        -- Empty passing lane: after RETURN_HELD_S, move one lane back toward the travel lane.
        if not c.race and road.limit >= 18 and c.v >= M.MIN_MS
            and now - (lastShift[c.id] or -math.huge) >= M.RETURN_COOLDOWN_S
            and not (nearJunction and nearJunction(c.p.x, c.p.y, M.JUNCTION_M)) then
          local backShift = M.returnShift(road.k, road.n, road.w, leftHand, others,
            M.RETURN_BACK_M, M.RETURN_AHEAD_M, c.v)
          if backShift then
            returnSince[c.id] = returnSince[c.id] or now
            if now - returnSince[c.id] >= M.RETURN_HELD_S then
              local dist = math.max(M.SHIFT_MIN_M, c.v * M.SHIFT_S)
              local cmd = M.laneChangeCmd(dist, backShift, road.w, c.id == playerAuto)
              if cmd then c.o:queueLuaCommand(cmd) end
              lastShift[c.id], returnSince[c.id] = now, nil
            end
          else
            returnSince[c.id] = nil
          end
        end
        return
      end
      returnSince[c.id] = nil
      held[c.id] = held[c.id] or now
      local waitS = M.holdSeconds(wreck, c.race, playerAuto and c.id == playerAuto and M.playerPass,
        M.playerSpeedMph, M.playerAggression)
      local coolS = M.COOLDOWN_S
      if wreck then coolS = 2.5
      elseif c.race then coolS = M.RACE_COOLDOWN_S
      elseif playerAuto and c.id == playerAuto and M.playerPass then coolS = M.playerPassCoolS
      end
      if now - held[c.id] < waitS or now - (lastPass[c.id] or -math.huge) < coolS then return end
      if not wreck and c.v < M.MIN_MS then return end
      -- A stopped car still lined up with the road is a queue (signal, junction), not a wreck to swerve around: it
      -- keeps the junction check. Only a spun / sideways one skips it.
      local spun = wreck and lead.f and math.abs(lead.f:dot(road.dir)) < 0.85
      if not spun and nearJunction and nearJunction(c.p.x + road.dir.x * 60, c.p.y + road.dir.y * 60,
          c.race and 50 or M.JUNCTION_M) then
        return
      end
      -- Do not start another pass while the car is still crossing between lanes. A wreck may dodge at once.
      if not wreck and road.n >= 2 then
        local laneCenter = -road.radius + (road.k + 0.5) * road.w
        if math.abs(road.lat - laneCenter) > road.w * 0.32 then return end
      end
      local shift
      -- Legal passing (traffic, and Pass slow cars) stays on the passing side. Either side is only for a
      -- wreck or Race, which may use the exit-side lane to get around a stopped car.
      if wreck or c.race then
        local aheadClear = c.race and math.max(M.CLEAR_AHEAD_M, 85) or M.CLEAR_AHEAD_M
        shift = M.avoidShift(road.k, road.n, road.w, leftHand, others, M.CLEAR_BACK_M, aheadClear, c.v)
      else
        shift = M.passShift(road.k, road.n, road.w, leftHand)
        if shift and not M.laneClear(others, shift, M.CLEAR_BACK_M, M.CLEAR_AHEAD_M, c.v) then shift = nil end
      end
      if not shift then return end
      local targetK = math.max(0, math.min(road.n - 1, road.k + (shift < 0 and -1 or 1)))
      local dist = math.max(wreck and 18 or M.SHIFT_MIN_M, c.v * (c.race and 1.8 or M.SHIFT_S))
      local cmd = M.laneChangeCmd(dist, shift, road.w, c.id == playerAuto)
      if not cmd then return end
      c.o:queueLuaCommand(cmd)
      passing[c.id] = { lead = lead.id, at = now, lastCmd = now, targetK = targetK, retries = 0 }
      lastPass[c.id], lastShift[c.id], held[c.id], returnSince[c.id] = now, now, nil, nil
      stats.passes = stats.passes + 1
    end)
    if not ok then log('W', 'beamngEarth', 'overtake: ' .. tostring(err)) end
  end
  -- forget the cars that are gone (a car may hold only a return timer or shift stamp, with no pass on record)
  forget(lastPass, carIds, playerAuto)
  forget(held, carIds, playerAuto)
  forget(passing, carIds, playerAuto)
  forget(lastShift, carIds, playerAuto)
  forget(returnSince, carIds, playerAuto)
end

function M.status(id)
  if id == nil then return { passes = stats.passes } end
  local p = passing[id]
  return {
    passes = stats.passes,
    active = p ~= nil,
    blocked = held[id] ~= nil,
    targetLane = p and p.targetK or nil,
    retries = p and p.retries or nil,
  }
end

function M.reset()
  held, passing, lastPass, lastShift, returnSince, lastScan = {}, {}, {}, {}, {}, -math.huge
end

return M
