-- 1-hotkey silent reporter for BeamNG.Earth - Traffic and AutoDrive fixes (Ctrl+Shift+T).
-- Captures anonymized coordinates, driving/traffic telemetry, and screenshot locally, notifies the
-- patched earthd server to publish cleanly to the collector, and flashes a subtle non-blocking toast.
local M = {}

M.LEVEL_ID = 'beamng_earth'
M.ADDON_ID = 'beamng-earth-autodrive-fixes'
M.ADDON_VERSION = '0.1.2'
M.DIR = 'beamngEarth/traffic_autodrive_reports/'
M.POLL_TIMEOUT_S = 22
M.FIX_NOTIFY_SETTLE_S = 3.0
M.FIX_TOAST_DURATION_S = 14.0
M.UPDATE_TOAST_DURATION_S = 14.0
M.PENDING_FIXES_FILE = M.DIR .. 'pending_fix_notifications.json'
M.PENDING_UPDATE_FILE = M.DIR .. 'pending_update_notification.json'
M.DISMISSED_UPDATE_FILE = M.DIR .. 'dismissed_update.json'
M.ALLOWED_ISSUE_URL_PREFIX = 'https://github.com/CodeWMLandfill/BeamNG-Earth-Traffic-and-AutoDrive-Fixes/issues/'
M.ALLOWED_RELEASE_URL_PREFIX = 'https://github.com/CodeWMLandfill/BeamNG-Earth-Traffic-and-AutoDrive-Fixes/releases/'
M.ADDON_OPTION_IDS = {
  'beamng-earth-autodrive-fixes',
  'beamng-earth-traffic-and-autod',
  'beamng-earth-traffic-and-autodrive-fixes',
}

local im = nil
local ourLevel = false
local seq = 0
local pending = nil      -- { rel = ..., kind = ..., deadline = ... }
local toast, toastUntil = nil, 0
local delayedToast, delayedAt = nil, nil
local readySince = nil
local fixCheckAttempts = 0
local nextFixCheckAt = 0
local fixToast = nil     -- { items = { ... }, untilT = ... }
local updateCheckAttempts = 0
local nextUpdateCheckAt = 0
local updateToast = nil  -- { latestVersion = ..., currentVersion = ..., tagName = ..., releaseUrl = ..., untilT = ... }

local wallMs = require('beamngEarth/stopwatch').clock()
local function now() return wallMs() / 1000 end

local function round2(v)
  local n = tonumber(v)
  if not n or n ~= n or n == math.huge or n == -math.huge then return nil end
  return math.floor(n * 100 + 0.5) / 100
end

local function onLevel()
  ourLevel = rawget(_G, 'getCurrentLevelIdentifier') ~= nil and getCurrentLevelIdentifier() == M.LEVEL_ID
  readySince = nil
  fixCheckAttempts = 0
  nextFixCheckAt = 0
  updateCheckAttempts = 0
  nextUpdateCheckAt = 0
end

M.onClientStartMission = onLevel
function M.onClientEndMission()
  ourLevel = false
  readySince = nil
  fixToast = nil
  updateToast = nil
end
function M.onExtensionLoaded() if scenetree and scenetree.MissionGroup then onLevel() end end

local function showToast(msg, dur)
  toast = tostring(msg)
  toastUntil = now() + (dur or 4)
end

local function addonOptions()
  local api = rawget(_G, 'beamngEarth_api')
  if api and type(api.addonOptions) == 'function' then
    for _, id in ipairs(M.ADDON_OPTION_IDS) do
      local ok, opts = pcall(api.addonOptions, id)
      if ok and type(opts) == 'table' and next(opts) ~= nil then return opts end
    end
    local ok, opts = pcall(api.addonOptions, M.ADDON_ID)
    if ok and type(opts) == 'table' then return opts end
  end
  return {}
end

local function sanitizeCategory(v)
  if v == 'autodrive' or v == 'traffic' or v == 'overtake' or v == 'routing' or v == 'other' then return v end
  return 'autodrive'
end

M._sanitizeCategory = sanitizeCategory

local function loadPairCounts(p)
  if type(p) ~= 'table' then return false, 0, 0 end
  local done, want = tonumber(p[1]) or 0, tonumber(p[2]) or 0
  return (want > 0 and done < want), done, want
end

