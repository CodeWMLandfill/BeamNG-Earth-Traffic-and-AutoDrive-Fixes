-- The AI road graph (server/navgraph.py, --traffic on) handed to the game's own navgraph with
-- map.reset(customNodes) (docs/research/2026-10-10-navgraph-traffic-signals.md sec.2.4-2.8, the plan in sec.8): BeamNG's
-- traffic and AI then drive our streamed roads. The server sends a graph around the player every ~400 m; this
-- module clips it to the terrain block (the only collidable ground, minus EDGE_M) and applies it whenever a new
-- graph arrives or the block moved, at most every MIN_GAP_S.
--
-- The custom map path's pitfalls (research sec.2.7), each handled here: vehicles are told to fetch the new graph
-- (mapmgr.requestMap); the node table object stays the same one across resets (gameplay_traffic caches it); every
-- edge carries its speed limit and lanes; the road rules come from a file of ours (the level's info.json has none);
-- a reset by someone else (map.reset() with no graph rebuilds an empty one from our level) is undone next frame.
local M = {}
local localplayers = require('beamngEarth/localplayers')
local overtake = require('beamngEarth/overtake')
local controlled = {}

M.EDGE_M = 60           -- graph nodes this far inside the block's ground box only
M.MIN_GAP_S = 1.0
-- m past the block's ground box (less EDGE_M) the graph's nodes are kept for the AI's route planning (GitHub #657 /
-- #279: the map ended a few hundred metres ahead, the AI planned back the other way and turned round in the road);
-- the cuts (headingToCut) and the guard stay at the box: a car is respawned before it leaves the ground. 0: off.
M.HALO_M = 0
-- ... but the fast roads' nodes (edges at FAST_HALO_MS or more: motorways, fast trunk roads) are kept FAST_HALO_M past it:
-- a highway's graph ended ~450 m ahead, so a traffic car placed there (the stock traffic's respawns go 100-500 m in
-- front of the camera) braked for the dead end and turned round across the lanes: the cars standing sideways on I-94 /
-- I-90 in Montana (Tann2019, 2026-11-05; PR #728's route halo, there for blocks over 1200 m only). A motorway's nodes are
-- ~80 m apart: a few dozen nodes, not the tripled city graph of HALO_M.
-- A flowing car on a fast road is not yanked while it crosses the inner physics cut onto this
-- extension (skipFastHaloCut, still inside the block). A car that has stopped or turned at a cut
-- is still recycled by cutStopped; skipFastHaloCut never clears that path.
-- Cost note: with the halo at 650 m an earlier measurement (before this change) read 87.9 / 84.5 fps against 92.8 / 88.2
-- with it off, and 89 frames over 50 ms in PhysicsAndVLUA in one run (the traffic AI's planning on the longer graph).
-- It is on here because far-ahead spawns (AHEAD_M, ONCOMING_M) need runway; set 0 to turn it off.
M.FAST_HALO_M = 650
M.FAST_HALO_MS = 24.5     -- (55 mph)
-- An apply (or its signals' second step) waits for a light frame (governor roomFor: at most APPLY_ROOM_MS of streaming
-- and other work spent in it, not a terrain commit's), APPLY_WAIT_FRAMES at most
M.APPLY_ROOM_MS = 4.0
M.APPLY_WAIT_FRAMES = 30
-- The block moved this far from where the graph was last clipped: clipped again to the new block (the server's graph
-- reaches WINDOW_M = 1100 m, the block ~450 m; without it the graph ran out ahead of a car driving on before the next
-- graph came, every 400 m: the GPS arrows ended and the autodrive stopped, the user's Cannonball run 2026-10-14)
M.RECLIP_M = 150
M.UI_MAP = false        -- send our graph to the UI (the big map) on every apply
M.RULES_FILE = '/temp/beamngEarth/roadRules.json'
M.rules = { rightHandDrive = false, turnOnRed = true }    -- US; `rightHandDrive = true` means driving on the left
M.enabled = true
-- Traffic (gameplay_traffic, the player's traffic amount): started once the first graph is in the map; unoccupied AI
-- within GUARD_M of the block's edge (or past it, where there is no ground) is respawned on the graph
-- (gameplay_traffic.forceTeleport), checked every GUARD_S; after a teleport the AI is respawned when the new graph is in.
M.GUARD_M = 40
M.GUARD_S = 0.5
-- A car standing still this long (longer than a signal cycle), this far from the player, is respawned too: stock
-- traffic respawns only cars far away and out of sight, so one wedged on a corner the map's road cuts through a
-- building, or queued behind the player's parked car in a one-lane street, stood for good (Frederick, Fells Point
-- 2026-10-14)
M.STUCK_S = 45
M.STUCK_MS = 0.5
M.STUCK_FAR_M = 40
M.RESPAWN_M = { 60, 300 }   -- a respawned car goes this far (min, max) behind the camera
-- A car heading for a graph node whose road the clip cut off (within 45 degrees, CUT_M or CUT_S of driving away) is
-- respawned too: to the AI the cut is a dead end, and a car spawned facing one made a three-point turn on the spot
-- (GitHub #72, "spawns the wrong way and tries to turn around immediately"; Bathurst 2026-10-21: 20 m from a cut)
-- A car that has just appeared (FRESH_S) turns round for a cut much farther ahead (Bathurst: 89 m): CUT_FRESH_M
-- (250 m sent cars respawned near a cut straight back again: traffic popping in and out, PR #493, OmdevShastri).
-- A car respawned for a cut or for standing still is not respawned for those again within COOLDOWN_S (one that
-- leaves the block's guard still is at once).
M.CUT_M = 60
M.CUT_S = 4
M.CUT_FRESH_M = 95
-- ... and at least as far as the car needs to stop for it (its speed squared over 2 CUT_DECEL, CUT_STOP_M more): the
-- AI plans to the end of the graph and brakes for the dead end long before 4 s from it; at 70 mph it braked from
-- ~200 m and turned round across the lanes ahead of the player, every half minute on a motorway (GitHub #855 / #966)
M.CUT_DECEL = 2.5
M.CUT_STOP_M = 30
M.FRESH_S = 5
M.COOLDOWN_S = 8.0
-- A car that reached the cut no longer heads for it: it stopped at the dead end at an angle, or it is turning round
-- across the road (the AI's three-point turn: crawling through the swing, reversing, or standing across its road's
-- axis), so headingToCut's 45-degree test let it go and it waited for the generic stuck timer -- STUCK_S (45 s), and
-- only STUCK_FAR_M or more from the player: time enough to block the lane and U-turn in front of the player
-- (GitHub #855, "happens everywhere PERMANENTLY", with #72 / #279 the history; driving slowly near Oceanside CA
-- 2026-11-10: cars stopping and U-turning constantly). Within CUT_NEAR_M of a cut, a car reversing, moving more
-- than CUT_TURN_COS off its road's axis or along a one-way road against it, or nearly stationary (under CUT_NEAR_MS)
-- while facing the dead end or standing across its road, is respawned at once -- no STUCK_S wait; COOLDOWN_S and the
-- fresh-car window (FRESH_S, a freshly placed car stands still before its launch) still apply, and nothing is taken
-- while the game is paused. A car merely slow or stopped near a cut is not the cut's: a red light or a stop sign on
-- a through road of a junction the clip cut holds cars just there, aligned with their road and facing past the dead
-- end -- those are left alone (cutStopped). Still uncaught: a car that completed its turn before a scan saw it and
-- stands or drives back along a two-way road reads as ordinary oncoming traffic and is left alone.
M.CUT_NEAR_M = 30
M.CUT_NEAR_MS = 2.5
M.CUT_TURN_COS = 0.5    -- (cos 60 degrees)
-- The guard is frame-timed: a long frame (streaming hitches, 0.3-5 s in session logs) runs between two scans, and a
-- car covers the whole gap unwatched -- CUT_STOP_M's margin assumes a scan every GUARD_S, and the first frame after
-- the gap runs the scan at once but measured only the car's position now. That scan widens each car's cut reach
-- (cutReach), and JUMP_M's respawn-detection threshold, by the missed time at the car's speed, so the stretch it
-- drove meanwhile is still inside the window; capped at GUARD_LAG_MAX_S (a level load or a pause is not a hitch).
M.GUARD_LAG_MAX_S = 6
M.JUMP_M = 30           -- a car this much farther than at the last check was respawned (stock or ours)
-- At speed a car respawned behind the camera is never seen again: on a motorway every car ahead reached the
-- block edge within about 30 s and reappeared behind the player. Faster than AHEAD_MS, a respawned car is
-- placed on the highway graph AHEAD_M (min, max) ahead, inside the block guard. A short block goes behind.
M.AHEAD_MS = 20         -- (45 mph)
M.AHEAD_M = { 420, 760 }
M.UNCLOAK_MIN_M = 360
M.AHEAD_FULL = 4        -- this many same-way cars already ahead: do not add another in front
M.CLEAR_GAP_M = 150     -- open gap between an existing car and a newly placed platoon
M.PLATOON_GAP_M = 34    -- spacing of a second car placed behind the lead of a platoon
M.CLOAK_MAX_S = 1.2     -- pre-roll settle budget; still cloaked after this, recycle behind the camera
-- ... and every other one on the other carriageway, ONCOMING_M ahead, facing the player: on a divided highway the cars
-- put ahead all went the player's way and the other carriageway stayed empty (Tann2019, I-94 2026-11-05: "is there a
-- way so that traffic goes both ways on the interstate"). The other carriageway is looked for ONCOMING_SIDE_M to the
-- side the oncoming traffic drives on (left in right-hand traffic): a one-way road there running against the player.
M.ONCOMING = true
M.ONCOMING_M = { 760, 680, 600, 520, 450, 380, 320 }
M.ONCOMING_SIDE_M = { 14, 20, 26, 34, 44, 56 }
M.ONCOMING_ON_M = 6     -- the point that far from the road at most
-- A respawned traffic car is launched at LAUNCH_SHARE of its road's limit, its wheels spinning with it (PR #728 by
-- ProxyFirewall202: spawnVelocityCommand and the vehicle's beamngEarth_spawnMotion): it came at 0 mph, so on a highway the
-- cars round the player crawled up from a standstill after every respawn (every 5-30 s at 65 mph) and the autodrive
-- behind them did 40 (Tann2019, Huntley to Billings 2026-11-05). Not near a junction (LAUNCH_JUNCTION_M), not facing
-- off its road (LAUNCH_ALIGN) or against a one-way road; looked at every LAUNCH_SCAN_S, once the stock traffic has placed
-- it (LAUNCH_DELAY_S after it is active), given up after LAUNCH_TIMEOUT_S.
M.LAUNCH = true
M.LAUNCH_SHARE = 0.9
M.LAUNCH_MIN_MS = 8
M.LAUNCH_ALIGN = 0.94     -- (cos 20 degrees)
M.LAUNCH_JUNCTION_M = 20
M.LAUNCH_SCAN_S = 0.05
M.CONFLICT_CACHE_S = 0.2   -- a car's spawnConflict (a full map.objects loop) is not asked again within this long
M.LAUNCH_DELAY_S = 0.22     -- (0.12 in PR #728 once read 63,000 mph; 0.4 left a respawned car sitting in a live lane)
M.LAUNCH_TIMEOUT_S = 6
M.LAUNCH_HEADWAY_S = 1.8    -- headway to any slower vehicle ahead in the same lane when capping launch speed
M.LAUNCH_LANE_M = 3.6       -- lateral half-band treated as the same lane for launch / spawn conflict checks
M.SPAWN_CONFLICT_M = 55     -- respawned at 0 mph within this distance in front of fast traffic: recycled behind camera
M.WRECK_DAMAGE = 150        -- damaged AI car on a fast road cleared after WRECK_CLEAR_S even inside STUCK_FAR_M
M.WRECK_CLEAR_S = 2.0
M.HIGHWAY_CLEAR_MS = 18     -- road speed limit (m/s, ~40 mph) counting as a fast road for wreck / stall clearing
M.HIGHWAY_STOP_S = 3.5      -- stalled AI car (< 2 m/s) in a live highway lane away from junctions cleared after this
M.HIGHWAY_JUNC_M = 55
M.HAZARD_MIN_DIST_M = 8     -- keep clear while actively colliding right against the player's bumper
-- (gameplay_walk: 0.40's traffic.lua calls gameplay_walk.addVehicleToBlacklist when it activates its cars, and this
-- level had none loaded: the cars spawned and never drove, 2026-10-19)
M.EXTENSIONS = { 'core_multiSpawn', 'gameplay_traffic', 'gameplay_walk' }

local graph = nil       -- the server's last graph {anchor, nodes, edges}
local applied = nil     -- {anchor, serial, box} of the graph in the game's map now
local dirty = false
local lastApply = -math.huge
local persistent = nil  -- the node table handed out once (research sec.2.7.2)
local ours = false
local foreign = false
local urgent = false      -- a rebase moved the graph: applied again without waiting for MIN_GAP_S (M.transform)
M.HEADS_ONLY = true      -- setupSignals sees only our signal heads, not every TSStatic (applySignals)
local stats = { applies = 0, lastMs = 0, nodes = 0, edges = 0 }
-- (the stopwatch's wall clock for the parts of an apply; in the game os.clock reads the same, measured 2026-11-09 on
-- Linux and Windows)
local stopwatch = require('beamngEarth/stopwatch')
local wall = stopwatch.clock()       -- ms since load, monotonic (one clock: stopwatch.clock() makes a new one)
local pendingSignals = nil -- the clipped nodes whose signals go in next frame
local started = false
local respawnAll = false
local lastGuard = -math.huge
local cuts = {}         -- { x, y } of the applied graph's nodes that lost a road to the clip
local launchCount, launchWait, lastLaunchScan = {}, {}, -math.huge
local conflictCache = {}   -- car id -> { at, v }: the last spawnConflict result, reused for M.CONFLICT_CACHE_S

-- Pure: point inside box {x0, y0, x1, y1} shrunk by m (no box: anywhere).
function M.inside(b, x, y, m)
  return not b or (x >= b[1] + m and x <= b[3] - m and y >= b[2] + m and y <= b[4] - m)
end

-- Pure: the lanes string of an edge (one char per lane, '+' along in -> out, '-' against; research sec.2.2), the
-- lanes against the direction first for right-hand traffic.
function M.lanes(out, back, leftHand)
  local plus, minus = string.rep('+', out), string.rep('-', back)
  return leftHand and (plus .. minus) or (minus .. plus)
end

-- A link steeper than this (rise over run) is left out of the graph: no car drives it, and the AI given a path over
-- one circled under it for ever (GitHub #131: Baltimore's East Eager Street, a bridge whose first node stood 6.4 m
-- over its junction with Greenmount Avenue 8 m away; the steepest real streets are ~0.37, San Francisco's).
M.MAX_GRADE = 0.45

-- Pure: is the link between graph nodes p and q ({x, y, z}) too steep to drive (MAX_GRADE)?
function M.tooSteep(p, q)
  local run = math.sqrt((p[1] - q[1]) ^ 2 + (p[2] - q[2]) ^ 2)
  return math.abs(p[3] - q[3]) > M.MAX_GRADE * math.max(run, 1.0)
end

-- Contract isolated sub-metre seams on a continuous road with matching directed lane metadata. The stock marker
-- planner merges points this close, but the vehicle AI given both nodes can make a sharp lane adjustment and stop.
-- Keep junctions, clip boundaries, controls and changes in lanes/width/rules; the downstream node stays stable.
function M.compactShortEdges(nodes, protected, step)
  local function other(id, except)
    local found
    for nextId in pairs(nodes[id].links) do
      if nextId ~= except then if found then return nil end; found = nextId end
    end
    return found
  end
  local function lengthSq(a, b)
    return (a.x - b.x)^2 + (a.y - b.y)^2 + (a.z - b.z)^2
  end
  local function aligned(a, b, c, d)
    local dot = (b.x - a.x) * (d.x - c.x) + (b.y - a.y) * (d.y - c.y) + (b.z - a.z) * (d.z - c.z)
    return dot > 0.98 * math.sqrt(lengthSq(a, b) * lengthSq(c, d))
  end
  local function sameLanes(a, b)
    return a.oneWay == b.oneWay and a.lanes == b.lanes and a.drivability == b.drivability
      and a.rough == b.rough and a.rumble == b.rumble
  end
  local ids, removed = {}, 0
  for id in pairs(nodes) do
    ids[#ids + 1] = id
    if step then step() end
  end
  table.sort(ids)
  for _, a in ipairs(ids) do
    if step then step() end
    local A = nodes[a]
    if A and not protected[a] then
      for b, e in pairs(A.links) do
        local B = nodes[b]
        if e.inNode == a and not protected[b] and lengthSq(A.pos, B.pos) <= 1 then
          local p, q = other(a, b), other(b, a)
          if p and q and p ~= q then
            local P, Q = nodes[p], nodes[q]
            local before, after = A.links[p], B.links[q]
            if before.inNode == p and after.inNode == b and sameLanes(before, e) and sameLanes(e, after)
              and before.outRadius == e.inRadius and e.inRadius == e.outRadius and e.outRadius == after.inRadius
              and e.speedLimit == after.speedLimit and e.posted == after.posted
              and lengthSq(P.pos, A.pos) > 1 and lengthSq(B.pos, Q.pos) > 1
              -- At <=5 cm, coordinate rounding can give the seam a zero or arbitrary direction; up to 20 cm,
              -- collapse forward micro-jogs whose two flanking edges stay collinear.
              and ((aligned(P.pos, A.pos, A.pos, B.pos) and aligned(A.pos, B.pos, B.pos, Q.pos))
                or (lengthSq(A.pos, B.pos) <= 0.0025 and aligned(P.pos, A.pos, B.pos, Q.pos))
                or (lengthSq(A.pos, B.pos) <= 0.04 and aligned(P.pos, A.pos, B.pos, Q.pos)
                  and ((A.pos.x - P.pos.x) * (B.pos.x - A.pos.x) + (A.pos.y - P.pos.y) * (B.pos.y - A.pos.y)
                    + (A.pos.z - P.pos.z) * (B.pos.z - A.pos.z))
                    > 0.5 * math.sqrt(lengthSq(P.pos, A.pos) * lengthSq(A.pos, B.pos)))) then
              P.links[a], B.links[a] = nil, nil
              P.links[b], B.links[p] = before, before
              before.outNode, before.outPos = b, B.pos
              nodes[a], removed = nil, removed + 1
              break
            end
          end
        end
      end
    end
  end
  return removed, ids
end

-- A straight shape node inside a junction's road radius can put the stock AI's preceding lane point past its
-- turn point, reversing the turn normal. Remove only redundant approach geometry; keep the junction and controls.
function M.compactJunctionApproaches(nodes, protected, step, ids)
  local function degree(n)
    local count = 0
    for _ in pairs(n.links) do count = count + 1 end
    return count
  end
  -- Reuse the seam pass's sorted IDs; removed nodes are skipped below. Keep appending neighbours for rechecks.
  local queue, removed = ids or {}, 0
  if not ids then
    for id in pairs(nodes) do
      queue[#queue + 1] = id
      if step then step() end
    end
    table.sort(queue)
  end
  local i = 1
  while i <= #queue do
    if step then step() end
    local a = queue[i]
    i = i + 1
    local A = nodes[a]
    if A and not protected[a] and degree(A) == 2 then
      for b, e in pairs(A.links) do
        local B = nodes[b]
        if degree(B) >= 3 then
          local p = next(A.links)
          if p == b then p = next(A.links, p) end
          local P, before = nodes[p], A.links[p]
          local forward = before.outNode == a and e.inNode == a
          local reverse = before.inNode == a and e.outNode == a
          local radius = e.inRadius
          if not P.links[b] and (forward or reverse) and radius and radius == e.outRadius
            and before.inRadius == radius and before.outRadius == radius
            and before.oneWay == e.oneWay and before.lanes == e.lanes
            and before.drivability == e.drivability and before.rough == e.rough and before.rumble == e.rumble
            and before.posted == e.posted and type(before.speedLimit) == 'number' and type(e.speedLimit) == 'number' then
            local dx, dy, dz = B.pos.x - P.pos.x, B.pos.y - P.pos.y, B.pos.z - P.pos.z
            local ax, ay, az = A.pos.x - P.pos.x, A.pos.y - P.pos.y, A.pos.z - P.pos.z
            local lenSq = dx * dx + dy * dy + dz * dz
            local t = lenSq > 0 and (ax * dx + ay * dy + az * dz) / lenSq or -1
            local gapSq = (A.pos.x - B.pos.x)^2 + (A.pos.y - B.pos.y)^2 + (A.pos.z - B.pos.z)^2
            -- A shortcut must stay within 10 cm of the original line and must not fold back or skip a bend.
            if t > 0 and t < 1 and gapSq <= radius * radius
              and (ax - t * dx)^2 + (ay - t * dy)^2 + (az - t * dz)^2 <= 0.01 then
              P.links[a], B.links[a] = nil, nil
              P.links[b], B.links[p] = before, before
              if forward then before.outNode, before.outPos = b, B.pos
              else before.inNode, before.inPos = b, B.pos end
              before.speedLimit = math.min(before.speedLimit, e.speedLimit)
              nodes[a], removed = nil, removed + 1
              queue[#queue + 1] = p -- another redundant point may now be the junction's nearest neighbour
              break
            end
          end
        end
      end
    end
  end
  return removed
end

-- BeamNG skips mergeOverlappingNodes/optimizeNodes when map.reset receives customMapNodes, so sub-half-meter
-- connector/approach stubs (<= STUB_EDGE_M) cause ai.lua curvature spikes and nil closestRoad/edgeGeom lookups.
M.STUB_EDGE_M = 0.5
M.DUPLICATE_NODE_M = 0.05

local function linkDeg(nd)
  local n = 0
  for _ in pairs(nd.links) do n = n + 1 end
  return n
end

local function edgeLen3d(aPos, bPos)
  local dx = bPos.x - aPos.x
  local dy = bPos.y - aPos.y
  local dz = (bPos.z or 0) - (aPos.z or 0)
  return math.sqrt(dx * dx + dy * dy + dz * dz)
end

local function signalProtected(signals)
  local keep = {}
  if type(signals) ~= 'table' then return keep end
  for _, j in ipairs(signals) do
    if j.j ~= nil then keep[j.j] = true end
    if type(j.app) == 'table' then
      for _, a in ipairs(j.app) do
        if a[1] ~= nil then keep[a[1]] = true end
      end
    end
  end
  return keep
end

local function allowsFlow(e, fromId, toId)
  return not e.oneWay or (e.inNode == fromId and e.outNode == toId)
end

local function compatibleBypass(eLong, eShort, other, u, v)
  if (eLong.oneWay == true) ~= (eShort.oneWay == true) then return false end
  if not eLong.oneWay then return true end
  return (allowsFlow(eLong, other, u) and allowsFlow(eShort, u, v))
    or (allowsFlow(eShort, v, u) and allowsFlow(eLong, u, other))
end

local function rewireEdgeNode(edge, u, v, vNode, vRadius)
  if edge.inNode == u then
    edge.inNode, edge.inPos = v, vNode.pos
    if vRadius then edge.inRadius = vRadius end
  elseif edge.outNode == u then
    edge.outNode, edge.outPos = v, vNode.pos
    if vRadius then edge.outRadius = vRadius end
  end
end

local function tryDropNode(nodes, u, v, protected, allowDupJunction)
  if protected[u] then return nil end
  local U, V = nodes[u], nodes[v]
  if not U or not V then return nil end
  local nbrs = {}
  for k in pairs(U.links) do nbrs[#nbrs + 1] = k end
  if #nbrs == 1 then
    if linkDeg(V) < 2 then return nil end
    V.links[u] = nil
    nodes[u] = nil
    return 1, 1
  elseif #nbrs == 2 then
    local other = nbrs[1] == v and nbrs[2] or nbrs[1]
    if other == v or nodes[other].links[v] ~= nil then return nil end
    local eLong, eShort = U.links[other], U.links[v]
    if not compatibleBypass(eLong, eShort, other, u, v) then return nil end
    local vr = (eShort.inNode == v and eShort.inRadius) or (eShort.outNode == v and eShort.outRadius) or V.radius
    rewireEdgeNode(eLong, u, v, V, vr)
    nodes[other].links[u] = nil
    nodes[other].links[v] = eLong
    V.links[u] = nil
    V.links[other] = eLong
    nodes[u] = nil
    return 1, 1
  elseif allowDupJunction then
    for _, k in ipairs(nbrs) do
      if k ~= v and nodes[k].links[v] ~= nil then
        local ekU, ekV = U.links[k], nodes[k].links[v]
        if ekU.oneWay or ekV.oneWay then return nil end
      end
    end
    local droppedEdges = 1
    V.links[u] = nil
    for _, k in ipairs(nbrs) do
      if k ~= v then
        local ek = U.links[k]
        nodes[k].links[u] = nil
        if nodes[k].links[v] == nil then
          local vr = (U.links[v] and ((U.links[v].inNode == v and U.links[v].inRadius) or U.links[v].outRadius)) or V.radius
          rewireEdgeNode(ek, u, v, V, vr)
          nodes[k].links[v] = ek
          V.links[k] = ek
        else
          droppedEdges = droppedEdges + 1
        end
      end
    end
    nodes[u] = nil
    return 1, droppedEdges
  end
  return nil
end

-- Collapse degenerate sub-half-meter dead-end dangles and zero-length duplicate junctions while keeping road seams,
-- junctions, signals, one-way legality, and double-sided links governed by the metadata-aware compaction passes.
function M.collapseShortEdges(nodes, nn, ne, signals, maxM, stepFn, extraProtected, sortedIds, idCount)
  maxM = tonumber(maxM) or M.STUB_EDGE_M or 0
  if maxM <= 0 then return nn, ne end
  local protected = signalProtected(signals)
  if type(extraProtected) == 'table' then
    for k, v in pairs(extraProtected) do
      if v then protected[k] = true end
    end
  end
  local ids = sortedIds
  if not ids then
    ids = {}
    for id in pairs(nodes) do
      ids[#ids + 1] = id
      if stepFn then stepFn() end
    end
    table.sort(ids)
  end
  local nIds = idCount or #ids
  repeat
    local changed = false
    for i = 1, nIds do
      if stepFn then stepFn() end
      local a = ids[i]
      local A = nodes[a]
      if A and not protected[a] then
        local da = linkDeg(A)
        if da == 1 then
          local b = next(A.links)
          local B = b and nodes[b]
          if B and linkDeg(B) >= 2 and edgeLen3d(A.pos, B.pos) <= maxM then
            local dn, de = tryDropNode(nodes, a, b, protected, false)
            if dn then nn, ne, changed = nn - dn, ne - de, true end
          end
        elseif da >= 2 then
          for b in pairs(A.links) do
            local B = nodes[b]
            if B and not protected[b] and edgeLen3d(A.pos, B.pos) < M.DUPLICATE_NODE_M then
              local db = linkDeg(B)
              if da >= 3 or db >= 3 then
                local u, v = a, b
                if db < da or (db == da and b > a) then u, v = b, a end
                local dn, de = tryDropNode(nodes, u, v, protected, true)
                if dn then
                  nn, ne, changed = nn - dn, ne - de, true
                  break
                end
              end
            end
          end
        end
      end
    end
  until not changed
  return nn, ne
end

-- Pure-ish (needs vec3): the stock map's node table of graph g clipped to box b: {name = {pos, radius,
-- links = {other = edge}}}, every edge shared by both ends (double-sided) with in/out nodes, positions and radii.
-- Returns nodes, node count, edge count and the cuts: { x, y } of each kept node with a road to a node left out.
function M.toNodes(g, b, edgeM, leftHand, yieldEvery, halo, fastHalo)
  local nodes, nn, ne = {}, 0, 0
  local keep = {}
  local fast = nil        -- (fastHalo: the nodes of the fast roads, FAST_HALO_MS)
  if fastHalo and fastHalo > 0 then
    fast = {}
    for _, e in ipairs(g.edges) do
      if (tonumber(e[6]) or 0) >= M.FAST_HALO_MS then fast[e[1]], fast[e[2]] = true, true end
    end
  end
  local phys = {}         -- (halo: the nodes kept past the box's edge for the AI's route planning; the cuts stay at it)
  local steps = 0
  local function step()       -- (in a coroutine: a pause every yieldEvery items, buildNodes)
    if yieldEvery then
      steps = steps + 1
      if steps == yieldEvery then
        steps = 0
        coroutine.yield()
      end
    end
  end
  for name, n in pairs(g.nodes) do
    if M.inside(b, n[1], n[2], edgeM) then keep[name], phys[name] = n, true
    elseif halo and halo > 0 and M.inside(b, n[1], n[2], edgeM - halo) then keep[name] = n
    elseif fast and fast[name] and M.inside(b, n[1], n[2], edgeM - fastHalo) then keep[name] = n end
    step()
  end
  local function node(name)
    local t = nodes[name]
    if not t then
      local n = keep[name]
      t = { pos = vec3(n[1], n[2], n[3]), radius = n[4], links = {} }
      nodes[name] = t
      nn = nn + 1
    end
    return t
  end
  local cut, cutList = {}, {}
  for _, e in ipairs(g.edges) do
    step()
    local a, bb = e[1], e[2]
    for _, pair in ipairs({ { a, bb }, { bb, a } }) do
      local k = pair[1]
      if phys[k] and not phys[pair[2]] and g.nodes[pair[2]] and not cut[k] then
        cut[k] = true
        cutList[#cutList + 1] = { keep[k][1], keep[k][2] }
      end
    end
    if keep[a] and keep[bb] and a ~= bb and not M.tooSteep(keep[a], keep[bb]) then
      local A, B = node(a), node(bb)
      if not A.links[bb] then
        local edge = { drivability = e[7], oneWay = e[3] == 1, lanes = M.lanes(e[4], e[5], leftHand),
          speedLimit = e[6], inNode = a, outNode = bb, inPos = A.pos, outPos = B.pos,
          inRadius = e[8], outRadius = e[9] or e[8],      -- (e[9]: a road narrowing to a tunnel's portal)
          posted = e[10], rough = e[11] == true, rumble = e[12] == true } -- (posted/legal speed, OSM rough-road
          -- metadata, the country/road rumble heuristic)
        A.links[bb] = edge
        B.links[a] = edge
        ne = ne + 1
      end
    end
  end
  -- A clipped-away arm must not turn a junction into an apparent degree-two seam.
  for _, sg in ipairs(g.signals or {}) do
    if sg.j then cut[sg.j] = true end
    for _, app in ipairs(sg.app or {}) do if app[1] then cut[app[1]] = true end end
  end
  local removed, ids = M.compactShortEdges(nodes, cut, step)
  local origIdsCount = #ids
  removed = removed + M.compactJunctionApproaches(nodes, cut, step, ids)
  nn, ne = nn - removed, ne - removed
  nn, ne = M.collapseShortEdges(nodes, nn, ne, g.signals, M.STUB_EDGE_M, step, cut, ids, origIdsCount)
  -- Stock ai.lua uses mapData.radius[node] (not edge.inRadius) for every edge incident on `node`. At a merge where a
  -- 3-lane segment (half 6 m) meets a 4-lane segment (half 8 m) + a 1-lane ramp (half 2.4 m), max() inflated the merge
  -- node's radius to 8 m and stretched the 3-lane plan's lane centres out onto the painted white dividers. Use the
  -- narrowest incident multi-lane edge's half-width when any multi-lane edge meets there. `rough` on the edge is untouched.
  for _, nd in pairs(nodes) do
    local minMulti = math.huge
    for _, ed in pairs(nd.links) do
      if type(ed.lanes) == 'string' and #ed.lanes >= 2 then
        local r = math.min(ed.inRadius or math.huge, ed.outRadius or math.huge)
        if r < minMulti then minMulti = r end
      end
    end
    if minMulti < math.huge and nd.radius and minMulti < nd.radius then nd.radius = minMulti end
  end
  return nodes, nn, ne, cutList
end

M.FOLLOW_MS = 8         -- m/s the player moves at least for a car ahead going its way to keep going (followsPlayer)
M.FOLLOW_DOT = 0.7

-- Pure-ish (vectors with x, y): a traffic car at p moving along dir, ahead of a player at pp moving at pu, going the
-- player's way: the cut it heads for is the block's edge, which moves on with the player, so it is not respawned
-- for it (on a highway at 30 m/s a car ~200 m ahead was within the cut's reach and vanished in front of the player,
-- GitHub #353); it still is when it leaves the box (guard's GUARD_M).
-- How far ahead a car at sp m/s sees a dead end it will brake for (CUT_DECEL, CUT_STOP_M). Pure.
function M.stopReach(sp)
  return (sp or 0) ^ 2 / (2 * M.CUT_DECEL) + M.CUT_STOP_M
end

-- (car_sp: a car faster than the player by FOLLOW_FASTER_MS outruns the graph growing with the player: not following;
-- a car already braking below FOLLOW_SLOWER_SHARE of the player's speed is stalling into a cut, not following either)
M.FOLLOW_FASTER_MS = 2.0
M.FOLLOW_SLOWER_SHARE = 0.55
function M.followsPlayer(p, dir, pp, pu, car_sp)
  local ps = math.sqrt(pu.x * pu.x + pu.y * pu.y)
  local dl = math.sqrt(dir.x * dir.x + dir.y * dir.y)
  if ps < M.FOLLOW_MS or dl < 1e-6 then return false end
  if car_sp and (car_sp > ps + M.FOLLOW_FASTER_MS or car_sp < ps * M.FOLLOW_SLOWER_SHARE) then return false end
  local ux, uy = pu.x / ps, pu.y / ps
  if (dir.x * ux + dir.y * uy) / dl < M.FOLLOW_DOT then return false end
  return (p.x - pp.x) * ux + (p.y - pp.y) * uy > 0
end

-- Pure: how far a car at sp m/s sees a cut it heads for (CUT_M / CUT_S, CUT_FRESH_M when fresh, at least its
-- stopping distance), widened by the time the guard missed (lag s, a long frame) at the car's speed.
function M.cutReach(sp, fresh, lag)
  return math.max(fresh and M.CUT_FRESH_M or math.max(M.CUT_M, sp * M.CUT_S), M.stopReach(sp)) + (lag or 0) * sp
end

-- Pure: is a car at (px, py) within m of one of the cuts, whichever way it points?
function M.nearCut(px, py, list, m)
  for _, c in ipairs(list) do
    if (c[1] - px) ^ 2 + (c[2] - py) ^ 2 <= m * m then return true end
  end
  return false
end

-- Pure: has a car near a cut stopped at the dead end or begun its turn (CUT_NEAR_MS, CUT_TURN_COS)? sp its speed
-- (m/s), fwd its velocity along its facing (negative: reversing), roadDot the cosine of its direction of travel
-- (at a stand: its facing) with its road's in -> out direction (nil: no road found), oneWay that road's, facingCut
-- whether its facing points at a cut (headingToCut's cone). Merely slow or stopped is not enough: a red light or a
-- stop sign on a through road of a junction the clip cut holds a car just there, aligned with its road and facing
-- past the dead end -- left alone.
function M.cutStopped(sp, fwd, roadDot, oneWay, facingCut)
  local turned = roadDot ~= nil
    and (math.abs(roadDot) < M.CUT_TURN_COS or (oneWay and roadDot < -M.CUT_TURN_COS)) or false
  if sp < M.CUT_NEAR_MS then
    return facingCut or turned                          -- stopped at the dead end, or stood across its road
  end
  return fwd < 0 or turned                              -- backing, across the road, or against a one-way road
end

-- Pure: is a car at (px, py) moving along unit (dx, dy) heading for one of the cuts within reach m (45 degrees)?
function M.headingToCut(px, py, dx, dy, list, reach)
  for _, c in ipairs(list) do
    local vx, vy = c[1] - px, c[2] - py
    local d = math.sqrt(vx * vx + vy * vy)
    if d <= reach and (d < 1 or (vx * dx + vy * dy) >= 0.7071 * d) then return true end
  end
  return false
end

function M.onMessage(msg, currentAnchor)
  if not M.enabled or msg.anchorId ~= currentAnchor then return false end
  local ok, g = pcall(jsonDecode, readFile(msg.path) or '')
  if not ok or type(g) ~= 'table' or type(g.nodes) ~= 'table' or type(g.edges) ~= 'table' then return false end
  graph = { anchor = msg.anchorId, serial = msg.serial, nodes = g.nodes, edges = g.edges,
            signals = type(g.signals) == 'table' and g.signals or {}, rules = type(g.rules) == 'table' and g.rules or nil,
            lines = type(g.lines) == 'table' and g.lines or nil,      -- (tunnel stretches: the minimap's, minimaproads)
            trafficProfile = type(g.trafficProfile) == 'table' and g.trafficProfile or nil,  -- (M.ADAPTIVE)
            mph = g.mph }       -- (the country's speed signs: api.speedLimit)
  dirty = true
  return true
end

-- Traffic signals and stop signs (core_trafficSignals, research sec.5): the server's controlled junctions whose
-- node and approach nodes are in the clipped graph. A signalled junction is one sequence of two phases (its
-- approaches split by axis, US lightsBasic timings from the approach speed), a stop junction shares one signStop
-- controller; an instance's pos is the stop point on its approach's right half, dir the travel direction.
M.SIGNAL_TYPE = 'lightsBasic'
-- the switchable head (the stock boom, instanced_trafficlight material): one TSStatic per approach where the
-- road tile's static boom stands (trees.lua's signal group is hidden while these exist)
M.SIGNAL_SHAPE = '/art/shapes/objects/s_trafficlight_boom_sn.dae'
local heads = {}        -- instance name -> {id, x, y}: the heads in the scene (kept across applies)
local seenHeads = {}

local function clearHeads(keep)
  for name, h in pairs(heads) do
    if not (keep and keep[name]) then
      local o = scenetree.findObjectById(h.id)
      if o then o:delete() end
      heads[name] = nil
    end
  end
end

-- A head for instance inst at a's prop transform: the one already standing there is kept (it is linked by
-- the instance's name, which stays the same: setupSignals finds it again), so a new graph costs only the new ones.
local function makeHead(inst, a)
  if not a[8] or not inst.createSignalObject then return end
  local shape = M.SIGNAL_SHAPE
  if a[12] == 1 then
    local left = require('beamngEarth/signalshape').ensureLeft()
    if not left then return end
    shape = left .. '.dae'
  end
  seenHeads[inst.name] = true
  local h = heads[inst.name]
  if h and h.shape == shape and math.abs(h.x - a[8]) < 0.05 and math.abs(h.y - a[9]) < 0.05
    and scenetree.findObjectById(h.id) then return end
  if h then
    local old = scenetree.findObjectById(h.id)
    if old then old:delete() end
  end
  local o = inst:createSignalObject(shape, vec3(a[8], a[9], a[10]), quat(0, 0, 0, 1))
  if not o then return end
  local m = MatrixF(true)
  m:setFromEuler(vec3(0, 0, -math.rad(a[11] or 0)))     -- as trees.lua turns the static boom (negated: clockwise)
  m:setPosition(vec3(a[8], a[9], a[10]))
  o:setTransform(m)
  heads[inst.name] = { id = o:getID(), x = a[8], y = a[9], shape = shape }
end

local function signalData(nodes)
  local ts = core_trafficSignals
  local inst, ctrls, seqs = {}, {}, {}
  local stop = nil
  local ns = 0
  for _, j in ipairs(graph.signals or {}) do
    local jn = nodes[j.j]
    if jn and type(j.app) == 'table' then
      local apps = {}
      for _, a in ipairs(j.app) do if nodes[a[1]] then apps[#apps + 1] = a end end
      if #apps > 0 then
        ns = ns + 1
        local cs = {}
        if j.kind == 'signal' then
          local speed = 13
          for _, l in pairs(jn.links) do speed = math.max(speed, l.speedLimit or 0) end
          for ph = 0, 1 do
            local c = ts.newController({ name = 'bne_' .. j.j .. '_' .. ph })
            c:applyDefinition(M.SIGNAL_TYPE)
            if c.autoSetTimings then c:autoSetTimings(speed, 2 * jn.radius + 6) end
            ctrls[#ctrls + 1] = c
            cs[ph] = c
          end
          local seq = ts.newSequence({ name = 'bne_' .. j.j, phases = { { controllerIds = { cs[0].id } },
            { controllerIds = { cs[1].id } } } })
          seqs[#seqs + 1] = seq
          for i, a in ipairs(apps) do
            local s = ts.newSignal({ name = 'bne_' .. j.j .. '_' .. i, pos = vec3(a[2], a[3], a[4]),
              dir = vec3(a[5], a[6], 0), controllerId = cs[a[7] == 1 and 1 or 0].id, sequenceId = seq.id })
            inst[#inst + 1] = s
            local okH, errH = pcall(makeHead, s, a)
            if not okH then log('W', 'beamngEarth', 'navgraph: signal head: ' .. tostring(errH)) end
          end
        else
          if not stop then
            stop = ts.newController({ name = 'bne_stop' })
            stop:applyDefinition('signStop')
            ctrls[#ctrls + 1] = stop
          end
          for i, a in ipairs(apps) do
            inst[#inst + 1] = ts.newSignal({ name = 'bne_' .. j.j .. '_' .. i, pos = vec3(a[2], a[3], a[4]),
              dir = vec3(a[5], a[6], 0), controllerId = stop.id })
          end
        end
      end
    end
  end
  return { instances = inst, controllers = ctrls, sequences = seqs }, ns
end

local function signalsSystem()
  if not core_trafficSignals and extensions and extensions.load then pcall(extensions.load, 'core_trafficSignals') end
  return core_trafficSignals
end

local function setRules()
  -- the country's rules (server navgraph.road_rules): driving side, turn on red, red+yellow before green
  local r = graph.rules
  if r then
    M.rules = { rightHandDrive = r.rightHandDrive and true or false, turnOnRed = r.turnOnRed and true or false }
    M.SIGNAL_TYPE = r.redYellow and 'lightsBasicEU' or 'lightsBasic'
  end
end

-- pre: toNodes' results built ahead (buildNodes), else made here
local function apply(b, pre, moved)
  setRules()
  local tN = wall()
  local nodes, nn, ne, cl
  if pre then nodes, nn, ne, cl = pre[1], pre[2], pre[3], pre[4]
  else nodes, nn, ne, cl = M.toNodes(graph, b, M.EDGE_M, M.rules.rightHandDrive, nil, M.HALO_M, M.FAST_HALO_M) end
  stats.nodesMs = wall() - tN         -- (the parts of an apply's frame: the hitch hunt, 2026-10-23)
  local ts = signalsSystem()
  -- before the reset: its onNavgraphReloaded would place the old instances on the new graph (research sec.2.8)
  if ts then pcall(ts.resetSignals) end
  writeFile(M.RULES_FILE, jsonEncode({ roadRules = M.rules }))
  local orig = path.getPathLevelInfo
  path.getPathLevelInfo = function() return M.RULES_FILE end
  -- the reset sends the whole map to the UI (NavigationMapChanged, research sec.2.3): only the big map uses it,
  -- and it is a good part of the reset's frame; dropped during ours (M.UI_MAP = true keeps it)
  local trig = guihooks and guihooks.trigger
  if trig and not M.UI_MAP then
    guihooks.trigger = function(name, ...) if name ~= 'NavigationMapChanged' then return trig(name, ...) end end
  end
  local okG, gps = pcall(require, 'beamngEarth/gps')
  local mOld = map.getMap and map.getMap()
  local tG = wall()
  local keep = okG and gps.beforeGraph and gps.beforeGraph(mOld and mOld.nodes) or nil
  stats.gpsMs = wall() - tG
  local t0 = wall()
  ours = true
  local ok, err = pcall(map.reset, nodes)
  ours = false
  if trig then guihooks.trigger = trig end
  path.getPathLevelInfo = orig
  if not ok then
    log('E', 'beamngEarth', 'navgraph: map.reset: ' .. tostring(err))
    return false
  end
  stats.resetMs = wall() - t0
  local m = map.getMap()
  if persistent and persistent ~= m.nodes then
    table.clear(persistent)
    for k, v in pairs(m.nodes) do persistent[k] = v end
    m.nodes = persistent
  else
    persistent = m.nodes
  end
  local tA = wall()
  if keep then pcall(gps.afterGraph, keep, m.nodes, moved) end    -- before the vehicles' new map
  stats.gpsMs = stats.gpsMs + (wall() - tA)
  be:queueAllObjectLua('mapmgr.requestMap()')
  -- the minimap (the navigation app, in 0.40's freeroam layout and ours) indexes the map's roads once and keeps them:
  -- it showed the first graph's streets, or none, as the player drove on (the user, 2026-10-22)
  local mr = rawget(_G, 'ui_apps_minimap_roads')
  if mr and mr.reset then pcall(mr.reset) end
  pendingSignals = ts and nodes or nil     -- next frame: a second, smaller hitch instead of one big one
  cuts = cl
  stats.applies, stats.lastMs, stats.nodes, stats.edges = stats.applies + 1, wall() - t0, nn, ne
  stats.applyMs = wall() - tN
  stats.cuts = #cl
  return true
end

-- The vehicle Lua that hands a vehicle the signals (a serialized table). A traffic car keeps the junction it
-- is approaching across map changes (ai.lua's trafficStates.intersection; stock maps never change) and
-- reads signalsData[node][nextNode][idx] without a check: a new graph without that junction (a teleport,
-- or the junction left the window) disabled every car near one (in game 2026-10-14, "attempt to index a
-- nil value" at ai.lua trafficActions). So the new table keeps the old entries it lacks: their nodes are
-- not on the new graph's paths (ai.lua's other reads check first), only a remembered junction resolves.
function M.setSignalsLua(data)
  return 'do local old, new = mapmgr.signalsData, ' .. data .. '\n'
    .. 'if old and new then for a, t in pairs(old) do local n = new[a] if not n then new[a] = t else '
    .. 'for b, u in pairs(t) do if n[b] == nil then n[b] = u end end end end end\n'
    .. 'mapmgr.setSignals(new) end'
end

-- The signals of the graph applied last frame (core_trafficSignals resolves them on the new map).
local function applySignals(nodes)
  local ts = core_trafficSignals
  if not ts then return end
  local t1 = wall()
  local okS, err2 = pcall(function()
    seenHeads = {}
    local data, ns = signalData(nodes)
    clearHeads(seenHeads)                   -- the heads of junctions no longer in the graph
    local t2 = wall()
    stats.sigDataMs = t2 - t1
    -- setupSignals finds the signal heads by walking every TSStatic in the level (setupSignalObjects:
    -- getObjectsByClass('TSStatic') and a dynamic-field read on each); ours holds thousands (buildings, roads, far
    -- tiles), so each apply paid for all of them. The heads are ours (heads): only they are handed over meanwhile.
    local gobc = rawget(_G, 'getObjectsByClass')
    if gobc and M.HEADS_ONLY then
      getObjectsByClass = function(cls)
        if cls ~= 'TSStatic' then return gobc(cls) end
        local out = {}
        for _, h in pairs(heads) do
          local o = scenetree.findObjectById(h.id)
          if o then out[#out + 1] = o end
        end
        return out
      end
    end
    local okSet, errSet = pcall(ts.setupSignals, data, false, true)
    if gobc then getObjectsByClass = gobc end
    if not okSet then error(errSet) end
    local t3 = wall()
    stats.sigSetupMs = t3 - t2
    -- a vehicle holding signals reads the state-change mailbox every frame and dies on one never written
    -- ("lpack: unexpected end of buffer", in game 2026-10-14): write an empty update before handing them out
    be:sendToMailbox('trafficSignalUpdates', lpack.encodeBin({}))
    be:queueAllObjectLua(M.setSignalsLua(serialize(ts.getMapNodeSignals())))
    local t4 = wall()
    stats.sigSendMs = t4 - t3
    stats.junctions = ns
    -- the road tiles' static booms stand where the switchable heads are now
    local trees = require('beamngEarth/trees')
    local grid = {}
    for _, h in pairs(heads) do grid[math.floor(h.x / 4) .. ',' .. math.floor(h.y / 4)] = true end
    trees.suppress = function(g, x, y)
      if g ~= 'signal' and g ~= 'signal_left' then return false end
      local cx, cy = math.floor(x / 4), math.floor(y / 4)
      for dx = -1, 1 do for dy = -1, 1 do if grid[(cx + dx) .. ',' .. (cy + dy)] then return true end end end
      return false
    end
    local t5 = wall()
    trees.refresh('signal')            -- (only the tiles with signal items: suppress hides nothing else)
    trees.refresh('signal_left')
    stats.sigTreesMs = wall() - t5
  end)
  stats.signalsMs = wall() - t1
  stats.signalsMaxMs = math.max(stats.signalsMaxMs or 0, stats.signalsMs)
  if not okS then log('W', 'beamngEarth', 'navgraph: signals: ' .. tostring(err2)) end
end

local function trafficSystem()
  if not (gameplay_traffic and core_multiSpawn and gameplay_walk) and extensions and extensions.load then
    pcall(extensions.load, unpack(M.EXTENSIONS))
  end
  return gameplay_traffic
end

local function playerId() return be and be.getPlayerVehicleID and be:getPlayerVehicleID(0) or -1 end

-- Pure: where a respawned car goes, (x, y, dirX, dirY, min m, max m) for gameplay_traffic.forceTeleport, or nil for
-- behind the camera: ahead of a player at (px, py) moving (vx, vy) m/s faster than AHEAD_MS, AHEAD_M along the way but
-- no farther than the block b's edge guard (GUARD_M + a margin) in that direction; nil too when that leaves no room.
function M.aheadSpot(px, py, vx, vy, b)
  local sp = math.sqrt(vx * vx + vy * vy)
  if sp < M.AHEAD_MS then return nil end
  local dx, dy = vx / sp, vy / sp
  local reach = math.huge
  if b then
    if dx > 1e-6 then reach = math.min(reach, (b[3] - px) / dx) elseif dx < -1e-6 then reach = math.min(reach, (b[1] - px) / dx) end
    if dy > 1e-6 then reach = math.min(reach, (b[4] - py) / dy) elseif dy < -1e-6 then reach = math.min(reach, (b[2] - py) / dy) end
  end
  local hi = math.min(M.AHEAD_M[2], reach - M.GUARD_M - 20)
  if hi < M.AHEAD_M[1] then return nil end
  return px, py, dx, dy, M.AHEAD_M[1], hi
end

local function forwardReach(px, py, ux, uy, b, margin)
  if not b then return math.huge end
  margin = margin or 0
  local reach = math.huge
  if ux > 1e-6 then reach = math.min(reach, (b[3] - margin - px) / ux)
  elseif ux < -1e-6 then reach = math.min(reach, (b[1] + margin - px) / ux) end
  if uy > 1e-6 then reach = math.min(reach, (b[4] - margin - py) / uy)
  elseif uy < -1e-6 then reach = math.min(reach, (b[2] + margin - py) / uy) end
  if reach ~= reach or reach < 0 then return 0 end
  return reach
end

-- Pure: how far ahead a fast player can place a car. nil when the player is slow or the
-- block / collision mesh has under 280 m of room. Otherwise a distance in AHEAD_M, longer
-- when the player is closing on the traffic ahead.
function M.horizonDistance(playerMps, carMps, boxReachM, collideReachM)
  playerMps = tonumber(playerMps)
  if not playerMps or playerMps < M.AHEAD_MS then return nil end
  local reach = math.min(boxReachM or math.huge, collideReachM or math.huge)
  if reach < 280 then return nil end
  local other = tonumber(carMps) or (playerMps * 0.85)
  local closing = math.max(0, playerMps - other)
  local want = math.max(M.AHEAD_M[1], math.min(M.AHEAD_M[2], M.AHEAD_M[1] + closing * 14.0))
  return math.min(want, reach)
end

-- Pure: lateral offset (m, +right of the in->out centreline) of travel lane `laneRank`.
-- Rank 1 is the outer travel lane. `rightHandDrive` is M.rules.rightHandDrive: true means
-- traffic keeps left. A lone cruiser uses rank 1, or rank 2 when three or more lanes exist.
function M.laneCenter(radius, lanes, along, laneRank, rightHandDrive)
  radius = tonumber(radius)
  laneRank = tonumber(laneRank) or 1
  if laneRank ~= laneRank then return nil end
  laneRank = math.floor(laneRank)
  if not radius or radius <= 0 or type(lanes) ~= 'string' or #lanes < 1 or laneRank < 1 then return nil end
  local alongWay = along ~= false
  local want = alongWay and '+' or '-'
  local idxs = {}
  for i = 1, #lanes do
    if lanes:sub(i, i) == want then idxs[#idxs + 1] = i end
  end
  local n = #idxs
  if n < 1 then return nil end
  local keepLeft = rightHandDrive == true
  local outerIsRightmost = (not keepLeft and alongWay) or (keepLeft and not alongWay)
  local order = {}
  if outerIsRightmost then
    for i = n, 1, -1 do order[#order + 1] = idxs[i] end
  else
    for i = 1, n do order[#order + 1] = idxs[i] end
  end
  local laneIndex = order[math.min(laneRank, n)]
  local w = 2 * radius / #lanes
  return -radius + (laneIndex - 0.5) * w
end

local function sampleXYZ(s)
  if type(s) ~= 'table' then return nil end
  local x = s.x or s[1]
  local y = s.y or s[2]
  local z = s.z or s[3] or 0
  if not (type(x) == 'number' and type(y) == 'number') then return nil end
  return x, y, z
end

-- Pure: true when the corridor bends at least 12 deg before 75% of its length, or an
-- intermediate sample crests 2.2 m above the line of sight from the player to the spot.
function M.isOccludedSpot(samples, playerX, playerY, playerZ, dirX, dirY)
  if type(samples) ~= 'table' or #samples < 2 then return false end
  playerX, playerY = tonumber(playerX), tonumber(playerY)
  playerZ = tonumber(playerZ) or 0
  dirX, dirY = tonumber(dirX) or 0, tonumber(dirY) or 0
  local dl = math.sqrt(dirX * dirX + dirY * dirY)
  if not playerX or not playerY or dl < 1e-6 then return false end
  dirX, dirY = dirX / dl, dirY / dl
  local sx, sy, sz = sampleXYZ(samples[#samples])
  if not sx then return false end
  local cum = { 0 }
  local total = 0
  for i = 2, #samples do
    local x1, y1 = sampleXYZ(samples[i - 1])
    local x2, y2 = sampleXYZ(samples[i])
    if not (x1 and x2) then return false end
    total = total + math.sqrt((x2 - x1) ^ 2 + (y2 - y1) ^ 2)
    cum[i] = total
  end
  local limit = total * 0.75
  for i = 2, #samples do
    if cum[i - 1] < limit then
      local x1, y1 = sampleXYZ(samples[i - 1])
      local x2, y2 = sampleXYZ(samples[i])
      local seg = math.sqrt((x2 - x1) ^ 2 + (y2 - y1) ^ 2)
      if seg > 1 then
        local dot = (x2 - x1) / seg * dirX + (y2 - y1) / seg * dirY
        if dot <= 0.978 then return true end
      end
    end
  end
  local chord2 = (sx - playerX) ^ 2 + (sy - playerY) ^ 2
  if chord2 < 1 then return false end
  for i = 2, #samples - 1 do
    local x, y, z = sampleXYZ(samples[i])
    local t = ((x - playerX) * (sx - playerX) + (y - playerY) * (sy - playerY)) / chord2
    if t < 0 then t = 0 elseif t > 1 then t = 1 end
    local chordZ = (playerZ + 1.2) + t * ((sz + 1.0) - (playerZ + 1.2))
    if z >= chordZ + 2.2 then return true end
  end
  return false
end

-- Pure: same-way cars strictly ahead of (px, py) out to maxAheadM and within halfBandM
-- to the side. Returns count, nearest ahead, farthest ahead.
function M.aheadCorridorState(cars, px, py, ux, uy, maxAheadM, halfBandM)
  px, py = tonumber(px), tonumber(py)
  ux, uy = tonumber(ux) or 0, tonumber(uy) or 0
  local ul = math.sqrt(ux * ux + uy * uy)
  if not px or not py or ul < 1e-6 then return 0, nil, nil end
  ux, uy = ux / ul, uy / ul
  maxAheadM = tonumber(maxAheadM) or 600
  halfBandM = tonumber(halfBandM) or 40
  local count, minA, maxA = 0, nil, nil
  for _, c in ipairs(cars or {}) do
    if type(c) == 'table' and tonumber(c.x) and tonumber(c.y) then
      local dx, dy = c.x - px, c.y - py
      local ahead = dx * ux + dy * uy
      local side = math.abs(-dx * uy + dy * ux)
      local same = true
      local vx, vy = tonumber(c.vx), tonumber(c.vy)
      if vx and vy then
        -- only a car that is actually moving the same way counts: parked and crawling cars do not fill the corridor
        local sp = math.sqrt(vx * vx + vy * vy)
        same = sp > 1.5 and (vx * ux + vy * uy) / sp > 0.5
      elseif tonumber(c.dx) and tonumber(c.dy) then
        same = c.dx * ux + c.dy * uy > 0.5
      end
      if same and ahead > 0 and ahead <= maxAheadM and side <= halfBandM then
        count = count + 1
        minA = minA and math.min(minA, ahead) or ahead
        maxA = maxA and math.max(maxA, ahead) or ahead
      end
    end
  end
  return count, minA, maxA
end

-- Pure: a cloaked pre-roll may fade in only once it is far enough ahead, the body is near
-- the target speed, the wheels are not slipping, and both brakes are released.
function M.uncloakReady(aheadM, minAheadM, bodyMps, targetMps, wheelMps, parkingBrake, brake)
  if minAheadM then
    aheadM, minAheadM = tonumber(aheadM), tonumber(minAheadM)
    if not aheadM or not minAheadM or aheadM < minAheadM then return false end
  end
  bodyMps, targetMps, wheelMps = tonumber(bodyMps), tonumber(targetMps), tonumber(wheelMps)
  if not bodyMps or not targetMps or not wheelMps then return false end
  if math.abs(bodyMps - targetMps) > math.max(2.5, 0.18 * targetMps) then return false end
  if math.abs(wheelMps - bodyMps) > 2.5 then return false end
  if (tonumber(parkingBrake) or 0) >= 0.05 then return false end
  if (tonumber(brake) or 0) >= 0.08 then return false end
  return true
end

local function edgeGeom(e)
  if type(e) ~= 'table' or type(e.inPos) ~= 'table' or type(e.outPos) ~= 'table' then return nil end
  local ix, iy, iz = e.inPos.x, e.inPos.y, e.inPos.z or 0
  local ox, oy, oz = e.outPos.x, e.outPos.y, e.outPos.z or 0
  if not (type(ix) == 'number' and type(iy) == 'number' and type(ox) == 'number' and type(oy) == 'number') then
    return nil
  end
  local ex, ey = ox - ix, oy - iy
  local el = math.sqrt(ex * ex + ey * ey)
  if el < 0.1 then return nil end
  return ix, iy, iz, ox, oy, oz, ex / el, ey / el, el
end

local function highwayEdge(e)
  if type(e) ~= 'table' then return false end
  return (tonumber(e.speedLimit) or 0) >= 18 or e.oneWay == true
end

local function sameWayCount(lanes, along)
  if type(lanes) ~= 'string' then return 0 end
  local want = along and '+' or '-'
  local n = 0
  for i = 1, #lanes do if lanes:sub(i, i) == want then n = n + 1 end end
  return n
end

local function travelRankOf(n)
  if n >= 3 then return 2 end
  return 1
end

-- Pure: a lane-centred pose on the highway ahead of the player, or nil when the corridor
-- is full, the block is too short, or every spot is blocked. Does not teleport; the guard
-- places the pose and cloaks the car. platoonSlot 0 is the lead. A later slot sits
-- PLATOON_GAP_M behind that lead, or in the next lane inward when a slower travel-lane
-- car is 18-32 m ahead of the spot.
function M.pickHorizonPose(nodes, px, py, pz, vx, vy, b, collideReachM, rightHandDrive, existingCars, platoonSlot)
  if type(nodes) ~= 'table' then return nil end
  px, py = tonumber(px), tonumber(py)
  pz = tonumber(pz) or 0
  vx, vy = tonumber(vx) or 0, tonumber(vy) or 0
  if not px or not py then return nil end
  local playerMps = math.sqrt(vx * vx + vy * vy)
  if playerMps < M.AHEAD_MS then return nil end
  local ux, uy = vx / playerMps, vy / playerMps
  local boxReach = forwardReach(px, py, ux, uy, b, M.GUARD_M + 25)
  collideReachM = tonumber(collideReachM)
  local target = M.horizonDistance(playerMps, nil, boxReach, collideReachM)
  if not target then return nil end
  local searchMax = math.min(boxReach, collideReachM or math.huge)
  if searchMax < 280 then return nil end
  local count = M.aheadCorridorState(existingCars, px, py, ux, uy, math.max(600, M.AHEAD_M[2] + 90), 40)
  if count >= M.AHEAD_FULL then return nil end
  local cars = existingCars or {}
  local slot = tonumber(platoonSlot) or 0
  if slot < 0 then slot = 0 end

  local bad = {}
  for _, nd in pairs(nodes) do
    if type(nd) == 'table' and type(nd.links) == 'table' and nd.pos then
      local nLinks, highwayOnly = 0, true
      for _, e in pairs(nd.links) do
        nLinks = nLinks + 1
        if not ((tonumber(e.speedLimit) or 0) >= 18 and e.oneWay == true) then highwayOnly = false end
      end
      if nLinks >= 3 and not highwayOnly then
        bad[#bad + 1] = { x = nd.pos.x, y = nd.pos.y }
      end
    end
  end

  local seen, start, startD = {}, nil, 30 * 30
  for _, nd in pairs(nodes) do
    if type(nd) == 'table' and type(nd.links) == 'table' then
      for _, e in pairs(nd.links) do
        if not seen[e] and highwayEdge(e) then
          seen[e] = true
          local ix, iy, iz, ox, oy, oz, ex, ey, el = edgeGeom(e)
          if ix then
            local t = math.max(0, math.min(1, ((px - ix) * ex + (py - iy) * ey) / el))
            local qx = ix + ex * el * t
            local qy = iy + ey * el * t
            local qz = iz + (oz - iz) * t
            local d2 = (px - qx) ^ 2 + (py - qy) ^ 2
            local dot = ex * ux + ey * uy
            local towardOut = dot >= 0
            if not towardOut then dot = -dot end
            if not (e.oneWay and not towardOut) and dot > 0.7 and d2 < startD then
              startD = d2
              start = { e = e, x = qx, y = qy, z = qz, towardOut = towardOut }
            end
          end
        end
      end
    end
  end
  if not start then return nil end

  local poly = { { x = start.x, y = start.y, z = start.z, dist = 0 } }
  local line = {}
  local raw = {}
  -- Engine surface under a point 2 m above it: the stand-in for a planner terrain Z (nodes carry none in the live
  -- game). Nil when there is no engine, no surface below (the engine's -1e10 sentinel), or the query fails.
  local function surfaceBelow(x, y, z)
    local beObj = rawget(_G, 'be')
    if not (beObj and type(beObj.getSurfaceHeightBelow) == 'function' and type(vec3) == 'function') then return nil end
    local ok, h = pcall(beObj.getSurfaceHeightBelow, beObj, vec3(x, y, z + 2.0))
    h = ok and tonumber(h) or nil
    if h and h == h and h > -1e5 and h < 1e5 then return h end
    return nil
  end
  local function terrainAt(e, t, x, y, z, dist)
    local a, bnode = nodes[e.inNode], nodes[e.outNode]
    local za = a and tonumber(a.terrainZ or a.groundZ)
    local zb = bnode and tonumber(bnode.terrainZ or bnode.groundZ)
    if not za and not zb then
      -- only spots near or past the collision reach are ever checked against it (see emit), so only those ask the engine
      if collideReachM and dist > collideReachM - 25 then return surfaceBelow(x, y, z) end
      return nil
    end
    za, zb = za or zb, zb or za
    return za + (zb - za) * t
  end
  local function pushRaw(cx, cy, cz, dist, iox, ioy, alongOut, e, onRamp, extra)
    if dist < 260 or dist > searchMax then return nil end
    if type(e.lanes) ~= 'string' then return nil end
    local nLanes = sameWayCount(e.lanes, alongOut)
    if nLanes < 1 then return nil end
    local ix, iy, iz, ox, oy, oz, ex, ey, el = edgeGeom(e)
    if not ix then return nil end
    local t = math.max(0, math.min(1, ((cx - ix) * ex + (cy - iy) * ey) / el))
    local radius = (tonumber(e.inRadius) or 0) + ((tonumber(e.outRadius) or tonumber(e.inRadius) or 0) - (tonumber(e.inRadius) or 0)) * t
    if radius <= 0.5 then
      local nd = nodes[e.inNode]
      radius = nd and tonumber(nd.radius) or 0
    end
    if radius <= 0.5 then return nil end
    local samples = {}
    for i = 1, #poly do
      if poly[i].dist <= dist + 0.5 then samples[#samples + 1] = poly[i] end
    end
    samples[#samples + 1] = extra or { x = cx, y = cy, z = cz, dist = dist }
    local hx, hy = iox, ioy
    if not alongOut then hx, hy = -iox, -ioy end
    raw[#raw + 1] = {
      cx = cx, cy = cy, cz = cz, dist = dist, iox = iox, ioy = ioy, hx = hx, hy = hy,
      alongOut = alongOut, lanes = e.lanes, radius = radius, nLanes = nLanes,
      travelRank = travelRankOf(nLanes), onRamp = onRamp and true or false,
      occluded = M.isOccludedSpot(samples, px, py, pz, ux, uy),
      terrainZ = terrainAt(e, t, cx, cy, cz, dist),
    }
    return raw[#raw]
  end

  local edge, towardOut = start.e, start.towardOut
  local cx, cy, cz = start.x, start.y, start.z
  local travelled, guard, visited = 0, 0, {}
  while edge and travelled < searchMax + 5 and guard < 48 do
    guard = guard + 1
    visited[edge] = true
    local ix, iy, iz, ox, oy, oz, ex, ey, el = edgeGeom(edge)
    if not ix then break end
    local destx, desty, destz, dirx, diry
    if towardOut then
      destx, desty, destz, dirx, diry = ox, oy, oz, ex, ey
    else
      destx, desty, destz, dirx, diry = ix, iy, iz, -ex, -ey
    end
    local remain = math.sqrt((destx - cx) ^ 2 + (desty - cy) ^ 2)
    local base = travelled
    local walked = 0
    while walked + 0.05 < remain and base + walked < searchMax + 20 do
      local step = math.min(20, remain - walked)
      walked = walked + step
      local u = walked / math.max(remain, 0.001)
      local x = cx + (destx - cx) * u
      local y = cy + (desty - cy) * u
      local z = cz + (destz - cz) * u
      local dist = base + walked
      poly[#poly + 1] = { x = x, y = y, z = z, dist = dist }
      line[#line + 1] = {
        x = x, y = y, z = z, dist = dist, iox = ex, ioy = ey, alongOut = towardOut, e = edge,
      }
      if dist >= 280 and dist <= searchMax then
        pushRaw(x, y, z, dist, ex, ey, towardOut, edge, false)
      end
    end
    travelled = base + remain
    local destName = towardOut and edge.outNode or edge.inNode
    local nd = destName and nodes[destName]
    if nd and type(nd.links) == 'table' and travelled >= 260 and travelled <= searchMax + 40 then
      for _, e2 in pairs(nd.links) do
        if e2 ~= edge and highwayEdge(e2) and e2.outNode == destName then
          local rix, riy, riz, rox, roy, roz, rex, rey, rel = edgeGeom(e2)
          if rix and rel > 18 then
            local back = math.min(30, rel * 0.45)
            local x = rox - rex * back
            local y = roy - rey * back
            local z = roz - (roz - riz) * (back / rel)
            pushRaw(x, y, z, travelled - back, rex, rey, true, e2, true, { x = x, y = y, z = z })
          end
        end
      end
    end
    local nextE, nextDot, nextToward = nil, 0.55, true
    if nd and type(nd.links) == 'table' then
      for _, e2 in pairs(nd.links) do
        if e2 ~= edge and not visited[e2] and highwayEdge(e2) then
          local nix, niy, niz, nox, noy, noz, nex, ney = edgeGeom(e2)
          if nix then
            local toward = e2.inNode == destName
            local hx, hy = nex, ney
            if not toward then hx, hy = -nex, -ney end
            local dot = hx * ux + hy * uy
            if dot > nextDot then nextE, nextDot, nextToward = e2, dot, toward end
          end
        end
      end
    end
    cx, cy, cz = destx, desty, destz
    edge, towardOut = nextE, nextToward
  end

  if slot >= 1 then
    for idx, car in ipairs(cars) do
      local vxC, vyC = tonumber(car.vx) or 0, tonumber(car.vy) or 0
      local sp = math.sqrt(vxC * vxC + vyC * vyC)
      if sp < playerMps * 0.9 and tonumber(car.x) and tonumber(car.y) then
        local ahead = (car.x - px) * ux + (car.y - py) * uy
        local want = ahead - 25
        if want >= 260 and want <= searchMax and (ahead - want) >= 18 and (ahead - want) <= 32 then
          local nearest, bestGap = nil, 12
          for i = 1, #line do
            local gap = math.abs(line[i].dist - ahead)
            if gap < bestGap then nearest, bestGap = line[i], gap end
          end
          local before, after = nil, nil
          for i = 1, #line do
            local row = line[i]
            if row.dist <= want and (not before or row.dist > before.dist) then before = row end
            if row.dist >= want and (not after or row.dist < after.dist) then after = row end
          end
          local spot = before or after
          if before and after and after.dist > before.dist then
            local u = (want - before.dist) / (after.dist - before.dist)
            spot = {
              x = before.x + (after.x - before.x) * u,
              y = before.y + (after.y - before.y) * u,
              z = before.z + (after.z - before.z) * u,
              dist = want, iox = before.iox, ioy = before.ioy,
              alongOut = before.alongOut, e = before.e,
            }
          end
          if nearest and spot and nearest.e and spot.e then
            local lat = M.laneCenter(nearest.e.inRadius or spot.e.inRadius or 3, nearest.e.lanes, nearest.alongOut, travelRankOf(sameWayCount(nearest.e.lanes, nearest.alongOut)), rightHandDrive)
            if lat then
              local rx, ry = nearest.ioy, -nearest.iox
              local latCar = (car.x - nearest.x) * rx + (car.y - nearest.y) * ry
              if math.abs(latCar - lat) <= 2.5 then
                local nLanes = sameWayCount(spot.e.lanes, spot.alongOut)
                local inner = travelRankOf(nLanes) + 1
                if inner <= nLanes then
                  local rec = pushRaw(spot.x, spot.y, spot.z, spot.dist, spot.iox, spot.ioy, spot.alongOut, spot.e, false)
                  if rec then
                    rec.pair = true
                    rec.exempt = idx
                    rec.forceRank = inner
                  end
                end
              end
            end
          end
        end
      end
    end
  end

  local function blocked(x, y, dist, exempt, minClearGap)
    if b and not M.inside(b, x, y, M.GUARD_M + 25) then return true end
    local min2d = exempt and (22 * 22) or (48 * 48)
    local gapM = minClearGap or M.CLEAR_GAP_M
    for i, car in ipairs(cars) do
      if tonumber(car.x) and tonumber(car.y) then
        local dx, dy = x - car.x, y - car.y
        if dx * dx + dy * dy < min2d then return true end
        if i ~= exempt then
          if dx * dx + dy * dy < 48 * 48 then return true end
          local ahead = (car.x - px) * ux + (car.y - py) * uy
          local side = math.abs(-(car.x - px) * uy + (car.y - py) * ux)
          if side <= 40 and math.abs(ahead - dist) < gapM then return true end
        end
      end
    end
    for i = 1, #bad do
      if (x - bad[i].x) ^ 2 + (y - bad[i].y) ^ 2 < M.LAUNCH_JUNCTION_M * M.LAUNCH_JUNCTION_M then
        return true
      end
    end
    return false
  end

  local function emit(c, rank, exempt)
    local lat = M.laneCenter(c.radius, c.lanes, c.alongOut, rank, rightHandDrive)
    if not lat then return nil end
    local rx, ry = c.ioy, -c.iox
    local x, y = c.cx + rx * lat, c.cy + ry * lat
    if c.terrainZ and collideReachM and c.dist > collideReachM - 25 and (c.cz - c.terrainZ) > 2.0 then
      return nil
    end
    if blocked(x, y, c.dist, exempt) then return nil end
    local bonus = (c.onRamp and 800 or 0) + (c.occluded and 400 or 0) + (c.pair and 250 or 0)
    return {
      x = x, y = y, z = c.cz, dx = c.hx, dy = c.hy, dist = c.dist,
      laneRank = rank, lateral = lat, occluded = c.occluded, onRamp = c.onRamp,
      score = -math.abs(c.dist - target) + bonus,
    }
  end

  local best = nil
  for i = 1, #raw do
    local c = raw[i]
    if slot >= 1 and c.pair then
      local pose = emit(c, c.forceRank or (c.travelRank + 1), c.exempt)
      if pose and (not best or pose.score > best.score) then best = pose end
    elseif slot < 1 and not c.pair then
      local pose = emit(c, c.travelRank, nil)
      if pose and (not best or pose.score > best.score) then best = pose end
    end
  end
  if slot >= 1 then
    -- A same-lane follower one platoon gap behind the best plain lane spot; it competes with a paired inner spot
    -- (higher score wins) or stands alone when no paired spot exists.
    local lead = nil
    for i = 1, #raw do
      local c = raw[i]
      if not c.pair then
        local pose = emit(c, c.travelRank, nil)
        if pose and (not lead or pose.score > lead.score) then lead = pose end
      end
    end
    if lead then
      local back = slot * M.PLATOON_GAP_M
      local dist = lead.dist - back
      if dist >= 260 then
        local pose = {
          x = lead.x - lead.dx * back, y = lead.y - lead.dy * back, z = lead.z,
          dx = lead.dx, dy = lead.dy, dist = dist, laneRank = lead.laneRank,
          lateral = lead.lateral, occluded = lead.occluded, onRamp = lead.onRamp,
          score = -math.abs(dist - target),
        }
        if not blocked(pose.x, pose.y, pose.dist, nil) and (not best or pose.score > best.score) then best = pose end
      end
    end
  end
  if not best then return nil end
  best.score = nil
  return best
end

local function collideReachOf(speed)
  local roads = package.loaded['beamngEarth/roads']
  if not (roads and type(roads.collideRadius) == 'function') then return nil end
  local ok, r = pcall(roads.collideRadius, speed)
  if ok then return tonumber(r) end
end

local function carsAround(skipId)
  local cars = {}
  local objs = rawget(_G, 'map') and map.objects or nil
  if not objs then return cars end
  for oid, mo in pairs(objs) do
    if oid ~= skipId and type(mo) == 'table' and mo.pos then
      cars[#cars + 1] = {
        x = mo.pos.x, y = mo.pos.y,
        vx = mo.vel and mo.vel.x or 0, vy = mo.vel and mo.vel.y or 0,
      }
    end
  end
  return cars
end

local function cloakPreRoll(t, o)
  if type(t) == 'table' then
    t.alpha = 0
    t.state = 'preRoll'
  end
  if o and o.setMeshAlpha then pcall(o.setMeshAlpha, o, 0, '') end
end

local nextPlatoonSlot, lastPlatoonAt = 0, -math.huge

-- Place one ahead recycle on the horizon pose and cloak it. Returns false when the
-- planner declines, so the caller sends the car behind the camera instead.
local function placeAhead(id, o, traffic, pp, pu, b, now)
  if not (o and spawn and type(spawn.safeTeleport) == 'function' and type(quatFromDir) == 'function'
      and type(vec3) == 'function' and pp and pu) then
    return false
  end
  local m = map and map.getMap and map.getMap()
  if not (m and m.nodes) then return false end
  local sp = math.sqrt(pu.x * pu.x + pu.y * pu.y)
  if sp < M.AHEAD_MS then return false end
  local slot = (now - lastPlatoonAt <= 5.0 and nextPlatoonSlot > 0 and nextPlatoonSlot <= 2) and nextPlatoonSlot or 0
  local around = carsAround(id)
  local reach = collideReachOf(sp)
  local rhd = M.rules.rightHandDrive == true
  local pose = M.pickHorizonPose(m.nodes, pp.x, pp.y, pp.z or 0, pu.x, pu.y, b, reach, rhd, around, slot)
  if not pose and slot > 0 then
    slot = 0
    pose = M.pickHorizonPose(m.nodes, pp.x, pp.y, pp.z or 0, pu.x, pu.y, b, reach, rhd, around, 0)
  end
  if not pose then return false end
  local ok = pcall(spawn.safeTeleport, o, vec3(pose.x, pose.y, pose.z),
    quatFromDir(vec3(pose.dx, pose.dy, 0), vec3(0, 0, 1)), true, nil, false, false, true)
  if not ok then return false end
  nextPlatoonSlot = (slot + 1) % 3
  lastPlatoonAt = now
  if type(traffic) == 'table' then
    if type(traffic.onRefresh) == 'function' then pcall(traffic.onRefresh, traffic) end
    traffic.respawnCount = (tonumber(traffic.respawnCount) or 0) + 1
    traffic.alpha = 0
    traffic.state = 'preRoll'
    launchCount[id] = traffic.respawnCount
  end
  if o.setMeshAlpha then pcall(o.setMeshAlpha, o, 0, '') end
  launchWait[id] = {
    phase = 'prep', nextAt = now + 0.06, expires = now + 0.2 + M.CLOAK_MAX_S,
    minAheadM = M.UNCLOAK_MIN_M, exactPose = true, target = nil,
  }
  return true
end


-- Pure but for closest: a spot on the other carriageway ahead of a player at (px, py) heading (dx, dy) (unit), inside
-- box b by GUARD_M + 20, for a car driving towards the player: x, y, dirX, dirY, or nil. closest(x, y) -> {dist, dx, dy,
-- oneWay} of the nearest road (its edge's direction in -> out) or nil. leftHand: traffic on the left (the oncoming side
-- is then to the right).
function M.oncomingSpot(px, py, dx, dy, b, leftHand, closest, maxAheadM)
  local sx, sy = -dy, dx                 -- (left of the heading)
  if leftHand then sx, sy = dy, -dx end
  maxAheadM = tonumber(maxAheadM)
  for _, ahead in ipairs(M.ONCOMING_M) do
    if not maxAheadM or ahead <= maxAheadM then
      for _, side in ipairs(M.ONCOMING_SIDE_M) do
        local x, y = px + dx * ahead + sx * side, py + dy * ahead + sy * side
        if M.inside(b, x, y, M.GUARD_M + 20) then
          local r = closest(x, y)
          if r and r.oneWay and r.dist <= M.ONCOMING_ON_M and (r.dx * dx + r.dy * dy) < -0.8 then
            return x, y, r.dx, r.dy
          end
        end
      end
    end
  end
  return nil
end

local function closestRoad(x, y, z)
  local m = map.getMap()
  local n1, n2 = map.findClosestRoad(vec3(x, y, z or 0))
  local a, c = n1 and m.nodes[n1], n2 and m.nodes[n2]
  local e = a and c and (a.links[n2] or c.links[n1])
  if not e then return nil end
  local i, o = m.nodes[e.inNode or n1], m.nodes[e.outNode or n2]
  if not i or not o then return nil end
  local ex, ey = o.pos.x - i.pos.x, o.pos.y - i.pos.y
  local l = math.sqrt(ex * ex + ey * ey)
  if l < 0.1 then return nil end
  -- (findClosestRoad's distance is 3D, the probe at the player's height: measured in the plane here)
  local t = math.max(0, math.min(1, ((x - i.pos.x) * ex + (y - i.pos.y) * ey) / (l * l)))
  local qx, qy = i.pos.x + t * ex, i.pos.y + t * ey
  return { dist = math.sqrt((x - qx) ^ 2 + (y - qy) ^ 2), dx = ex / l, dy = ey / l, oneWay = e.oneWay,
    speedLimit = e.speedLimit, limit = e.speedLimit, posted = e.posted, rough = e.rough == true, drivability = e.drivability,
    lateral = (ex * (y - qy) - ey * (x - qx)) / l, halfWidth = (e.inRadius or 3.5) +
      ((e.outRadius or e.inRadius or 3.5) - (e.inRadius or 3.5)) * t, rumble = e.rumble == true,
    edgeKey = tostring(e.inNode or n1) .. ':' .. tostring(e.outNode or n2) }
end

-- Local road speed metadata for traffic drivers. The graph is the current streamed graph; a generous distance gate
-- avoids applying an adjacent road's speed profile when the vehicle is off-road or the graph is still rebuilding.
function M.speedProfileAt(x, y, z)
  local r = closestRoad(x, y, z)
  if not r or r.dist > 35 then return nil end
  return { target = r.speedLimit, posted = r.posted, rough = r.rough, distance = r.dist, drivability = r.drivability }
end

-- Lane-departure strip metadata for the rumble audio. The server only marks selected country/road classes; only the
-- centerline of an undivided carriageway and its outer edges are candidates (never same-direction lane separators).
function M.rumbleLineAt(x, y, z)
  local r = closestRoad(x, y, z)
  if not r or not r.rumble or r.dist > 20 or r.halfWidth < 2 then return nil end
  return { lateral = r.lateral, halfWidth = r.halfWidth, oneWay = r.oneWay, edgeKey = r.edgeKey }
end
local oncomingTurn = false
local junctionNodes, junctionOf = nil, nil   -- (the graph's nodes where 3+ roads meet: positions, per graph table)

local function nearJunction(x, y, radius)
  if not graph then return false end
  if junctionOf ~= graph then
    local deg = {}
    for _, e in ipairs(graph.edges) do deg[e[1]], deg[e[2]] = (deg[e[1]] or 0) + 1, (deg[e[2]] or 0) + 1 end
    junctionNodes, junctionOf = {}, graph
    for name, n in pairs(graph.nodes) do
      if (deg[name] or 0) > 2 then junctionNodes[#junctionNodes + 1] = n end
    end
  end
  local r2 = (radius or M.LAUNCH_JUNCTION_M) ^ 2
  for _, n in ipairs(junctionNodes) do
    if (x - n[1]) ^ 2 + (y - n[2]) ^ 2 < r2 then return true end
  end
  return false
end

-- Pure: is a car that has stood still since `since` (nil: moving) at distance d from the player stuck at `now`?
function M.stuck(since, now, d)
  return since ~= nil and now - since >= M.STUCK_S and d >= M.STUCK_FAR_M
end

-- Pure: should an AI car on a fast road (limit >= HIGHWAY_CLEAR_MS, away from junctions) be recycled once the player
-- is well past it and it is out of sight? Wrecks and stalls in front of or visible to the player are never deleted:
-- recycling waits until behind the camera (aheadM < -40 and not visible).
function M.highwayHazard(damage, speed, limit, nearJunc, since, now, dist, camVisible, aheadM)
  if not since or (dist or math.huge) < M.HAZARD_MIN_DIST_M then return false end
  if camVisible == true or (tonumber(aheadM) or 0) > -40 then return false end
  if (tonumber(limit) or 0) < M.HIGHWAY_CLEAR_MS or nearJunc then return false end
  local sp = tonumber(speed) or 0
  if (tonumber(damage) or 0) >= M.WRECK_DAMAGE and sp < 8 then
    return now - since >= M.WRECK_CLEAR_S
  end
  if sp < 2.0 then
    return now - since >= M.HIGHWAY_STOP_S
  end
  return false
end

M.SIGHT_SHIELD_S = 0.10
M.SIGHT_SHIELD_MIN_M = 450

-- Pure: minimum activeRadius (m) to hold on any AI car (including wrecks) that is visible or in the forward corridor
-- inside the terrain block, so stock gameplay/traffic/vehicle.lua tryRespawn() cannot fade or delete a car or wreck in
-- front of the player; nil once behind the camera and out of sight (or reaching the block's guard edge).
function M.shieldInSight(camVisible, aheadM, sideM, b, x, y, _damage)
  if not M.inside(b, x, y, M.GUARD_M + 15) then return nil end
  local ahead = tonumber(aheadM) or -math.huge
  local side = math.abs(tonumber(sideM) or 0)
  if camVisible == true or (ahead > -10 and ahead < M.AHEAD_M[2] + 60 and side < 90) then
    return math.max(M.SIGHT_SHIELD_MIN_M, ahead + 200)
  end
  return nil
end

-- Pure: raise a traffic car's stock respawn radii to `need` (never lowering one that is already larger) and return the
-- record needed to hand them back; with need == nil, restore only the radii this shield set and that stock has not
-- changed since, and return nil. `saved` is the record from the previous call for the same car.
function M.shieldApply(respawn, saved, need)
  if type(respawn) ~= 'table' then return nil end
  if need then
    local active, final = tonumber(respawn.activeRadius) or 0, tonumber(respawn.finalRadius) or 0
    if active < need or final < need then
      saved = saved or { active = respawn.activeRadius, final = respawn.finalRadius }
      if active < need then respawn.activeRadius, saved.setActive = need, need end
      if final < need then respawn.finalRadius, saved.setFinal = need, need end
    end
    return saved
  end
  if saved then
    if saved.setActive and respawn.activeRadius == saved.setActive then respawn.activeRadius = saved.active end
    if saved.setFinal and respawn.finalRadius == saved.setFinal then respawn.finalRadius = saved.final end
  end
  return nil
end

-- Pure: evaluate evalFn(id) at most once per ttl seconds per car (cache: id -> { at, v }, reused in place).
function M.cachedConflict(cache, id, now, ttl, evalFn)
  local c = cache[id]
  if c and now - c.at < ttl then return c.v end
  local v = evalFn(id) and true or false
  if c then c.at, c.v = now, v else cache[id] = { at = now, v = v } end
  return v
end

-- Pure: with FAST_HALO_M > 0, a flowing car on a fast road (limit >= FAST_HALO_MS) has route runway well past the
-- phys cut, so it will not brake or U-turn there. This exempts toCut only. cutStopped / atCut still recycle a car
-- that has actually stopped or turned at a cut, including the outer graph dead-end once it is no longer flowing.
function M.skipFastHaloCut(roadLimit, carSpeed, b, x, y)
  if (M.FAST_HALO_M or 0) <= 0 then return false end
  if (tonumber(roadLimit) or 0) < M.FAST_HALO_MS or (tonumber(carSpeed) or 0) < 8.0 then return false end
  return M.inside(b, x, y, M.GUARD_M + 25)
end

-- Pure: choose which active AI vehicle id to deactivate first when adaptive traffic lowers activeAmount by 1.
-- Picks the farthest out-of-sight car behind the camera; returns nil when every car is visible or ahead so the
-- step-down waits rather than popping a car out of existence in front of the player.
function M.pickAdaptiveDrop(candidates)
  local bestId, bestAhead = nil, math.huge
  for _, c in ipairs(candidates or {}) do
    local ahead = tonumber(c.ahead) or 0
    if not c.camVisible and ahead < -20 and ahead < bestAhead then
      bestId, bestAhead = c.id, ahead
    end
  end
  return bestId
end

local lastSightShield = -math.huge
local shielded = {}   -- car id -> record from M.shieldApply, for the cars whose stock respawn radii we raised

local function roadAt(x, y, z)
  local ok, got = pcall(closestRoad, x, y, z)
  if ok then return got end
end

local function camAhead(p, fallback)
  local cp = core_camera and core_camera.getPosition and core_camera.getPosition() or fallback
  local cf = core_camera and core_camera.getForward and core_camera.getForward() or nil
  if not (cp and cf) then return 0 end
  local fl = math.sqrt(cf.x * cf.x + cf.y * cf.y)
  if fl <= 1e-6 then return 0 end
  return ((p.x - cp.x) * cf.x + (p.y - cp.y) * cf.y) / fl
end

local function shieldVisibleTraffic(now, b)
  if now - lastSightShield < M.SIGHT_SHIELD_S then return end
  lastSightShield = now
  local gt = gameplay_traffic
  if not (gt and gt.getState and gt.getState() == 'on' and gt.getTrafficData) then return end
  local cp = core_camera and core_camera.getPosition and core_camera.getPosition() or nil
  local cf = core_camera and core_camera.getForward and core_camera.getForward() or nil
  local me = playerId()
  local pv = me >= 0 and getObjectByID(me) or nil
  if not cp and pv then cp = pv:getPosition() end
  if not cf and pv then
    local v = pv:getVelocity()
    cf = v:length() > 1 and v:normalized() or pv:getDirectionVector()
  end
  if not (cp and cf) then return end
  local fx, fy = cf.x, cf.y
  local fl = math.sqrt(fx * fx + fy * fy)
  if fl < 1e-6 then return end
  fx, fy = fx / fl, fy / fl
  local mapObjs = rawget(_G, 'map') and map.objects or nil
  localplayers.snapshot(controlled)
  local data = gt.getTrafficData() or {}
  for id, t in pairs(data) do
    local o = getObjectByID(id)
    if type(t) == 'table' and t.isAi ~= false and o and o:getActive() and not localplayers.controlled(id, o, controlled) then
      local p = o:getPosition()
      local dx, dy = p.x - cp.x, p.y - cp.y
      local ahead = dx * fx + dy * fy
      local side = -dx * fy + dy * fx
      local dmg = (mapObjs and mapObjs[id] and tonumber(mapObjs[id].damage)) or tonumber(t.damage) or 0
      local r = M.shieldInSight(t.camVisible, ahead, side, b, p.x, p.y, dmg)
      if r and type(t.respawn) == 'table' then
        local need = math.max(r, (tonumber(t.focusDist) or 0) + 200)
        shielded[id] = M.shieldApply(t.respawn, shielded[id], need)
        if t.state == 'fadeOut' then
          t.state = 'active'
          t.alpha = 1
          if o.setMeshAlpha then o:setMeshAlpha(1, '') end
          stats.fadeSaves = (stats.fadeSaves or 0) + 1
        end
      elseif shielded[id] then
        -- behind the camera and out of sight again: stock may recycle it normally
        shielded[id] = M.shieldApply(t.respawn, shielded[id], nil)
      end
    end
  end
  for id in pairs(shielded) do
    if not data[id] then shielded[id] = nil end
  end
end

-- Traffic cars the block left behind (or all, after a teleport), and stuck ones, are respawned on the graph.
local stillSince = {}   -- car id -> os.clock() it stopped moving
local hazardSince = {}  -- car id -> os.clock() it became a damaged / stalled highway hazard
local seenAt = {}       -- car id -> { x, y, os.clock() it appeared there }
local lastTeleport = {} -- car id -> os.clock() it was last teleported
local syncTrafficPool
-- (lag: the time beyond GUARD_S since the last scan, a long frame's; it widens the cut reach and the jump threshold)
local function guard(b, all, lag)
  lag = lag or 0
  local gt = gameplay_traffic
  if not gt or gt.getState() ~= 'on' then return end
  syncTrafficPool(gt)
  local me = playerId()
  local pv = me >= 0 and getObjectByID(me) or nil
  local pp = pv and pv:getPosition() or nil
  local now = os.clock()
  -- paused: nothing moves, nobody is stuck (the clocks start again after it)
  local paused = simTimeAuthority and simTimeAuthority.getPause and simTimeAuthority.getPause()
  if paused then table.clear(stillSince); table.clear(hazardSince) end
  local mapObjs = rawget(_G, 'map') and map.objects or nil
  for id, traffic in pairs(gt.getTrafficData() or {}) do
    local o = getObjectByID(id)
    -- The full traffic table also contains player vehicles and may still contain an AI car just entered by a player.
    if localplayers.controlled(id, o, controlled) or (type(traffic) == 'table' and traffic.isAi == false) then
      stillSince[id], hazardSince[id], seenAt[id], lastTeleport[id] = nil, nil, nil, nil
    else
      if o and o:getActive() then
        local p = o:getPosition()
        local u = o:getVelocity()
        local sp = math.sqrt(u.x * u.x + u.y * u.y)
        local dir = sp > 1 and { x = u.x / sp, y = u.y / sp } or o:getDirectionVector()
        local dl = math.sqrt(dir.x * dir.x + dir.y * dir.y)
        local s0 = seenAt[id]
        -- (a long frame carried the car on at its speed: that is driving, not a respawn's jump)
        local jump = M.JUMP_M + (s0 and s0[4] or 0) * lag
        if not s0 or (p.x - s0[1]) ^ 2 + (p.y - s0[2]) ^ 2 > jump ^ 2 then
          s0 = { p.x, p.y, now }
          hazardSince[id] = nil
        end
        seenAt[id] = { p.x, p.y, s0[3], sp }
        local fresh = now - s0[3] < M.FRESH_S
        local reach = M.cutReach(sp, fresh, lag)
        local toCut = dl > 1e-6 and M.headingToCut(p.x, p.y, dir.x / dl, dir.y / dl, cuts, reach)
        if toCut and pp and M.followsPlayer(p, dir, pp, pv:getVelocity(), sp) then
          toCut = false     -- (the block goes on with the player: the guard's box check stays; GitHub #353)
        end
        -- Flowing fast-halo traffic crosses the inner phys cut onto the extension. Exempt toCut only.
        -- atCut below is unchanged: a car that has stopped or turned at a cut is still recycled.
        local rd = nil
        if toCut and (M.FAST_HALO_M or 0) > 0 and sp >= 8.0 then
          rd = roadAt(p.x, p.y, p.z)
          if M.skipFastHaloCut(rd and (rd.limit or rd.speedLimit) or 0, sp, b, p.x, p.y) then
            toCut = false
          end
        end
        -- at the cut already (GitHub #855): stopped at the dead end or turning round, which no longer heads for it
        local atCut = false
        if not toCut and not fresh and not paused and M.nearCut(p.x, p.y, cuts, M.CUT_NEAR_M) then
          local f = o:getDirectionVector()
          local fl = math.sqrt(f.x * f.x + f.y * f.y)
          local fwd = u.x * f.x + u.y * f.y
          local hx, hy        -- its direction of travel at speed, its facing at a stand
          if sp >= M.CUT_NEAR_MS then hx, hy = u.x / sp, u.y / sp
          elseif fl > 1e-6 then hx, hy = f.x / fl, f.y / fl end
          local r = hx and closestRoad(p.x, p.y, p.z) or nil
          local roadDot = r and (r.dx * hx + r.dy * hy) or nil
          local facingCut = fl > 1e-6 and M.headingToCut(p.x, p.y, f.x / fl, f.y / fl, cuts, M.CUT_NEAR_M) or false
          atCut = M.cutStopped(sp, fwd, roadDot, r and r.oneWay or nil, facingCut)
        end
        if not paused and o:getVelocity():length() < M.STUCK_MS then
          stillSince[id] = stillSince[id] or now
        else
          stillSince[id] = nil
        end
        local d = pp and (p - pp):length() or math.huge
        local dmg = (mapObjs and mapObjs[id] and tonumber(mapObjs[id].damage))
          or (type(traffic) == 'table' and tonumber(traffic.damage)) or 0
        local isHaz = false
        if not paused and (sp < 2.0 or (dmg >= M.WRECK_DAMAGE and sp < 8)) then
          hazardSince[id] = hazardSince[id] or now
          if not rd then rd = roadAt(p.x, p.y, p.z) end
          local camVis = type(traffic) == 'table' and traffic.camVisible == true or false
          isHaz = M.highwayHazard(dmg, sp, rd and (rd.limit or rd.speedLimit) or 0,
            nearJunction(p.x, p.y, M.HIGHWAY_JUNC_M), hazardSince[id], now, d, camVis, camAhead(p, pp))
        else
          hazardSince[id] = nil
        end
        local underCooldown = (now - (lastTeleport[id] or -math.huge)) < M.COOLDOWN_S
        local urgent = not M.inside(b, p.x, p.y, M.GUARD_M)
        if all or urgent or isHaz or (not underCooldown and (toCut or atCut or M.stuck(stillSince[id], now, d))) then
          if toCut then stats.cutRespawns = (stats.cutRespawns or 0) + 1
          elseif atCut then stats.cutStopRespawns = (stats.cutStopRespawns or 0) + 1 end
          if isHaz then stats.wreckRespawns = (stats.wreckRespawns or 0) + 1 end
          stillSince[id], hazardSince[id] = nil, nil
          lastTeleport[id] = now
          -- Ahead highway recycles use the graph horizon. Anything that cannot be placed far
          -- enough goes behind the camera, still invisible until its own pre-roll settles.
          local cp, cf = core_camera and core_camera.getPosition(), core_camera and core_camera.getForward()
          local pu = pv and pv:getVelocity()
          local psp = pu and math.sqrt(pu.x * pu.x + pu.y * pu.y) or 0
          local tryAhead = pp and pu and psp >= M.AHEAD_MS and not all and not isHaz
          local adx, ady = 1, 0
          if tryAhead then adx, ady = pu.x / psp, pu.y / psp end
          local ox, oy, odx, ody
          if tryAhead and M.ONCOMING then
            oncomingTurn = not oncomingTurn
            if oncomingTurn then
              local ok, x, y, ddx, ddy = pcall(M.oncomingSpot, pp.x, pp.y, adx, ady, b, M.rules.rightHandDrive,
                function(x, y) return closestRoad(x, y, pp.z) end, collideReachOf(psp))
              if ok and x then ox, oy, odx, ody = x, y, ddx, ddy end
            end
          end
          local aheadPlaced = false
          if tryAhead and not ox then
            local okP, placed = pcall(placeAhead, id, o, traffic, pp, pu, b, now)
            aheadPlaced = okP and placed or false
          end
          if ox then
            stats.oncomingRespawns = (stats.oncomingRespawns or 0) + 1
            cloakPreRoll(traffic, o)
            pcall(gt.forceTeleport, id, vec3(ox, oy, pp.z), vec3(odx, ody, 0), 0, 40)
            cloakPreRoll(traffic, o)
            launchWait[id] = {
              phase = 'prep', nextAt = now + 0.06, expires = now + 0.2 + M.CLOAK_MAX_S,
              minAheadM = M.UNCLOAK_MIN_M, target = nil,
            }
          elseif aheadPlaced then
            stats.aheadRespawns = (stats.aheadRespawns or 0) + 1
          elseif cp and cf then
            cloakPreRoll(traffic, o)
            pcall(gt.forceTeleport, id, cp, -cf, M.RESPAWN_M[1], M.RESPAWN_M[2])
            cloakPreRoll(traffic, o)
          else
            cloakPreRoll(traffic, o)
            pcall(gt.forceTeleport, id)
            cloakPreRoll(traffic, o)
          end
        end
      else
        stillSince[id], hazardSince[id] = nil, nil
      end
    end
  end
end

-- Stock forceTeleportAll defaults to every AI id, including an AI car a local player may have just entered. Hand it
-- an explicit filtered list instead; its full traffic data also contains non-AI player cars.
local eligibleTraffic = {}
local function scatterTraffic(gt, b)
  local list = gt.getTrafficList and gt.getTrafficList()
  if not gt.forceTeleportAll or type(list) ~= 'table' then return guard(b, true) end
  localplayers.snapshot(controlled)
  for i = #eligibleTraffic, 1, -1 do eligibleTraffic[i] = nil end
  for _, id in ipairs(list) do
    local o = getObjectByID(id)
    if o and not localplayers.controlled(id, o, controlled) then
      eligibleTraffic[#eligibleTraffic + 1] = id
    end
  end
  pcall(gt.forceTeleportAll, eligibleTraffic)
end

-- Stock can keep a traffic car in its AI pool after somebody enters it. In particular, setActiveAmount(0) would
-- deactivate that car, and reactivating it can invoke stock forceTeleport before our filtered scatter. Protect only
-- the occupied AI cars, and return each to its original pool and respawn policy when its last local occupant leaves.
local protectedTraffic, aiTraffic = {}, {}
local function trafficPool()
  local pools = rawget(_G, 'core_vehicleActivePooling')
  local pool = pools and pools.getPool and pools.getPool('autoTraffic')
  if pool and type(pool.allVehs) == 'table' and pool.removeVeh and pool.insertVeh then return pool end
end

local function releaseTraffic(id, saved, pool, data, force)
  local t = saved.traffic
  if data[id] == t then
    if t.enableRespawn == false then t.enableRespawn = saved.enableRespawn end
    if t.ignoreForceTeleport == true then t.ignoreForceTeleport = saved.ignoreForceTeleport end
  end
  if saved.pool == pool and pool and aiTraffic[id] and getObjectByID(id) and pool.allVehs[id] == nil then
    pool:insertVeh(id, force)
  end
  protectedTraffic[id] = nil
end

-- A false return means the pool cannot safely be put on hold; the post-graph guard still recycles the AI.
syncTrafficPool = function(gt)
  local data = gt.getTrafficData and gt.getTrafficData() or {}
  local list = gt.getTrafficList and gt.getTrafficList()
  for id in pairs(aiTraffic) do aiTraffic[id] = nil end
  if type(list) == 'table' then
    for _, id in ipairs(list) do aiTraffic[id] = true end
  else
    for id, t in pairs(data) do if type(t) == 'table' and t.isAi then aiTraffic[id] = true end end
  end
  localplayers.snapshot(controlled)
  local pool = trafficPool()
  for id, saved in pairs(protectedTraffic) do
    local o = getObjectByID(id)
    if not aiTraffic[id] or not o or data[id] ~= saved.traffic or not localplayers.controlled(id, o, controlled) then
      releaseTraffic(id, saved, pool, data, false)
    end
  end
  local safe = type(list) == 'table'
  for id in pairs(aiTraffic) do
    local o, t = getObjectByID(id), data[id]
    if o and localplayers.controlled(id, o, controlled) then
      if type(t) ~= 'table' then
        safe = false
      else
        local saved = protectedTraffic[id]
        if not saved then
          saved = { traffic = t, enableRespawn = t.enableRespawn, ignoreForceTeleport = t.ignoreForceTeleport }
          protectedTraffic[id] = saved
          t.enableRespawn, t.ignoreForceTeleport, t._teleport = false, true, nil
          if t.state ~= nil then t.state = 'locked' end
          if t.alpha ~= nil then t.alpha = 1 end
          if o.setMeshAlpha then o:setMeshAlpha(1, '') end
        end
        if pool and pool.allVehs[id] ~= nil then
          if pool:removeVeh(id) then saved.pool = pool else safe = false end
        elseif not pool then
          safe = false
        end
        -- A player can join while the pool is held. Clear its deferred stock teleport before making that car active.
        if o.getActive and o.setActive and not o:getActive() then
          t._teleport = nil
          o:setActive(1)
        end
      end
    end
  end
  return safe
end

-- core forwards the game's switch hook so a newly occupied traffic car is protected before the next guard tick.
function M.onVehicleSwitched(_, _, player)
  localplayers.observe(player)
  local gt = gameplay_traffic
  if gt and gt.getState() == 'on' then syncTrafficPool(gt) end
end

-- The player's traffic choice (the user, 2026-10-14: "if a player has them on then yes, if not then no"): the
-- level-start configurator's (Disabled / Parked only / Normal / Police; freeroam's spawningOptionsHelper), taken at
-- mission start by M.takeChoice. Off by default; the radial menu's traffic entries work at any time as on stock maps.
M.choice = { traffic = false, parked = false, police = false, trafficAmount = -1, parkedAmount = -1 }

-- Pure on its argument: the choice in freeroam's spawning options `o`, which are then set to disabled: freeroam would
-- spawn at camera-ready, before our first road graph is in the map; we start them once it is.
local heldOpts = nil    -- { options table, its traffic values } until freeroam's spawn time has passed

-- The player's options back as they chose them (the level-start configurator shows them as the current choice next
-- time): once our cars started, and when the level ends.
function M.restoreChoice()
  if not heldOpts then return end
  local o, v = heldOpts[1], heldOpts[2]
  o.trafficMode, o.trafficParked, o.trafficPolice = v[1], v[2], v[3]
  heldOpts = nil
end

function M.takeChoice(o)
  -- level start runs twice (seen 2026-10-14): the second would read the options we just disabled
  if heldOpts and heldOpts[1] == o then return M.choice end
  heldOpts = nil
  if type(o) ~= 'table' then
    M.choice = { traffic = false, parked = false, police = false, trafficAmount = -1, parkedAmount = -1 }
    return M.choice
  end
  M.choice = { traffic = o.trafficMode == 'enabled', parked = o.trafficParked == 'enabled',
               police = o.trafficPolice == 'enabled', trafficAmount = tonumber(o.trafficAmount) or -1,
               -- (0.40 renamed trafficParkedAmount to trafficParkingAmount)
               parkedAmount = tonumber(o.trafficParkingAmount or o.trafficParkedAmount) or -1 }
  -- (as stock freeroam: with traffic and parked cars both chosen, Options > Gameplay's parked vehicles setting off
  -- means none; GitHub #320 / #145: parked cars came with the setting off)
  local st = rawget(_G, 'settings')
  if M.choice.traffic and M.choice.parked and st and st.getValue and st.getValue('trafficParkedVehicles') == false then
    M.choice.parked = false
  end
  if log then
    log('I', 'beamngEarth', string.format('traffic choice: traffic %s, parked %s, police %s (freeroam %s / %s)',
      tostring(M.choice.traffic), tostring(M.choice.parked), tostring(M.choice.police), tostring(o.trafficMode),
      tostring(o.trafficParked)))
  end
  heldOpts = { o, { o.trafficMode, o.trafficParked, o.trafficPolice } }
  o.trafficMode, o.trafficParked, o.trafficPolice = 'disabled', 'disabled', 'disabled'
  return M.choice
end

-- The traffic cars of the region the player is in (the user, 2026-11-09: "the normal maps have this already"): a stock
-- level names its region in info.json (northAmerica, europe, asiaPacific) and the game's traffic picks the vehicles
-- of that region with Options > Gameplay > Traffic's smart selections on (gameplay/traffic/trafficUtils
-- getBaseGroupParams). Ours spans the Earth: the region of the road rules' country (server navgraph.road_rules),
-- the same filters as the game's own group (its simple vehicles too); nil (the game's own group) elsewhere, without
-- smart selections, or when the group cannot be made.
M.REGIONS = {
  northAmerica = { 'US', 'CA', 'MX', 'PR' },
  europe = { 'AT', 'BE', 'BG', 'HR', 'CY', 'CZ', 'DK', 'EE', 'FI', 'FR', 'DE', 'GR', 'HU', 'IE', 'IT', 'LV', 'LT', 'LU',
    'MT', 'NL', 'PL', 'PT', 'RO', 'SK', 'SI', 'ES', 'SE', 'GB', 'NO', 'CH', 'IS', 'LI', 'MC', 'SM', 'AD', 'AL', 'BA',
    'ME', 'MK', 'RS', 'XK', 'MD', 'UA', 'BY', 'FO', 'GI', 'IM', 'JE', 'GG' },
  asiaPacific = { 'JP', 'KR', 'TW', 'HK', 'MO', 'SG', 'MY', 'TH', 'ID', 'PH', 'AU', 'NZ' },
}
M.COUNTRY_NAMES = { US = 'United States', DE = 'Germany', IT = 'Italy', FR = 'France', JP = 'Japan' }   -- (countryRef)
M.GROUP_SIZE = 40       -- vehicles drawn for the traffic's pool
local regionOfCountry = {}
for region, list in pairs(M.REGIONS) do for _, cc in ipairs(list) do regionOfCountry[cc] = region end end

function M.regionOf(cc)
  return type(cc) == 'string' and regionOfCountry[string.upper(cc)] or nil
end

function M.regionalGroupParams(cc, getSetting)
  local region = M.regionOf(cc)
  if not region or not getSetting or not getSetting('trafficSmartSelections') then return nil end
  local params = { filters = { Type = { car = 1, truck = 0.6 }, Region = { [region] = 1, default = 1 } },
    country = M.COUNTRY_NAMES[string.upper(cc)] or 'default', maxYear = 0, minPop = 50, allConfigs = true,
    allMods = getSetting('trafficAllowMods') and true or false, modelPopPower = 1, configPopPower = 1 }
  if getSetting('trafficSimpleVehicles') then
    -- (the simple traffic models' configs carry their region too: US, EU and Japanese-market bodies; the game's
    -- own simple group, createTrafficGroup's: prop traffic only, every config)
    params.filters.Type = { proptraffic = 1 }
    params.minPop = 0
  else
    params.filters['Config Type'] = { Police = 0, other = 1 }
    if params.allMods then params.filters.Type.automation = 1; params.minPop = 0 end
  end
  return params, region
end

local function regionalGroup()
  local st = rawget(_G, 'settings')
  local cc = graph and graph.rules and graph.rules.country
  local params, region = M.regionalGroupParams(cc, st and st.getValue and function(k) return st.getValue(k) end)
  M.trafficRegionKey = region or 'none'
  M.trafficRegion = nil
  if not params then return nil end
  if not core_multiSpawn and extensions and extensions.load then pcall(extensions.load, 'core_multiSpawn') end
  if not (core_multiSpawn and core_multiSpawn.createGroup) then return nil end
  local ok, g = pcall(core_multiSpawn.createGroup, M.GROUP_SIZE, params)
  if not ok or type(g) ~= 'table' or not g[1] then return nil end
  M.trafficRegion = region
  log('I', 'beamngEarth', string.format('traffic: %s vehicles (%s)', region, tostring(cc)))
  return g
end

local function setupTraffic(gt, c)
  local okG, group = pcall(regionalGroup)
  local ok, err = pcall(gt.setupTraffic, c.trafficAmount ~= 0 and c.trafficAmount or -1,
    { police = c.police, vehGroup = okG and group or nil })
  if not ok then log('W', 'beamngEarth', 'navgraph: setupTraffic: ' .. tostring(err)) end
end

local function startTraffic()
  started = true
  M.restoreChoice()       -- freeroam's own spawn time has passed
  local c = M.choice
  local gt = c.traffic and trafficSystem() or nil
  if gt and gt.getState() ~= 'on' then setupTraffic(gt, c) end
end

-- The traffic set up again with the new place's vehicles when a graph of another region comes (a teleport from
-- the US to Germany kept the American cars): once per region change, the stock group outside the regions.
M.REGION_CHECK_S = 2.0
local regionCheckAt = 0
local function checkRegion(t)
  if t < regionCheckAt or not started or not M.trafficRegionKey then return end
  regionCheckAt = t + M.REGION_CHECK_S
  local want = M.regionOf(graph and graph.rules and graph.rules.country) or 'none'
  if want == M.trafficRegionKey then return end
  local c = M.choice
  local gt = c and c.traffic and trafficSystem() or nil
  if not gt or gt.getState() ~= 'on' then return end
  log('I', 'beamngEarth', string.format('traffic: region %s -> %s, set up again', M.trafficRegionKey, want))
  setupTraffic(gt, c)
  M.trafficRegionKey = want         -- (once a change, whatever the setup did)
end

-- The player teleported: the traffic cars go inactive (no physics: their old positions have no ground) and come
-- back on the new place's graph once it is in.
local heldActive = nil

-- Adaptive traffic amount (PR #728 / #773, ProxyFirewall202; docs/ADAPTIVE_TRAFFIC.md): with the setting on (M.ADAPTIVE,
-- settings adaptiveTraffic; off by default), the stock traffic's active amount follows the graph's trafficProfile.target
-- (server traffic.py: the nearest populated place and the road length round the graph's centre, a divided road
-- doubling it), never over the player's own amount (the ceiling before we touched it, or the cars spawned), moved one
-- car at a time at most every ADAPTIVE_STEP_S; after a teleport the new place's amount at once. Not while the pool is
-- held for a teleport or an occupied traffic car is protected. A ceiling changed by someone else (the radial menu, the
-- game's UI) becomes the player's amount. Off again: the player's amount back. Looked at every ADAPTIVE_CHECK_S.
M.ADAPTIVE = false
M.ADAPTIVE_STEP_S = 5
M.ADAPTIVE_CHECK_S = 1
-- base: the player's amount (the stock ceiling we found, or set by someone else since); seen: the ceiling as we last
-- left it; touched: we changed it; paused: the player's traffic toggled off (ceiling 0), left alone until it is back
local function newAdaptive()
  return { base = nil, seen = nil, touched = false, paused = false, target = nil, lastStep = -math.huge,
           lastCheck = -math.huge }
end
local adaptive = newAdaptive()

-- Pure: the amount (whole cars) for a profile target t and the player's amount cap.
function M.adaptiveTarget(t, cap)
  t, cap = tonumber(t), tonumber(cap)
  if not t or t ~= t or not cap or cap ~= cap then return nil end
  return math.max(0, math.min(math.floor(cap), math.floor(t + 0.5)))
end

-- Pure: the next ceiling from current toward target, one car at a time.
function M.adaptiveStep(current, target)
  if current < target then return current + 1 elseif current > target then return current - 1 end
  return current
end

-- The player's amount as a number of cars: base, at most the cars the traffic spawned (a ceiling of math.huge: those).
local function adaptiveCap(gt)
  local base = adaptive.base
  local n = gt.getNumOfTraffic and tonumber(gt.getNumOfTraffic()) or nil
  if not base or base == math.huge then base = n or base end
  if n and base then base = math.min(base, n) end
  return base
end

-- The amount the graph's profile asks for now (nil: adaptive off, no profile or no player's amount yet).
local function adaptiveAmount(gt)
  if not (M.ADAPTIVE and graph and graph.trafficProfile and gt and adaptive.base) then return nil end
  return M.adaptiveTarget(graph.trafficProfile.target, adaptiveCap(gt))
end

local function updateAdaptive(now)
  if now - adaptive.lastCheck < M.ADAPTIVE_CHECK_S then return end
  adaptive.lastCheck = now
  local gt = gameplay_traffic
  if not (started and M.choice.traffic and gt and gt.getState and gt.getState() == 'on' and gt.setActiveAmount) then
    return
  end
  if heldActive ~= nil or respawnAll or next(protectedTraffic) ~= nil then return end
  local vars = gt.getTrafficVars and gt.getTrafficVars() or {}
  local cur = tonumber(vars.activeAmount)
  if cur == nil then return end
  if not M.ADAPTIVE then
    if adaptive.touched and cur == adaptive.seen and adaptive.base ~= nil and not adaptive.paused then
      pcall(gt.setActiveAmount, adaptive.base)               -- (off again: the player's amount back)
      log('I', 'beamngEarth', 'Adaptive traffic off: active amount back to ' .. tostring(adaptive.base))
    end
    if adaptive.base ~= nil then adaptive = newAdaptive() end
    return
  end
  if adaptive.seen == nil or cur ~= adaptive.seen then
    -- (first look, or changed by someone else: the player's amount now; 0: their traffic toggled off)
    if cur == 0 then adaptive.paused = true else adaptive.base, adaptive.paused = cur, false end
    adaptive.seen = cur
  end
  if adaptive.paused then return end
  local target = adaptiveAmount(gt)
  adaptive.target = target
  if not target or now - adaptive.lastStep < M.ADAPTIVE_STEP_S then return end
  local current = cur
  if current == math.huge then current = adaptiveCap(gt) end
  if not current or current == target then return end
  local nextAmount = M.adaptiveStep(current, target)
  if nextAmount < current and gt.getTrafficData then
    local data = gt.getTrafficData() or {}
    local cp = core_camera and core_camera.getPosition and core_camera.getPosition() or nil
    local cf = core_camera and core_camera.getForward and core_camera.getForward() or nil
    local cands = {}
    if cp and cf then
      local fl = math.sqrt(cf.x * cf.x + cf.y * cf.y)
      if fl > 1e-6 then
        local fx, fy = cf.x / fl, cf.y / fl
        localplayers.snapshot(controlled)
        for id, t in pairs(data) do
          local o = getObjectByID(id)
          if type(t) == 'table' and t.isAi ~= false and o and o:getActive()
              and not localplayers.controlled(id, o, controlled) then
            local p = o:getPosition()
            cands[#cands + 1] = {
              id = id, camVisible = t.camVisible == true,
              ahead = (p.x - cp.x) * fx + (p.y - cp.y) * fy,
            }
          end
        end
      end
    end
    if #cands > 0 then
      local dropId = M.pickAdaptiveDrop(cands)
      if not dropId then return end     -- (every active AI car is in view / ahead: wait until one is behind)
      local pool = trafficPool()
      if pool and pool.setVeh then pcall(pool.setVeh, pool, dropId, false) end
    end
  end
  local ok, err = pcall(gt.setActiveAmount, nextAmount)
  if ok then
    adaptive.seen, adaptive.touched, adaptive.lastStep = nextAmount, true, now
    log('D', 'beamngEarth', string.format('Adaptive traffic: active amount %s -> %d (target %d)', tostring(current),
      nextAmount, target))
  else
    log('W', 'beamngEarth', 'Adaptive traffic: setActiveAmount: ' .. tostring(err))
  end
end

function M._adaptiveTick(now) return updateAdaptive(now) end     -- (tests)
function M.setAdaptive(on) M.ADAPTIVE = on and true or false end

function M.trafficStatus()
  return { enabled = M.ADAPTIVE, target = adaptive.target, applied = adaptive.touched and adaptive.seen or nil,
           base = adaptive.base, paused = adaptive.paused, profile = graph and graph.trafficProfile or nil }
end

-- A rebase (core finishRebase, xf: the old frame -> the new one): the server's graph moved into the new anchor and
-- applied again at once, as the roads and trees are moved. It was dropped instead (its anchor no longer the current
-- one) and the game's map stayed in the old frame until the server's graph for the new anchor came, seconds later:
-- the AI planned toward roads hundreds of metres off and swerved, into a street tree on North Front Street, Baltimore
-- (Steam Deck, 3 of 3 rebase drives, 2026-10-29). The server's own graph replaces this one when it comes.
function M.transform(xf, anchorId)
  if not graph or not xf then return false end
  local nodes = {}
  for name, n in pairs(graph.nodes) do
    local m = {}
    for i, v in ipairs(n) do m[i] = v end
    m[1], m[2], m[3] = xf:apply(n[1], n[2], n[3])
    nodes[name] = m
  end
  local lines
  if graph.lines then
    lines = {}
    for i, l in ipairs(graph.lines) do
      local x1, y1 = xf:apply(l[1], l[2], 0)
      local x2, y2 = xf:apply(l[3], l[4], 0)
      lines[i] = { x1, y1, x2, y2, l[5] }
    end
  end
  graph = { anchor = anchorId, serial = graph.serial, nodes = nodes, edges = graph.edges, signals = graph.signals,
            rules = graph.rules, lines = lines, trafficProfile = graph.trafficProfile, mph = graph.mph, moved = true }
  dirty, urgent = true, true
  if applied then applied.box = nil end
  return true
end

function M.currentGraph() return graph end      -- (tests)
function M._startTraffic() return startTraffic() end    -- (tests: the level start's traffic, by hand)
function M._cuts() return cuts end      -- (probes: the clip's cut nodes)

function M.onTeleport()
  respawnAll = true
  launchCount, launchWait, nextPlatoonSlot, lastPlatoonAt = {}, {}, 0, -math.huge
  local gt = gameplay_traffic
  if gt and gt.getState() == 'on' and not heldActive and syncTrafficPool(gt) then
    local vars = gt.getTrafficVars and gt.getTrafficVars() or {}
    heldActive = vars.activeAmount or math.huge
    pcall(gt.setActiveAmount, 0)
  end
end

-- Every frame: a new graph, a moved block or a foreign reset applies the graph again; the traffic guard.
-- Pure: how far the centre of ground box b {x0, y0, x1, y1} is from that of box a (0 when either is missing).
function M.boxMoved(a, b)
  if not a or not b or not a[4] or not b[4] then return 0 end
  local dx = (b[1] + b[3]) / 2 - (a[1] + a[3]) / 2
  local dy = (b[2] + b[4]) / 2 - (a[2] + a[4]) / 2
  return math.sqrt(dx * dx + dy * dy)
end

local waited = 0

-- The node tables an apply hands map.reset, built over several frames (toNodes in a coroutine, PREP_MS a frame; ~20 ms in
-- one frame on a Steam Deck), for this graph and block box; nil until done.
M.PREP_MS = 2.0
M.PREP_YIELD = 256
local prep = nil        -- { graph, box, co, result }

local function buildNodes(b)
  if not prep or prep.graph ~= graph or not (prep.box == b or (prep.box and b and prep.box[1] == b[1] and prep.box[2] == b[2]
      and prep.box[3] == b[3] and prep.box[4] == b[4])) then
    setRules()
    local g, box, rh = graph, b and { b[1], b[2], b[3], b[4] } or nil, M.rules.rightHandDrive
    prep = { graph = g, box = box, co = coroutine.create(function()
      return { M.toNodes(g, box, M.EDGE_M, rh, M.PREP_YIELD, M.HALO_M, M.FAST_HALO_M) }
    end) }
  end
  if prep.result then return prep.result end
  local sw = stopwatch.new()
  repeat
    local ok, res = coroutine.resume(prep.co)
    if not ok then
      log('W', 'beamngEarth', 'navgraph: nodes: ' .. tostring(res))
      prep = nil
      return nil
    end
    if coroutine.status(prep.co) == 'dead' then prep.result = res end
  until prep.result or sw() >= M.PREP_MS
  return prep.result
end

-- May the apply run in this frame (gov: the frame governor; nil: always)?
local function room(gov)
  if not (gov and gov.roomFor) or waited >= M.APPLY_WAIT_FRAMES or gov:roomFor(M.APPLY_ROOM_MS) then
    waited = 0
    return true
  end
  waited = waited + 1
  return false
end

-- Pure (PR #728, ProxyFirewall202): the vehicle Lua that pushes a car standing (or slower than target - 1 m/s) on to
-- target m/s along its heading (thrusters.applyVelocity) and spins its wheels to match (beamngEarth_spawnMotion), or nil.
function M.spawnVelocityCommand(target)
  target = tonumber(target)
  if not target or target ~= target or target <= 1 or target == math.huge or target > 80 then return nil end
  local threshold = math.max(0, target - 1)
  return string.format('local _d=obj:getDirectionVector(); local _v=obj:getVelocity(); local _f=_v:dot(_d); '
    .. 'if _f>=-1 and _f<%.3f and thrusters and type(thrusters.applyVelocity)=="function" then '
    .. 'local _ok=pcall(thrusters.applyVelocity,_d*%.3f); if _ok then pcall(function() '
    .. 'if not beamngEarth_spawnMotion then extensions.load("beamngEarth_spawnMotion") end; '
    .. 'if beamngEarth_spawnMotion then beamngEarth_spawnMotion.applyRollingSpeed(%.3f) end end) end end',
    threshold, target, target)
end

-- Pure: the speed (m/s) a respawned car is launched at on an edge with this limit (m/s), its heading's cosine with the
-- edge's direction (in -> out; negative: against it), the edge one-way, a junction within LAUNCH_JUNCTION_M, and an
-- optional leadGap (m) / leadSpeed (m/s) of a vehicle ahead in the same lane; or nil.
function M.launchSpeed(limit, cosine, oneWay, nearJunction, leadGap, leadSpeed)
  limit, cosine = tonumber(limit), tonumber(cosine)
  if not limit or not cosine or nearJunction then return nil end
  if (oneWay and cosine or math.abs(cosine)) < M.LAUNCH_ALIGN then return nil end
  local v = math.min(limit * M.LAUNCH_SHARE, 80)
  if leadGap and leadGap > 0 and leadGap < 80 and leadSpeed ~= nil then
    if leadGap < 25 and leadSpeed < v * 0.7 then return nil end
    local safe = math.max(0, math.min(leadSpeed - 1.5, leadGap / M.LAUNCH_HEADWAY_S))
    v = math.min(v, safe)
  end
  if v < M.LAUNCH_MIN_MS then return nil end
  return v
end

-- True when a newly respawned 0-mph AI car landed right in the path of a fast-approaching vehicle (player or AI)
-- within SPAWN_CONFLICT_M, or on top of another vehicle (< 16 m). map.objects entries with active == false still count:
-- a pooled car is not driving, but it is still in the lane.
local function spawnConflict(id)
  local o = getObjectByID(id)
  if not o or not o:getActive() or localplayers.controlled(id, o, controlled) then return false end
  local p = o:getPosition()
  local objs = rawget(_G, 'map') and map.objects or nil
  if not objs then return false end
  for oid, mo in pairs(objs) do
    if oid ~= id and mo and mo.pos then
      local dx, dy = p.x - mo.pos.x, p.y - mo.pos.y
      local d2 = dx * dx + dy * dy
      if d2 < 16 ^ 2 then return true end
      if mo.vel then
        local vx, vy = mo.vel.x or 0, mo.vel.y or 0
        local sp = math.sqrt(vx * vx + vy * vy)
        local reach = math.max(M.SPAWN_CONFLICT_M, sp * 3.5)
        if sp >= 12 and d2 < reach ^ 2 then
          local ux, uy = vx / sp, vy / sp
          local ahead = dx * ux + dy * uy
          local side = math.abs(-dx * uy + dy * ux)
          if ahead > 0 and ahead < reach and side < M.LAUNCH_LANE_M then return true end
        end
      end
    end
  end
  return false
end

-- Pure: snap a vehicle's lateral offset `lat` (+ right of in->out centreline) onto the nearest legal lane centre of an
-- edge with half-width `radius` and lane config `lanes`. Stock trafficUtils.finalizeSpawnPoint divides roadWidth by a
-- hardcoded 3.05 m (estimating 4 lanes on a 12.0 m 3-lane highway and placing cars at +-1.5 m right on the +-2.0 m
-- white lane dividers). `leftHand` is accepted so callers can pass the rules flag; lane centres come from `lanes`.
function M.snapLaneOffset(lat, radius, lanes, along, leftHand)
  radius = tonumber(radius)
  if not lat or not radius or radius <= 0 or type(lanes) ~= 'string' or #lanes < 1 then return lat end
  local n = #lanes
  local w = 2 * radius / n
  local want = along and '+' or '-'
  local best, bestD = nil, math.huge
  for i = 1, n do
    if lanes:sub(i, i) == want then
      local c = -radius + (i - 0.5) * w
      local d = math.abs(lat - c)
      if d < bestD then best, bestD = c, d end
    end
  end
  return best or lat
end

local function alignToRealLane(o, inN, outN, e, fn)
  if not (o and inN and outN and e and type(e.lanes) == 'string' and #e.lanes >= 2) then return end
  local d = outN.pos - inN.pos
  d.z = 0
  local dl = d:length()
  if dl < 0.1 then return end
  local ux, uy = d.x / dl, d.y / dl
  local rx, ry = uy, -ux
  local p = o:getPosition()
  local along = (ux * fn.x + uy * fn.y) >= 0
  local radius = math.min(inN.radius or e.inRadius or 0, outN.radius or e.outRadius or 0)
  if radius <= 0 then radius = e.inRadius or 0 end
  local lat = (p.x - inN.pos.x) * rx + (p.y - inN.pos.y) * ry
  local snap = M.snapLaneOffset(lat, radius, e.lanes, along, M.rules.rightHandDrive)
  local shift = snap - lat
  if math.abs(shift) > 0.25 and math.abs(shift) < radius then
    local nx, ny, nz = p.x + rx * shift, p.y + ry * shift, p.z
    local dir = vec3(along and ux or -ux, along and uy or -uy, 0)
    if spawn and spawn.safeTeleport then
      pcall(spawn.safeTeleport, o, vec3(nx, ny, nz), quatFromDir(dir, vec3(0, 0, 1)), nil, nil, false, nil, false)
    end
  end
end

local function queueMotion(o, body)
  if not (o and type(o.queueLuaCommand) == 'function') then return end
  pcall(o.queueLuaCommand, o,
    'if not beamngEarth_spawnMotion then extensions.load("beamngEarth_spawnMotion") end; if beamngEarth_spawnMotion then '
    .. body .. ' end')
end

local function objectById(id)
  if type(getObjectByID) ~= 'function' then return nil end
  local ok, o = pcall(getObjectByID, id)
  if ok then return o end
end

local function aheadMeters(o)
  if not (o and o.getPosition) then return nil end
  local me = playerId()
  if not me or me < 0 then return nil end
  local pv = objectById(me)
  if not (pv and pv.getPosition and pv.getVelocity) then return nil end
  local pp, p = pv:getPosition(), o:getPosition()
  if not (pp and p and pp.x and p.x) then return nil end
  local vel = pv:getVelocity()
  local ux, uy
  if vel and vel.x then
    local sp = math.sqrt(vel.x * vel.x + vel.y * vel.y)
    if sp > 1 then ux, uy = vel.x / sp, vel.y / sp end
  end
  if not ux and pv.getDirectionVector then
    local f = pv:getDirectionVector()
    if f and f.x then
      local fl = math.sqrt(f.x * f.x + f.y * f.y)
      if fl > 1e-6 then ux, uy = f.x / fl, f.y / fl end
    end
  end
  if not ux then return nil end
  return (p.x - pp.x) * ux + (p.y - pp.y) * uy
end

local function recycleBehind(id, t, now)
  launchWait[id] = nil
  lastTeleport[id] = now
  cloakPreRoll(t, objectById(id))
  local gt = gameplay_traffic
  local cp = core_camera and core_camera.getPosition and core_camera.getPosition()
  local cf = core_camera and core_camera.getForward and core_camera.getForward()
  if cp and cf and gt and gt.forceTeleport then
    pcall(gt.forceTeleport, id, cp, -cf, M.RESPAWN_M[1], M.RESPAWN_M[2])
  elseif gt and gt.forceTeleport then
    pcall(gt.forceTeleport, id)
  end
end

local function uncloakFade(id, t)
  if type(t) == 'table' then
    t.alpha = 0
    t.state = 'fadeIn'
  end
  launchWait[id] = nil
end

local function alignLaunch(id)
  local o = objectById(id)
  if not (o and o.getActive and o:getActive()) then return end
  if localplayers.controlled(id, o, controlled) then return end
  local m = map and map.getMap and map.getMap()
  if not (m and map.findClosestRoad) then return end
  local p = o:getPosition()
  local n1, n2 = map.findClosestRoad(p)
  local a, c = n1 and m.nodes[n1], n2 and m.nodes[n2]
  local e = a and c and (a.links[n2] or c.links[n1])
  if not e then return end
  local inN, outN = m.nodes[e.inNode or n1], m.nodes[e.outNode or n2]
  if not (inN and outN) then return end
  local f = o.getDirectionVector and o:getDirectionVector()
  if not (f and f.x) then return end
  pcall(alignToRealLane, o, inN, outN, e, f)
end

local function roadLaunchSpeed(id)
  local o = objectById(id)
  if not (o and o.getActive and o:getActive()) then return nil end
  if localplayers.controlled(id, o, controlled) then return nil end
  local m = map and map.getMap and map.getMap()
  if not (m and map.findClosestRoad) then return nil end
  local p = o:getPosition()
  local n1, n2 = map.findClosestRoad(p)
  local a, c = n1 and m.nodes[n1], n2 and m.nodes[n2]
  local e = a and c and (a.links[n2] or c.links[n1])
  if not (e and e.inPos and e.outPos and e.inPos.x and e.outPos.x) then return nil end
  local ex, ey = e.outPos.x - e.inPos.x, e.outPos.y - e.inPos.y
  local el = math.sqrt(ex * ex + ey * ey)
  if el < 0.1 then return nil end
  ex, ey = ex / el, ey / el
  local f = o:getDirectionVector()
  if not (f and f.x) then return nil end
  local fl = math.sqrt(f.x * f.x + f.y * f.y)
  if fl < 0.1 then return nil end
  local fx, fy = f.x / fl, f.y / fl
  local leadGap, leadSpeed = nil, nil
  local objs = rawget(_G, 'map') and map.objects or nil
  if objs then
    for oid, mo in pairs(objs) do
      if oid ~= id and mo and mo.pos then
        local rx, ry = mo.pos.x - p.x, mo.pos.y - p.y
        local ahead = rx * fx + ry * fy
        local side = math.abs(-rx * fy + ry * fx)
        if ahead > 0 and ahead < (leadGap or 80) and side < M.LAUNCH_LANE_M then
          local vx = mo.vel and mo.vel.x or 0
          local vy = mo.vel and mo.vel.y or 0
          if vx * fx + vy * fy > -2 then
            leadGap = ahead
            leadSpeed = math.max(0, vx * fx + vy * fy)
          end
        end
      end
    end
  end
  local v = M.launchSpeed(e.speedLimit, ex * fx + ey * fy, e.oneWay, nearJunction(p.x, p.y), leadGap, leadSpeed)
  local w = package.loaded['beamngEarth/winter']
  if v and w and type(w.speedFactor) == 'function' then
    v = v * w.speedFactor()
    if v < M.LAUNCH_MIN_MS then v = nil end
  end
  return v
end

-- Vehicle pre-roll reports land here. navgraph is require()'d, so the vehicle calls
-- package.loaded["beamngEarth/navgraph"].onLaunchReport rather than extensions.*.
function M.onLaunchReport(id, bodyMps, wheelMps, parkingBrake, brake)
  local w = launchWait[id]
  if not w or w.phase ~= 'settle' then return false end
  local ahead = w.aheadM
  local o = objectById(id)
  if o then
    local ok, got = pcall(aheadMeters, o)
    if ok and type(got) == 'number' then ahead = got end
  end
  w.aheadM = ahead
  if not M.uncloakReady(ahead, w.minAheadM, bodyMps, w.target, wheelMps, parkingBrake, brake) then
    return false
  end
  local gt = gameplay_traffic
  local data = gt and gt.getTrafficData and gt.getTrafficData() or nil
  uncloakFade(id, data and data[id])
  stats.launches = (stats.launches or 0) + 1
  return true
end

-- Every LAUNCH_SCAN_S: a new respawn is cloaked in preRoll, brakes released, then launched
-- on one graphics dt. It fades in only after onLaunchReport says the drivetrain has settled.
local function launchRespawned(now)
  local gt = gameplay_traffic
  if not M.LAUNCH or not gt or not gt.getState or gt.getState() ~= 'on' or now - lastLaunchScan < M.LAUNCH_SCAN_S then
    return
  end
  lastLaunchScan = now
  if simTimeAuthority and simTimeAuthority.getPause and simTimeAuthority.getPause() then return end
  localplayers.snapshot(controlled)
  local data = gt.getTrafficData() or {}
  for id in pairs(conflictCache) do
    if not data[id] then conflictCache[id] = nil end
  end
  for id, t in pairs(data) do
    if type(t) == 'table' and t.isAi ~= false then
      local rc = tonumber(t.respawnCount)
      if rc and launchCount[id] and rc > launchCount[id] then
        launchCount[id] = rc
        conflictCache[id] = nil   -- a new respawn is a new position: always look afresh
        if M.cachedConflict(conflictCache, id, now, M.CONFLICT_CACHE_S, spawnConflict) then
          conflictCache[id] = nil
          stats.conflictRespawns = (stats.conflictRespawns or 0) + 1
          recycleBehind(id, t, now)
        elseif not launchWait[id] then
          local o0 = objectById(id)
          cloakPreRoll(t, o0)
          local a0 = nil
          local ok0, got0 = pcall(aheadMeters, o0)
          if ok0 and type(got0) == 'number' then a0 = got0 end
          launchWait[id] = {
            phase = 'prep', nextAt = now + 0.06, expires = now + 0.2 + M.CLOAK_MAX_S,
            minAheadM = (a0 and a0 > 40) and M.UNCLOAK_MIN_M or nil, target = nil,
          }
        end
      end
      launchCount[id] = tonumber(t.respawnCount) or rc
      local w = launchWait[id]
      local o = w and objectById(id) or nil
      if w and o then
        local ahead = nil
        local okA, got = pcall(aheadMeters, o)
        if okA and type(got) == 'number' then ahead = got end
        w.aheadM = ahead
        local conflict = M.cachedConflict(conflictCache, id, now, M.CONFLICT_CACHE_S, spawnConflict)
        if (w.minAheadM and ahead and ahead < M.UNCLOAK_MIN_M) or conflict then
          if conflict then
            stats.conflictRespawns = (stats.conflictRespawns or 0) + 1
            conflictCache[id] = nil
          end
          recycleBehind(id, t, now)
        elseif w.phase == 'prep' and now >= (w.nextAt or 0) then
          if not w.exactPose then
            local before = tonumber(t.respawnCount)
            pcall(alignLaunch, id)
            local after = tonumber(t.respawnCount)
            if after and after ~= before then launchCount[id] = after end
          end
          cloakPreRoll(t, o)
          queueMotion(o, 'beamngEarth_spawnMotion.prepLaunch()')
          w.phase, w.nextAt = 'impulse', now + 0.06
        elseif w.phase == 'impulse' and now >= (w.nextAt or 0) then
          local v = roadLaunchSpeed(id)
          if not v then
            if w.minAheadM then
              recycleBehind(id, t, now)
            else
              uncloakFade(id, t)
            end
          else
            w.target = v
            w.phase = 'settle'
            w.expires = now + M.CLOAK_MAX_S
            queueMotion(o, string.format('beamngEarth_spawnMotion.launchAtSpeed(%.3f, %d)', v, id))
          end
        elseif w.phase == 'settle' and now >= (w.expires or 0) then
          if w.minAheadM then
            recycleBehind(id, t, now)
          else
            uncloakFade(id, t)
          end
        elseif w.phase == 'settle' then
          queueMotion(o, string.format('beamngEarth_spawnMotion.probeSettle(%.3f, %d)', w.target or 0, id))
        elseif now >= (w.expires or math.huge) then
          if w.minAheadM then
            recycleBehind(id, t, now)
          else
            uncloakFade(id, t)
          end
        end
      end
    end
  end
  for wid in pairs(launchCount) do
    if data[wid] == nil then launchCount[wid], launchWait[wid] = nil, nil end
  end
end

-- Overtaking on multi-lane highways (overtake.lua): the traffic cars and the player's car while it autodrives.
local function overtakeScan(now)
  local gt = gameplay_traffic
  if not overtake.due(now) or not gt or not gt.getState or gt.getState() ~= 'on' then return end
  local ids = {}
  for id, t in pairs(gt.getTrafficData() or {}) do
    if type(t) == 'table' and t.isAi ~= false then ids[id] = true end
  end
  local gps = package.loaded['beamngEarth/gps']
  local st = gps and gps.status and gps.status()
  local me = playerId()
  local auto = st and st.autodrive and me >= 0 and me or nil
  if auto then ids[auto] = nil end
  localplayers.snapshot(controlled)
  overtake.update(now, ids, auto, nearJunction, M.rules.rightHandDrive,
    function(id) return localplayers.controlled(id, getObjectByID(id), controlled) end)
end

function M.update(aid, b, gov)
  if not M.enabled or not graph then return end
  local t0 = os.clock()
  do
    local ok, err = pcall(checkRegion, t0)
    if not ok then log('W', 'beamngEarth', 'navgraph region: ' .. tostring(err)) end
  end
  if applied then
    local ok, err = pcall(shieldVisibleTraffic, t0, b)
    if not ok then log('W', 'beamngEarth', 'navgraph sight shield: ' .. tostring(err)) end
    ok, err = pcall(launchRespawned, t0)
    if not ok then log('W', 'beamngEarth', 'navgraph launch scan: ' .. tostring(err)) end
    ok, err = pcall(overtakeScan, t0)
    if not ok then log('W', 'beamngEarth', 'navgraph overtake scan: ' .. tostring(err)) end
    if t0 - adaptive.lastCheck >= M.ADAPTIVE_CHECK_S then       -- (once a second, on or off: off puts the amount back)
      ok, err = pcall(updateAdaptive, t0)
      if not ok then log('W', 'beamngEarth', 'navgraph adaptive traffic: ' .. tostring(err)) end
    end
  end
  if applied and t0 - lastGuard >= M.GUARD_S then
    -- (the gate is elapsed time, so the first frame after a long one scans at once; the lag widens its window)
    local lag = lastGuard > -math.huge
      and math.min(math.max(t0 - lastGuard - M.GUARD_S, 0), M.GUARD_LAG_MAX_S) or 0
    lastGuard = t0
    local ok, err = pcall(guard, b, false, lag)
    if not ok then log('W', 'beamngEarth', 'navgraph guard: ' .. tostring(err)) end
    local g = (os.clock() - t0) * 1000
    stats.guardMs = g
    stats.guardMaxMs = math.max(stats.guardMaxMs or 0, g)
  end
  if graph.anchor ~= aid then
    graph = nil
    return
  end
  if pendingSignals then
    if not room(gov) then return end
    local nodes = pendingSignals
    pendingSignals = nil
    local sw = stopwatch.new()
    applySignals(nodes)
    if gov and gov.noteFrame then gov:noteFrame(sw()) end
    return
  end
  -- a small block move alone does not re-apply (each apply is a ~40 ms frame); one of RECLIP_M does
  if not (dirty or foreign) and applied and M.boxMoved(applied.box, b) >= M.RECLIP_M then dirty = true end
  if not (dirty or foreign) then return end
  local t = os.clock()
  if t - lastApply < M.MIN_GAP_S and not foreign and not urgent then return end
  -- (a moved graph, M.transform: built in one go and applied in this frame, whatever the frame budget: until the cars
  -- have it their AI drives on points of the old frame)
  local pre
  if urgent then
    setRules()
    pre = { M.toNodes(graph, b and { b[1], b[2], b[3], b[4] } or nil, M.EDGE_M, M.rules.rightHandDrive, nil, M.HALO_M,
                      M.FAST_HALO_M) }
  else
    pre = buildNodes(b)
    if not pre or not room(gov) then return end
  end
  prep = nil
  lastApply = t
  dirty, foreign = false, false
  local moved = urgent
  urgent = false
  local sw = stopwatch.new()
  local okA = apply(b, pre, moved)
  if gov and gov.noteFrame then gov:noteFrame(sw()) end
  if okA then
    applied = { anchor = aid, serial = graph.serial, box = b and { b[1], b[2], b[3], b[4] } or {} }
    if not started then startTraffic() end
    if respawnAll then
      respawnAll = false
      local gt = gameplay_traffic
      if gt and heldActive then
        syncTrafficPool(gt)
        -- (adaptive: the new place's amount at once, not stepped up from the old place's)
        if M.ADAPTIVE and not adaptive.paused then
          if adaptive.base == nil then adaptive.base = heldActive end    -- (the pool's 0 while held is not the player's)
          local okA2, want = pcall(adaptiveAmount, gt)
          if okA2 and want then heldActive, adaptive.seen, adaptive.touched = want, want, true end
        end
        pcall(gt.setActiveAmount, heldActive)
        heldActive = nil
        scatterTraffic(gt, b)
      else
        pcall(guard, b, true)
      end
    end
  end
end

-- onNavgraphReloaded (core forwards the hook): someone else rebuilt the map; ours again next frame.
function M.onReloaded()
  if not ours and graph then foreign = true end
end

-- How many times a graph went into the game's map (gps.lua: the map's node table stays the same table across them).
function M.applies() return stats.applies end

-- The server's last graph, unclipped ({anchor, serial, nodes, edges}; nil before one): the cars' navigation screens
-- draw its roads (dashboard.lua). Read-only. (The accessor as in iambobab's PR #472.)
function M.graph() return graph end

function M.status()
  return { graph = graph and graph.serial or nil, applies = stats.applies, lastMs = stats.lastMs,
           nodes = stats.nodes, edges = stats.edges, junctions = stats.junctions, started = started,
           cuts = stats.cuts, cutRespawns = stats.cutRespawns, cutStopRespawns = stats.cutStopRespawns,
           aheadRespawns = stats.aheadRespawns,
           wreckRespawns = stats.wreckRespawns, conflictRespawns = stats.conflictRespawns, fadeSaves = stats.fadeSaves,
           launches = stats.launches, oncomingRespawns = stats.oncomingRespawns, overtake = overtake.status(),
           resetMs = stats.resetMs, signalsMs = stats.signalsMs, nodesMs = stats.nodesMs, gpsMs = stats.gpsMs,
           applyMs = stats.applyMs, guardMs = stats.guardMs, guardMaxMs = stats.guardMaxMs,
           traffic = gameplay_traffic and gameplay_traffic.getState() or nil, trafficAdaptive = M.trafficStatus() }
end

function M.reset()
  local gt = gameplay_traffic
  if gt and gt.getState and gt.getState() == 'on' and (heldActive or next(protectedTraffic)) then
    syncTrafficPool(gt)
    if heldActive then pcall(gt.setActiveAmount, heldActive) end
    local pool, data = trafficPool(), gt.getTrafficData and gt.getTrafficData() or {}
    -- Keep any still-occupied cars active while returning them to the stock pool on level exit.
    for id, saved in pairs(protectedTraffic) do releaseTraffic(id, saved, pool, data, true) end
  else
    for id, saved in pairs(protectedTraffic) do
      local t = saved.traffic
      if t.enableRespawn == false then t.enableRespawn = saved.enableRespawn end
      if t.ignoreForceTeleport == true then t.ignoreForceTeleport = saved.ignoreForceTeleport end
      protectedTraffic[id] = nil
    end
  end
  heads = {}            -- (the level took them with it)
  graph, applied, dirty, lastApply, persistent, ours, foreign = nil, nil, false, -math.huge, nil, false, false
  started, respawnAll, lastGuard, heldActive = false, false, -math.huge, nil
  adaptive = newAdaptive()
  launchCount, launchWait, lastLaunchScan, junctionNodes, junctionOf = {}, {}, -math.huge, nil, nil
  nextPlatoonSlot, lastPlatoonAt = 0, -math.huge
  lastSightShield = -math.huge
  shielded = {}
  conflictCache = {}
  overtake.reset()
  pendingSignals = nil
  stillSince = {}
  hazardSince = {}
  seenAt = {}
  lastTeleport = {}
  cuts = {}
  M.restoreChoice()
  stats = { applies = 0, lastMs = 0, nodes = 0, edges = 0 }
  prep, waited = nil, 0
end

return M