-- Guard genuine mid-teleport black frames and unrendered world states
-- while keeping 0 per-cell cooldowns and allowing rendered 98-99% background streaming reports.
local function blackFrameBlocker(core, stStatus)
  if type(stStatus) == 'table' then
    if stStatus.inOurLevel == false or stStatus.active == false then
      return 'Wait for world to activate before reporting'
    end
    if stStatus.terrainRendered == false then
      return 'Wait for terrain rendering before reporting'
    end
    if stStatus.anchorId == nil then
      return 'Wait for world anchor before reporting'
    end
    if stStatus.groundReady == false or stStatus.loading == true then
      return 'Wait for terrain to finish loading before reporting'
    end
  end

  if core then
    if type(core._loadCover) == 'function' then
      local okC, cover = pcall(core._loadCover)
      if okC and cover == true then
        return 'Wait for teleport loading cover before reporting'
      end
    end
    if type(core.heldReason) == 'function' then
      local okH, held = pcall(core.heldReason)
      if okH and (held == 'loading' or held == 'ground') then
        return 'Wait for teleport terrain before reporting'
      end
    end
    if type(core.groundReady) == 'function' then
      local okG, ready = pcall(core.groundReady)
      if okG and ready == false then
        return 'Wait for terrain to finish loading before reporting'
      end
    end
    local rawSt = nil
    if type(core._state) == 'function' then
      local okR, r = pcall(core._state)
      if okR and type(r) == 'table' then rawSt = r end
    end
    if rawSt then
      if rawSt.inOurLevel == false or rawSt.active == false then
        return 'Wait for world to activate before reporting'
      end
      local pend = (type(rawSt.pending) == 'table' and rawSt.pending.purpose)
        or (type(stStatus) == 'table' and stStatus.pending)
      if pend == 'teleport' or pend == 'resync' or rawSt.loadingFault ~= nil then
        return 'Wait for teleport terrain before reporting'
      end
      if rawSt.placeFrames ~= nil or rawSt.teleportRetry ~= nil
          or rawSt.floorZ ~= nil or rawSt.randomStart ~= nil
          or rawSt.anchor == nil or rawSt.block == nil then
        return 'Wait for terrain placement before reporting'
      end
      if type(rawSt.clock) == 'number' and type(rawSt.loadingUntil) == 'number'
          and rawSt.clock < rawSt.loadingUntil then
        return 'Wait for teleport loading cover before reporting'
      end
    elseif type(stStatus) == 'table' and (stStatus.pending == 'teleport' or stStatus.pending == 'resync') then
      return 'Wait for teleport terrain before reporting'
    end
  end
  return nil
end

M._blackFrameBlocker = blackFrameBlocker

local function inspectLoadingState(core, stStatus, anchorId)
  local reasons = {}
  local details = {}

  if core then
    if type(core.groundReady) == 'function' then
      local okG, ready = pcall(core.groundReady)
      if okG and ready == false then
        reasons[#reasons + 1] = 'ground_not_ready'
        details.groundReady = false
      end
    end
    local rawSt = nil
    if type(core._state) == 'function' then
      local okR, r = pcall(core._state)
      if okR and type(r) == 'table' then rawSt = r end
    end
    local pend = (rawSt and type(rawSt.pending) == 'table' and rawSt.pending.purpose)
      or (type(stStatus) == 'table' and stStatus.pending)
    if pend == 'teleport' or pend == 'resync' then
      reasons[#reasons + 1] = 'teleport_pending'
      details.pendingPurpose = tostring(pend)
    end
    if rawSt and (rawSt.placeFrames ~= nil or rawSt.teleportRetry ~= nil
        or (rawSt.anchor ~= nil and rawSt.block == nil)) then
      reasons[#reasons + 1] = 'terrain_placement_pending'
      details.placementPending = true
    end
    local blk = (rawSt and rawSt.block) or (type(stStatus) == 'table' and stStatus.block)
    if type(blk) == 'table' and blk.provisional == true then
      reasons[#reasons + 1] = 'provisional_block'
      details.provisionalBlock = true
    end
    local lp = rawSt and type(rawSt.loadProgress) == 'table' and rawSt.loadProgress or nil
    if lp and (anchorId == nil or lp.anchorId == nil or lp.anchorId == anchorId) then
      local gPend, gDone, gWant = loadPairCounts(lp.ground)
      if gPend then
        reasons[#reasons + 1] = 'graded_first_block_pending'
        details.groundDone = gDone
        details.groundWant = gWant
      end
      local rPend, rDone, rWant = loadPairCounts(lp.roads)
      if rPend then
        reasons[#reasons + 1] = 'roads_pending'
        details.roadsDone = rDone
        details.roadsWant = rWant
      end
    end
  end

  local okLp, loadprogress = pcall(require, 'beamngEarth/loadprogress')
  if okLp and type(loadprogress) == 'table' and type(loadprogress.active) == 'function' then
    local okAct, active = pcall(loadprogress.active)
    if okAct and active == true then
      reasons[#reasons + 1] = 'world_load_active'
      details.worldLoadActive = true
    end
  end

  local okRd, roadsMod = pcall(require, 'beamngEarth/roads')
  if okRd and type(roadsMod) == 'table' and type(roadsMod.queued) == 'function' then
    local okQ, q = pcall(roadsMod.queued)
    if okQ and type(q) == 'number' and q > 0 then
      reasons[#reasons + 1] = 'queued_road_tiles'
      details.roadsQueued = q
    end
  end

  if #reasons == 0 then
    return false, nil, nil
  end
  local reason = table.concat(reasons, ',')
  details.reason = reason
  return true, reason, details
end

M._inspectLoadingState = inspectLoadingState

-- Collect anonymized driving, autodrive, road-edge, tune, overtake, and nearby-traffic snapshot.
-- Never includes vehicle IDs, model names, license plates, or filesystem paths.
local function collectDrivingTelemetry(veh)
  local snap = {}
  local okGps, gps = pcall(require, 'beamngEarth/gps')
  if not okGps then gps = nil end

  pcall(function()
    local ad = {}
    if gps then
      if type(gps.autodrive) == 'function' then ad.active = gps.autodrive() and true or false end
      if type(gps.autodriveMode) == 'function' then
        local m = gps.autodriveMode()
        if type(m) == 'string' then ad.mode = m:sub(1, 16) end
      end
      if type(gps.status) == 'function' then
        local st = gps.status()
        if type(st) == 'table' and type(st.remaining) == 'number' then
          ad.remainingM = round2(st.remaining)
        end
      end
    end
    local gm = rawget(_G, 'core_groundMarkers')
    local rp = gm and gm.routePlanner
    if rp and type(rp.path) == 'table' then
      local nilCount = 0
      for _, wp in ipairs(rp.path) do
        if type(wp) == 'table' and wp.wp == nil then nilCount = nilCount + 1 end
      end
      ad.nilWps = nilCount
    end
    if next(ad) ~= nil then snap.autodrive = ad end
  end)

  local px, py, pz, fx, fy = nil, nil, nil, 1, 0
  local playerVehId = nil
  pcall(function()
    if veh then
      if type(veh.getId) == 'function' then playerVehId = veh:getId() end
      if type(veh.getID) == 'function' and playerVehId == nil then playerVehId = veh:getID() end
      local p = veh.getPosition and veh:getPosition()
      local d = veh.getDirectionVector and veh:getDirectionVector()
      if p then px, py, pz = tonumber(p.x), tonumber(p.y), tonumber(p.z) or 0 end
      if d then
        local dl = math.sqrt((tonumber(d.x) or 0) ^ 2 + (tonumber(d.y) or 0) ^ 2)
        if dl > 1e-6 then fx, fy = d.x / dl, d.y / dl end
      end
    end
    local beObj = rawget(_G, 'be')
    if playerVehId == nil and beObj and type(beObj.getPlayerVehicleID) == 'function' then
      playerVehId = beObj:getPlayerVehicleID(0)
    end
  end)

  pcall(function()
    local mapApi = rawget(_G, 'map')
    local mapData = mapApi and type(mapApi.getMap) == 'function' and mapApi.getMap()
    local nodes = mapData and mapData.nodes
    if nodes and px and py and gps and type(gps.edgeUnder) == 'function' then
      local a, b = gps.edgeUnder(nodes, px, py, fx, fy, pz)
      local na, nb = a and nodes[a], b and nodes[b]
      local e = na and na.links and na.links[b]
      if na and nb and e then
        local dx, dy = nb.pos.x - na.pos.x, nb.pos.y - na.pos.y
        local lenSq = dx * dx + dy * dy
        local len = math.sqrt(lenSq)
        local t = lenSq > 1e-6 and math.max(0, math.min(1, ((px - na.pos.x) * dx + (py - na.pos.y) * dy) / lenSq)) or 0
        local ra = (e.inNode == a and e.inRadius or e.outRadius) or na.radius or 0
        local rb = (e.inNode == a and e.outRadius or e.inRadius) or nb.radius or ra
        local halfM = ra + t * (rb - ra)
        local latOffM = len > 1e-6 and (((px - na.pos.x) * dy - (py - na.pos.y) * dx) / len) or 0
        snap.roadEdge = {
          oneway = e.oneWay and true or false,
          lanes = type(e.lanes) == 'string' and e.lanes:sub(1, 16) or nil,
          halfM = round2(halfM),
          latOffM = round2(latOffM),
          limitMs = type(e.speedLimit) == 'number' and round2(e.speedLimit) or nil,
        }
      end
    end
  end)

  pcall(function()
    local settings = rawget(_G, 'beamngEarth_settings')
    if not settings then
      local okS, modS = pcall(require, 'beamngEarth/settings')
      if okS then settings = modS end
    end
    if type(settings) == 'table' then
      local pub = type(settings.tunePublic) == 'function' and settings.tunePublic() or nil
      local full = type(settings.autodriveTune) == 'function' and settings.autodriveTune() or nil
      local avoid = type(settings.get) == 'function' and settings.get('routeAvoidHighways') or false
      if type(pub) == 'table' or type(full) == 'table' then
        local src = pub or full
        snap.tune = {
          speedMph = round2(src.speedMph),
          mode = (full and type(full.speedMode) == 'string') and full.speedMode:sub(1, 16) or nil,
          aggression = round2(src.aggression),
          pass = src.pass and true or false,
          awareness = round2(src.awareness),
          avoidHighways = avoid and true or false,
        }
      end
    end
  end)

  pcall(function()
    local okOv, ov = pcall(require, 'beamngEarth/overtake')
    if okOv and type(ov) == 'table' and type(ov.status) == 'function' then
      local st = ov.status(playerVehId)
      if type(st) == 'table' then
        snap.overtake = {
          passes = type(st.passes) == 'number' and math.floor(st.passes) or 0,
          active = st.active ~= nil and (st.active and true or false) or nil,
          blocked = st.blocked ~= nil and (st.blocked and true or false) or nil,
          targetLane = type(st.targetLane) == 'number' and math.floor(st.targetLane) or nil,
          retries = type(st.retries) == 'number' and math.floor(st.retries) or nil,
        }
      end
    end
  end)

  pcall(function()
    local gt = rawget(_G, 'gameplay_traffic')
    local getObj = rawget(_G, 'getObjectByID')
    if not (gt and type(gt.getTrafficData) == 'function' and type(getObj) == 'function' and px and py) then
      return
    end
    local tdata = gt.getTrafficData()
    if type(tdata) ~= 'table' then return end
    local cands = {}
    for id, td in pairs(tdata) do
      if id ~= playerVehId and type(td) == 'table' and td.isAi then
        local o = getObj(id)
        if o and (type(o.getActive) ~= 'function' or o:getActive()) and type(o.getPosition) == 'function' then
          local op = o:getPosition()
          if op and type(op.x) == 'number' and type(op.y) == 'number' then
            local dx, dy = op.x - px, op.y - py
            local dz = (tonumber(op.z) or 0) - (pz or 0)
            local dSq = dx * dx + dy * dy + dz * dz
            if dSq <= 250 * 250 then
              local sp = 0
              if type(o.getVelocity) == 'function' then
                local ov = o:getVelocity()
                if ov then
                  if type(ov.length) == 'function' then
                    sp = ov:length()
                  else
                    sp = math.sqrt((tonumber(ov.x) or 0) ^ 2 + (tonumber(ov.y) or 0) ^ 2 + (tonumber(ov.z) or 0) ^ 2)
                  end
                end
              end
              local ahead = dx * fx + dy * fy
              local side = dx * fy - dy * fx
              cands[#cands + 1] = {
                dSq = dSq,
                entry = {
                  aheadM = round2(ahead),
                  sideM = round2(side),
                  speedMs = round2(sp),
                  state = type(td.state) == 'string' and td.state:sub(1, 24) or nil,
                  alpha = type(td.alpha) == 'number' and round2(td.alpha) or nil,
                },
              }
            end
          end
        end
      end
    end
    table.sort(cands, function(a, b) return a.dSq < b.dSq end)
    local out = {}
    for i = 1, math.min(6, #cands) do
      out[i] = cands[i].entry
    end
    if #out > 0 then snap.nearbyTraffic = out end
  end)

  if next(snap) == nil then return nil end
  return snap
end

M._collectDrivingTelemetry = collectDrivingTelemetry

local function isAllowedIssueUrl(url)
  if type(url) ~= 'string' then return false end
  local pre = M.ALLOWED_ISSUE_URL_PREFIX
  if url:sub(1, #pre) ~= pre then return false end
  local tail = url:sub(#pre + 1)
  return tail ~= '' and tail:match('^%d+$') ~= nil
end

local function isAllowedReleaseUrl(url)
  if type(url) ~= 'string' then return false end
  local pre = M.ALLOWED_RELEASE_URL_PREFIX
  if url:sub(1, #pre) ~= pre then return false end
  local tail = url:sub(#pre + 1)
  return tail ~= '' and not url:match('%s')
end

local function normalizeVer(v)
  if type(v) ~= 'string' then return nil end
  local s = v:gsub('^v', '')
  if s:match('^%d+%.%d+%.%d+[A-Za-z0-9%.%-]*$') then return s end
  return nil
end

local function readDismissedUpdateVersion()
  if not FS or not FS:fileExists(M.DISMISSED_UPDATE_FILE) then return nil end
  local txt = readFile(M.DISMISSED_UPDATE_FILE)
  local okD, doc = pcall(jsonDecode, txt or '')
  if okD and type(doc) == 'table' then
    return normalizeVer(tostring(doc.skippedVersion or ''))
  end
  return nil
end

local function writeDismissedUpdateVersion(ver)
  local norm = normalizeVer(ver)
  if not norm then return end
  local okEnc, encoded = pcall(jsonEncode, {
    skippedVersion = norm,
    skippedAt = os.time(),
  })
  if okEnc and encoded then
    pcall(writeFile, M.DISMISSED_UPDATE_FILE, encoded)
  end
end

local function clearPendingFile(path, emptyJson)
  if FS and type(FS.removeFile) == 'function' then
    pcall(FS.removeFile, FS, path)
  end
  if FS and FS:fileExists(path) then
    pcall(writeFile, path, emptyJson)
  end
end

local function isSafeReportDir(dir)
  if type(dir) ~= 'string' then return false end
  if dir:sub(1, #M.DIR) ~= M.DIR then return false end
  if dir:find('..', 1, true) or dir:find('\\', 1, true) then return false end
  return dir:sub(-1) == '/'
end

local function markItemNotified(item)
  if type(item) ~= 'table' then return end
  local dirs = {}
  local seen = {}
  if isSafeReportDir(item.dir) then
    dirs[#dirs + 1] = item.dir
    seen[item.dir] = true
  end
  if type(item.dirs) == 'table' then
    for _, d in ipairs(item.dirs) do
      if isSafeReportDir(d) and not seen[d] then
        dirs[#dirs + 1] = d
        seen[d] = true
      end
    end
  end
  local okEnc, encoded = pcall(jsonEncode, {
    notifiedAt = os.time(),
    reportNumber = item.issueNumber,
    fixedIn = tostring(item.fixedIn or '0.1.0'),
  })
  if not okEnc or not encoded then return end
  for _, d in ipairs(dirs) do
    pcall(writeFile, d .. 'notified_fixed.json', encoded)
  end
end

local function clearPendingFixesFile()
  clearPendingFile(M.PENDING_FIXES_FILE, '{"version":1,"items":[]}')
end

local function clearPendingUpdateFile()
  clearPendingFile(M.PENDING_UPDATE_FILE, '{}')
end

local function tryLoadUpdateNotification(t)
  if not FS or not FS:fileExists(M.PENDING_UPDATE_FILE) then return false end
  local txt = readFile(M.PENDING_UPDATE_FILE)
  clearPendingUpdateFile()
  local okD, doc = pcall(jsonDecode, txt or '')
  if not okD or type(doc) ~= 'table' then return true end
  local latest = normalizeVer(tostring(doc.latestVersion or doc.tagName or ''))
  local installed = normalizeVer(tostring(doc.currentVersion or M.ADDON_VERSION)) or M.ADDON_VERSION
  if not latest or latest == normalizeVer(M.ADDON_VERSION) then return true end
  if not isAllowedReleaseUrl(doc.releaseUrl) then return true end
  if readDismissedUpdateVersion() == latest then return true end
  updateToast = {
    latestVersion = latest,
    currentVersion = installed,
    tagName = tostring(doc.tagName or ('v' .. latest)),
    releaseUrl = doc.releaseUrl,
    untilT = t + M.UPDATE_TOAST_DURATION_S,
  }
  return true
end

local function tryLoadFixNotifications(t)
  if not FS or not FS:fileExists(M.PENDING_FIXES_FILE) then return false end
  local txt = readFile(M.PENDING_FIXES_FILE)
  local okD, doc = pcall(jsonDecode, txt or '')
  if not okD or type(doc) ~= 'table' or type(doc.items) ~= 'table' then
    clearPendingFixesFile()
    return true
  end

  local valid = {}
  for _, it in ipairs(doc.items) do
    if type(it) == 'table' and type(it.issueNumber) == 'number' and it.issueNumber >= 1
        and isSafeReportDir(it.dir) then
      local num = math.floor(it.issueNumber)
      local already = FS:fileExists(it.dir .. 'notified_fixed.json')
      if not already and type(it.dirs) == 'table' then
        for _, d in ipairs(it.dirs) do
          if isSafeReportDir(d) and FS:fileExists(d .. 'notified_fixed.json') then
            already = true
            break
          end
        end
      end
      if not already then
        local url = isAllowedIssueUrl(it.issueUrl) and it.issueUrl or (M.ALLOWED_ISSUE_URL_PREFIX .. tostring(num))
        local fixedIn = tostring(it.fixedIn or '0.1.0'):gsub('^v', '')
        local lat = (type(it.lat) == 'number' and it.lat >= -90 and it.lat <= 90) and it.lat or nil
        local lon = (type(it.lon) == 'number' and it.lon >= -180 and it.lon <= 180) and it.lon or nil
        valid[#valid + 1] = {
          dir = it.dir,
          dirs = type(it.dirs) == 'table' and it.dirs or { it.dir },
          issueNumber = num,
          fixedIn = fixedIn,
          issueUrl = url,
          lat = lat,
          lon = lon,
          kind = sanitizeCategory(it.kind),
        }
      end
    end
  end

  clearPendingFixesFile()
  if #valid > 0 then
    for _, entry in ipairs(valid) do
      markItemNotified(entry)
    end
    fixToast = {
      items = valid,
      untilT = t + M.FIX_TOAST_DURATION_S,
    }
  end
  return true
end

function M.status()
  local ft = (fixToast and now() < fixToast.untilT and fixToast.items and fixToast.items[1]) or nil
  local ut = (updateToast and now() < updateToast.untilT) and updateToast or nil
  return {
    ourLevel = ourLevel,
    pending = pending ~= nil,
    toast = (toast and now() < toastUntil) and toast or nil,
    fixToast = ft and {
      issueNumber = ft.issueNumber,
      fixedIn = ft.fixedIn,
      issueUrl = ft.issueUrl,
      lat = ft.lat,
      lon = ft.lon,
      kind = ft.kind,
      count = #fixToast.items,
    } or nil,
    updateToast = ut and {
      latestVersion = ut.latestVersion,
      currentVersion = ut.currentVersion,
      tagName = ut.tagName,
      releaseUrl = ut.releaseUrl,
    } or nil,
  }
end

function M.captureSilent()
  if not ourLevel then return false, 'not_in_level' end
  local opts = addonOptions()
  if opts.enableOneHotkeyReport == false then
    local msg = '1-hotkey reporter disabled in Add-on Options'
    showToast(msg, 3)
    return false, msg
  end

  local core = rawget(_G, 'beamngEarth_core')
  local stStatus = nil
  local lat, lon, altM, anchorId, earthVer = nil, nil, nil, nil, nil
  if core and core.status then
    local ok, st = pcall(function() return loadstring('return ' .. core.status())() end)
    if ok and type(st) == 'table' then
      stStatus = st
      if type(st.player) == 'table' then
        lat, lon = st.player.lat, st.player.lon
        altM = st.player.alt or st.player.altM
      end
      anchorId = st.anchorId
    end
  end
  local api = rawget(_G, 'beamngEarth_api')
  if api and type(api.pose) == 'function' then
    local okP, p = pcall(api.pose)
    if okP and type(p) == 'table' then
      lat = lat or p.lat
      lon = lon or p.lon
      altM = altM or p.alt
    end
  end
  if api and type(api.earthVersion) == 'function' then
    local okV, ver = pcall(api.earthVersion)
    if okV and type(ver) == 'string' then earthVer = ver end
  end

  if type(lat) ~= 'number' or type(lon) ~= 'number' then
    local msg = 'Wait for world coordinates before reporting'
    showToast(msg, 3)
    return false, msg
  end

  local blackFrameMsg = blackFrameBlocker(core, stStatus)
  if blackFrameMsg then
    showToast(blackFrameMsg, 3)
    return false, blackFrameMsg
  end

  local loadingIncomplete, loadingReason, loadingDetails = inspectLoadingState(core, stStatus, anchorId)

  local headingDeg, speedMs = nil, nil
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  if veh then
    local v, f = veh:getVelocity(), veh:getDirectionVector()
    speedMs = v and v:length() or nil
    if f then headingDeg = math.deg(math.atan2(f.x, f.y)) % 360 end
  end

  local cam = nil
  if core_camera and core_camera.getPosition then
    local c = core_camera.getPosition()
    local fw = core_camera.getForward and core_camera.getForward()
    if c then
      cam = { pos = { c.x, c.y, c.z }, forward = fw and { fw.x, fw.y, fw.z } or nil }
    end
  end

  local drivingTelemetry = collectDrivingTelemetry(veh)

  seq = seq + 1
  local kind = sanitizeCategory(opts.reportCategory)
  local id = os.date('!%Y%m%d_%H%M%S') .. '_' .. tostring(seq)
  local rel = M.DIR .. id .. '/'
  if not FS:directoryExists(rel) then FS:directoryCreate(rel, true) end

  -- Clear any visible toast and take the screenshot cleanly before drawing status feedback.
  toast, toastUntil = nil, 0
  fixToast = nil
  updateToast = nil
  if type(createScreenshot2) == 'function' then
    pcall(createScreenshot2, { filename = rel .. 'screenshot', writeJPG = true, superSampling = 1 })
  end

  local noteText = 'Captured with Ctrl+Shift+T silent 1-hotkey reporter (Traffic and AutoDrive fixes).'
  if loadingIncomplete and loadingReason then
    noteText = noteText .. ' [loading_incomplete: ' .. loadingReason .. ']'
  end

  local nearestMap = nil
  if (loadingIncomplete and loadingDetails) or drivingTelemetry then
    nearestMap = {}
    if loadingIncomplete and loadingDetails then nearestMap.loading = loadingDetails end
    if drivingTelemetry then nearestMap.drivingTelemetry = drivingTelemetry end
  end

  local payload = {
    addonId = M.ADDON_ID,
    lat = lat,
    lon = lon,
    kind = kind,
    summary = string.format('In-game %s report @ %.5f, %.5f', kind, lat, lon),
    note = noteText,
    earthVersion = earthVer or (core and core.MOD_VERSION) or nil,
    addonVersion = M.ADDON_VERSION,
    headingDeg = headingDeg,
    altM = altM,
    speedMs = speedMs,
    anchorId = anchorId,
    camera = cam,
    drivingTelemetry = drivingTelemetry,
    loading_incomplete = loadingIncomplete and true or nil,
    loading_reason = loadingIncomplete and loadingReason or nil,
    nearest = nearestMap,
  }

  local okEnc, encoded = pcall(jsonEncode, payload)
  if okEnc and encoded then
    pcall(writeFile, rel .. 'report.json', encoded)
  end

  local okNet, net = pcall(require, 'beamngEarth/net')
  if okNet and net and type(net.send) == 'function' and net.isConnected and net.isConnected() then
    net.send({ type = 'traffic_autodrive_report', id = id, rel = rel, payload = payload })
    pending = { rel = rel, kind = kind, deadline = now() + M.POLL_TIMEOUT_S }
    if loadingIncomplete then
      delayedToast = 'Uploading ' .. kind .. ' report (loading incomplete)...'
    else
      delayedToast = 'Uploading ' .. kind .. ' report...'
    end
    delayedAt = now() + 0.15
  else
    delayedToast = 'Saved report locally (world server disconnected)'
    delayedAt = now() + 0.15
  end
  return true
end

local function siblingToastOffset()
  local brf = rawget(_G, 'beamngEarth_bugreport')
  if not brf or type(brf.status) ~= 'function' then return 0 end
  local okS, st = pcall(brf.status)
  if not okS or type(st) ~= 'table' then return 0 end
  local rows = 0
  if st.fixToast then rows = rows + 1 end
  if st.updateToast then rows = rows + 1 end
  if st.toast then rows = rows + 1 end
  return rows * 64
end

function M.onUpdate()
  local t = now()
  if delayedToast and delayedAt and t >= delayedAt then
    showToast(delayedToast, 3)
    delayedToast, delayedAt = nil, nil
  end
  if pending then
    local receipt = pending.rel .. 'uploaded.json'
    if FS and FS:fileExists(receipt) then
      local txt = readFile(receipt)
      local okD, res = pcall(jsonDecode, txt or '')
      if okD and type(res) == 'table' then
        if res.ok then
          showToast(string.format('Sent #%s (%s)', tostring(res.issueNumber or '?'), pending.kind), 5)
        else
          showToast('Saved locally (upload failed: ' .. tostring(res.error or 'error') .. ')', 5)
        end
      else
        showToast('Report uploaded', 4)
      end
      pending = nil
    elseif t > pending.deadline then
      pending = nil
    end
  end

  local opts = addonOptions()
  if opts.notifyFixedReports == false then
    fixToast = nil
  elseif fixToast and t >= fixToast.untilT then
    fixToast = nil
  end
  if opts.notifyAddonUpdates == false then
    updateToast = nil
  elseif updateToast and t >= updateToast.untilT then
    updateToast = nil
  end

  local needFixCheck = (opts.notifyFixedReports ~= false) and (not fixToast) and (fixCheckAttempts < 2)
  local needUpdCheck = (opts.notifyAddonUpdates ~= false) and (not updateToast) and (updateCheckAttempts < 2)
  if ourLevel and (needFixCheck or needUpdCheck) then
    local api = rawget(_G, 'beamngEarth_api')
    local core = rawget(_G, 'beamngEarth_core')
    local worldReady = false
    if api and type(api.ready) == 'function' then
      local okR, r = pcall(api.ready)
      if okR and r == true and blackFrameBlocker(core, nil) == nil then
        worldReady = true
      end
    end
    if not worldReady then
      readySince = nil
    else
      readySince = readySince or t
      if (t - readySince) >= M.FIX_NOTIFY_SETTLE_S then
        if needFixCheck and t >= nextFixCheckAt then
          fixCheckAttempts = fixCheckAttempts + 1
          if tryLoadFixNotifications(t) then
            fixCheckAttempts = 2
          else
            nextFixCheckAt = t + 5.0
          end
        end
        if needUpdCheck and t >= nextUpdateCheckAt then
          updateCheckAttempts = updateCheckAttempts + 1
          if tryLoadUpdateNotification(t) then
            updateCheckAttempts = 2
          else
            nextUpdateCheckAt = t + 5.0
          end
        end
      end
    end
  end

  im = im or rawget(_G, 'ui_imgui')
  if not im then return end

  local noticeFlags = (im.WindowFlags_NoDecoration or 0)
    + (im.WindowFlags_AlwaysAutoResize or 0)
    + (im.WindowFlags_NoSavedSettings or 0)
    + (im.WindowFlags_NoFocusOnAppearing or 0)
    + (im.WindowFlags_NoNav or 0)

  local baseY = 88 + siblingToastOffset()
  local showFix = fixToast and t < fixToast.untilT and fixToast.items and fixToast.items[1]
  if showFix then
    if type(im.SetNextWindowBgAlpha) == 'function' then
      pcall(im.SetNextWindowBgAlpha, 0.84)
    end
    im.SetNextWindowPos(im.ImVec2(20, baseY), im.Cond_Always)
    if im.Begin('##bngeTrafficAutodriveFixedToast', nil, noticeFlags) then
      local cur = fixToast.items[1]
      local extra = (#fixToast.items > 1) and string.format(' (+%d more)', #fixToast.items - 1) or ''
      im.TextUnformatted(string.format('Traffic/AutoDrive report #%d (%s) fixed in v%s!%s',
        cur.issueNumber, cur.kind, cur.fixedIn, extra))
      if im.Button('View Report') then
        if isAllowedIssueUrl(cur.issueUrl) then
          local openFn = rawget(_G, 'openWebBrowser')
          if openFn ~= nil then pcall(openFn, cur.issueUrl) end
        end
        fixToast = nil
      end
      if type(cur.lat) == 'number' and type(cur.lon) == 'number' then
        if im.SameLine ~= nil then im.SameLine() end
        if im.Button('Teleport There') then
          local api = rawget(_G, 'beamngEarth_api')
          if api and api.teleport ~= nil then
            pcall(api.teleport, cur.lat, cur.lon, 'Traffic Report #' .. tostring(cur.issueNumber))
          end
          fixToast = nil
        end
      end
      if im.SameLine ~= nil then im.SameLine() end
      if im.Button('Dismiss') then
        fixToast = nil
      end
    end
    im.End()
  end

  local showUpdate = updateToast and t < updateToast.untilT
  if showUpdate then
    if type(im.SetNextWindowBgAlpha) == 'function' then
      pcall(im.SetNextWindowBgAlpha, 0.84)
    end
    local updY = baseY + ((fixToast and t < fixToast.untilT) and 64 or 0)
    im.SetNextWindowPos(im.ImVec2(20, updY), im.Cond_Always)
    if im.Begin('##bngeTrafficAutodriveUpdateToast', nil, noticeFlags) then
      local ut = updateToast
      im.TextUnformatted(string.format('Traffic and AutoDrive fixes v%s available (installed: v%s)',
        ut.latestVersion, ut.currentVersion or M.ADDON_VERSION))
      if im.Button('Get Update') then
        if isAllowedReleaseUrl(ut.releaseUrl) then
          local openFn = rawget(_G, 'openWebBrowser')
          if openFn ~= nil then pcall(openFn, ut.releaseUrl) end
        end
        updateToast = nil
      end
      if im.SameLine ~= nil then im.SameLine() end
      if im.Button('Later') then
        updateToast = nil
      end
      if im.SameLine ~= nil then im.SameLine() end
      if im.Button(string.format('Skip v%s', ut.latestVersion)) then
        writeDismissedUpdateVersion(ut.latestVersion)
        updateToast = nil
      end
    end
    im.End()
  end

  if not toast or t >= toastUntil then return end
  local toastY = baseY + ((updateToast and t < updateToast.untilT) and 128 or ((fixToast and t < fixToast.untilT) and 64 or 62))
  im.SetNextWindowPos(im.ImVec2(20, toastY), im.Cond_Always)
  if im.Begin('##bngeTrafficAutodriveToast', nil,
      im.WindowFlags_NoDecoration + im.WindowFlags_AlwaysAutoResize + im.WindowFlags_NoInputs) then
    im.TextUnformatted(toast)
  end
  im.End()
end

return M
