-- Synchronise tyre rotation with BeamNG.Earth's one-shot spawn velocity boost.
-- (From PR #728 by ProxyFirewall202, "Adaptive Traffic Density and AI Safety Improvements": this module as written
-- there; navgraph.lua's respawn launch calls it. Ported with the respawn speed, 2026-11-05.)
--
-- BeamNG does not expose a supported setter for soft-body node velocities.  The vehicle VM
-- does expose per-node force application, node masses/positions/velocities, and pressure-wheel
-- tread-node IDs.  We use those APIs to apply a one-physics-step tangential impulse to each
-- tyre's tread nodes after thrusters.applyVelocity has applied the vehicle's linear impulse.
--
-- The native vehicle AI, powertrain, brakes, steering, ABS and obstacle avoidance remain intact.

local M = {}

local MIN_NODE_RADIUS_RATIO = 0.35
local MAX_NODE_RADIUS_RATIO = 1.60
local MIN_WHEEL_NODES = 6
local MIN_DELTA_V = 0.02
local VECTOR_EPSILON = 0.0001

local function finite(n)
  return type(n) == "number" and n == n and n ~= math.huge and n ~= -math.huge
end

local function xyz(value, y, z)
  if type(value) == "number" then return value, y, z end
  if value ~= nil then return value.x, value.y, value.z end
  return nil, nil, nil
end

local function readNodePosition(nodeId)
  local ok, a, b, c = pcall(obj.getNodePosition, obj, nodeId)
  if not ok then return nil end
  local x, y, z = xyz(a, b, c)
  if not (finite(x) and finite(y) and finite(z)) then return nil end
  return x, y, z
end

local function readNodeVelocity(nodeId)
  local ok, a, b, c = pcall(obj.getNodeVelocityVectorXYZ, obj, nodeId)
  if not ok then return nil end
  local x, y, z = xyz(a, b, c)
  if not (finite(x) and finite(y) and finite(z)) then return nil end
  return x, y, z
end

local function cross(ax, ay, az, bx, by, bz)
  return ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx
end

local function dot(ax, ay, az, bx, by, bz)
  return ax * bx + ay * by + az * bz
end

local function addNodeIds(result, seen, source)
  if type(source) ~= "table" then return end
  for key, value in pairs(source) do
    local nodeId
    if type(value) == "number" or type(value) == "string" then
      nodeId = tonumber(value)
    elseif type(value) == "table" then
      nodeId = tonumber(value.cid or value.nodeId or value.id)
    end
    -- Also support node-ID keyed sets such as { [cid] = true }, without mistaking
    -- an array index for a node ID when an entry contains an unresolved node name.
    if not nodeId and type(value) == "boolean" then nodeId = tonumber(key) end
    if nodeId and nodeId >= 0 and nodeId % 1 == 0 and not seen[nodeId] then
      seen[nodeId] = true
      result[#result + 1] = nodeId
    end
  end
end

local function getVehicleWheelData(wheelId, wheel)
  if not (v and v.data and type(v.data.wheels) == "table") then return nil end
  local dataWheels = v.data.wheels
  local data = dataWheels[wheelId]
  if data then return data end
  if wheel and wheel.name then
    data = dataWheels[wheel.name]
    if data then return data end
    for _, candidate in pairs(dataWheels) do
      if type(candidate) == "table" and candidate.name == wheel.name then
        return candidate
      end
    end
  end
  return nil
end

local function collectWheelNodes(wheelId, wheel)
  local result, seen = {}, {}
  local dataWheel = getVehicleWheelData(wheelId, wheel)

  -- `v.data.wheels[id].treadNodes` is the wheel generator's pressure-wheel node list.
  -- The other fields are optional aliases used by some vehicle/controller implementations.
  addNodeIds(result, seen, wheel.nodes)
  addNodeIds(result, seen, wheel.treadNodes)
  addNodeIds(result, seen, wheel.hubNodes)
  addNodeIds(result, seen, wheel.wheelNodes)
  if dataWheel then
    addNodeIds(result, seen, dataWheel.treadNodes)
    addNodeIds(result, seen, dataWheel.nodes)
    addNodeIds(result, seen, dataWheel.hubNodes)
    addNodeIds(result, seen, dataWheel.wheelNodes)
  end
  return result
end

local function getPhysicsFrequency()
  -- `physicsFPS` is available in vehicle Lua; 2000 Hz is BeamNG's standard physics rate.
  local frequency = tonumber(physicsFPS)
  if not finite(frequency) or frequency < 100 or frequency > 10000 then
    frequency = 2000
  end
  return frequency
end

-- Highest forward gear whose engine angular velocity sits in [minAV, maxAV], or the gear
-- closest to that band when every gear is outside it. Index 0 and non-positive ratios are
-- neutral / reverse and are ignored. clampedAV is the engine speed limited to the band.
function M.pickGear(gearRatios, diffRatio, wheelRadius, speedMps, minAV, maxAV)
  if type(gearRatios) ~= "table" then return nil, nil end
  speedMps = tonumber(speedMps)
  if not finite(speedMps) or speedMps <= 0 then return nil, nil end
  diffRatio = tonumber(diffRatio)
  if not finite(diffRatio) or diffRatio <= 0 then diffRatio = 1 end
  minAV = tonumber(minAV) or 140
  maxAV = tonumber(maxAV) or 440
  if not (finite(minAV) and finite(maxAV)) then return nil, nil end
  if maxAV < minAV then minAV, maxAV = maxAV, minAV end
  local radius = math.max(tonumber(wheelRadius) or 0.2, 0.2)
  local wheelAV = speedMps / radius
  local bestIdx, bestAV, bestDist, maxI = nil, nil, math.huge, 0
  for k, v in pairs(gearRatios) do
    if type(k) == "number" and k >= 1 and k > maxI and tonumber(v) and tonumber(v) > 0 then
      maxI = k
    end
  end
  for i = 1, maxI do
    local ratio = tonumber(gearRatios[i])
    if ratio and ratio > 0 then
      local engineAV = wheelAV * ratio * diffRatio
      local dist
      if engineAV < minAV then dist = minAV - engineAV
      elseif engineAV > maxAV then dist = engineAV - maxAV
      else dist = 0 end
      if dist < bestDist - 1e-6 or (math.abs(dist - bestDist) <= 1e-6 and (not bestIdx or i > bestIdx)) then
        bestDist = dist
        bestIdx = i
        bestAV = math.max(minAV, math.min(maxAV, engineAV))
      end
    end
  end
  if not bestIdx then return nil, nil end
  return bestIdx, bestAV
end

local function graphicsDt(v)
  v = tonumber(v)
  if finite(v) and v > 0.0002 and v <= 0.25 then return v end
end

local function findGearbox()
  if not powertrain then return nil end
  if type(powertrain.getDevice) == "function" then
    local gb = powertrain.getDevice("gearbox")
    if type(gb) == "table" then return gb end
  end
  if type(powertrain.getDevicesByType) ~= "function" then return nil end
  for _, kind in ipairs({ "automaticGearbox", "manualGearbox", "dctGearbox", "cvtGearbox" }) do
    local list = powertrain.getDevicesByType(kind)
    if type(list) == "table" then
      local gb = list[1] or list[0]
      if type(gb) == "table" then return gb end
    end
  end
end

local function shiftGear(gb, idx)
  if type(gb) ~= "table" then return false end
  if gb.type == "dctGearbox" and type(gb.setGearIndex1) == "function" then
    return pcall(gb.setGearIndex1, gb, idx)
  end
  if type(gb.setGearIndex) == "function" then
    return pcall(gb.setGearIndex, gb, idx, 0)
  end
  if type(gb.setGearIndex1) == "function" then
    return pcall(gb.setGearIndex1, gb, idx)
  end
  return false
end

local function releaseBrakes(throttle)
  if input and type(input.event) == "function" then
    pcall(input.event, "parkingbrake", 0, "FILTER_DIRECT")
    pcall(input.event, "brake", 0, "FILTER_DIRECT")
    if throttle then pcall(input.event, "throttle", throttle, "FILTER_DIRECT") end
  end
  if electrics and type(electrics.values) == "table" then
    electrics.values.parkingbrake = 0
    electrics.values.brake = 0
    if throttle then electrics.values.throttle = throttle end
  end
end

local function downstreamRatio(gb)
  if type(gb.children) == "table" then
    for _, child in pairs(gb.children) do
      if type(child) == "table" then
        local c = tonumber(child.cumulativeGearRatio)
        if c and c > 0.05 then return c end
      end
    end
  end
  if powertrain and type(powertrain.getDevicesByType) == "function" then
    local diffs = powertrain.getDevicesByType("differential")
    if type(diffs) == "table" then
      local d = diffs[1] or diffs[0]
      if type(d) == "table" then
        local g = tonumber(d.gearRatio)
        if g and g > 0 then return g end
      end
    end
  end
  local gr = tonumber(gb.gearRatio)
  local cum = tonumber(gb.cumulativeGearRatio)
  if gr and math.abs(gr) > 1e-3 and cum and cum > 0 then return math.abs(cum / gr) end
  return nil
end

local function meanWheelRadius()
  if not (wheels and type(wheels.wheels) == "table") then return nil end
  local sum, n = 0, 0
  for _, wheel in pairs(wheels.wheels) do
    local r = type(wheel) == "table" and tonumber(wheel.radius) or nil
    if r and r > 0.05 then sum, n = sum + r, n + 1 end
  end
  if n == 0 then return nil end
  return sum / n
end

local function findEngine()
  if not powertrain then return nil end
  if type(powertrain.getDevice) == "function" then
    local eng = powertrain.getDevice("mainEngine")
    if type(eng) == "table" then return eng end
  end
  if type(powertrain.getDevicesByType) ~= "function" then return nil end
  for _, kind in ipairs({ "combustionEngine", "electricMotor" }) do
    local list = powertrain.getDevicesByType(kind)
    if type(list) == "table" then
      local eng = list[1] or list[0]
      if type(eng) == "table" then return eng end
    end
  end
end

-- Drop the parking brake and service brake, crack the throttle so an automatic does not
-- re-apply its arcade autobrake, and leave the gearbox in neutral before the speed impulse.
function M.prepLaunch()
  releaseBrakes(0.25)
  shiftGear(findGearbox(), 0)
  return "ready"
end

-- Bring the tread nodes' tangential velocity up to the rolling velocity of the body.  Applying
-- a balanced set of tangential forces (net force approximately zero) creates a wheel-spin couple
-- without adding a second forward kick to the vehicle.
-- dtOverride, when it is a graphics-step dt, is the same dt passed to thrusters.applyVelocity.
-- The wheel forces then use applyForceVectorTime over that dt. A one-substep applyForceVector
-- remains the fallback when no graphics dt is available or the timed API is missing.
function M.applyRollingSpeed(requestedTargetMps, dtOverride)
  local targetMps = tonumber(requestedTargetMps)
  if not finite(targetMps) or targetMps <= 0 or targetMps > 80 then
    return "invalid-target"
  end
  if not (obj and type(obj.getDirectionVector) == "function"
      and type(obj.getDirectionVectorUp) == "function"
      and type(obj.getVelocity) == "function"
      and type(obj.getNodeMass) == "function"
      and type(obj.getNodePosition) == "function"
      and type(obj.getNodeVelocityVectorXYZ) == "function"
      and type(obj.applyForceVector) == "function"
      and type(float3) == "function"
      and wheels and type(wheels.wheels) == "table") then
    return "required-node-physics-api-unavailable"
  end

  local dir = obj:getDirectionVector()
  local up = obj:getDirectionVectorUp()
  local dx, dy, dz = dir.x, dir.y, dir.z
  local ux, uy, uz = up.x, up.y, up.z
  local directionLength = math.sqrt(dx * dx + dy * dy + dz * dz)
  local upLength = math.sqrt(ux * ux + uy * uy + uz * uz)
  if directionLength < 0.5 or upLength < 0.5 then return "invalid-vehicle-orientation" end
  dx, dy, dz = dx / directionLength, dy / directionLength, dz / directionLength
  ux, uy, uz = ux / upLength, uy / upLength, uz / upLength

  local bodyVelocity = obj:getVelocity()
  local bvx, bvy, bvz = bodyVelocity.x, bodyVelocity.y, bodyVelocity.z
  if not (finite(bvx) and finite(bvy) and finite(bvz)) then return "invalid-body-velocity" end

  -- The body helper may land a little below the requested target because of its physics-step
  -- timing.  Match the wheels to the actual forward velocity when available, never above target.
  local bodyForwardSpeed = dot(bvx, bvy, bvz, dx, dy, dz)
  local rollingSpeed = targetMps
  if finite(bodyForwardSpeed) and bodyForwardSpeed > math.max(0.5, targetMps * 0.15) then
    rollingSpeed = math.min(targetMps, bodyForwardSpeed)
  end

  local stepSeconds = graphicsDt(dtOverride)
  local usedLong = stepSeconds ~= nil
  if not stepSeconds then
    stepSeconds = graphicsDt(lastDt)
    usedLong = stepSeconds ~= nil
  end
  if not stepSeconds then
    stepSeconds = 1 / getPhysicsFrequency()
    usedLong = false
  end
  local useTimed = usedLong and type(obj.applyForceVectorTime) == "function"
  local wheelCount, nodeCount, failedNodes, skippedWheels = 0, 0, 0, 0
  local maxDeltaV, peakForce = 0, 0

  for wheelId, wheel in pairs(wheels.wheels) do
    if type(wheel) == "table" then
      local radius = tonumber(wheel.radius)
      local node1, node2 = tonumber(wheel.node1), tonumber(wheel.node2)
      if finite(radius) and radius > 0.08 and node1 and node2 then
        local x1, y1, z1 = readNodePosition(node1)
        local x2, y2, z2 = readNodePosition(node2)
        if x1 and x2 then
          local ax, ay, az = x2 - x1, y2 - y1, z2 - z1
          local axisLength = math.sqrt(ax * ax + ay * ay + az * az)
          if axisLength > 0.02 then
            ax, ay, az = ax / axisLength, ay / axisLength, az / axisLength
            local cx, cy, cz = (x1 + x2) * 0.5, (y1 + y2) * 0.5, (z1 + z2) * 0.5
            local wheelNodes = {}
            local bottomRx, bottomRy, bottomRz, bottomScore = nil, nil, nil, math.huge

            for _, nodeId in ipairs(collectWheelNodes(wheelId, wheel)) do
              if nodeId ~= node1 and nodeId ~= node2 then
                local px, py, pz = readNodePosition(nodeId)
                if px then
                  local rx, ry, rz = px - cx, py - cy, pz - cz
                  local axial = dot(rx, ry, rz, ax, ay, az)
                  rx, ry, rz = rx - ax * axial, ry - ay * axial, rz - az * axial
                  local radialLength = math.sqrt(rx * rx + ry * ry + rz * rz)
                  -- Keep hub/tread nodes which participate in wheel rotation; reject axle nodes.
                  if radialLength > radius * MIN_NODE_RADIUS_RATIO
                      and radialLength < radius * MAX_NODE_RADIUS_RATIO then
                    wheelNodes[#wheelNodes + 1] = {
                      id = nodeId, rx = rx, ry = ry, rz = rz, radialLength = radialLength
                    }
                    local verticalScore = dot(rx, ry, rz, ux, uy, uz)
                    if verticalScore < bottomScore then
                      bottomRx, bottomRy, bottomRz, bottomScore = rx, ry, rz, verticalScore
                    end
                  end
                end
              end
            end

            if #wheelNodes >= MIN_WHEEL_NODES and bottomRx then
              -- Select spin sign so the bottom-of-tyre tangent points in the vehicle's forward
              -- direction, independent of whether this wheel's axle nodes are ordered L->R or R->L.
              local bottomTx, bottomTy, bottomTz = cross(ax, ay, az, bottomRx, bottomRy, bottomRz)
              local spinSign = dot(bottomTx, bottomTy, bottomTz, dx, dy, dz) >= 0 and 1 or -1
              local forceRows, sumFx, sumFy, sumFz = {}, 0, 0, 0

              for _, node in ipairs(wheelNodes) do
                local tx, ty, tz = cross(ax, ay, az, node.rx, node.ry, node.rz)
                local tangentLength = math.sqrt(tx * tx + ty * ty + tz * tz)
                if tangentLength > VECTOR_EPSILON then
                  tx, ty, tz = tx / tangentLength * spinSign,
                    ty / tangentLength * spinSign, tz / tangentLength * spinSign
                  local nvx, nvy, nvz = readNodeVelocity(node.id)
                  local massOk, mass = pcall(obj.getNodeMass, obj, node.id)
                  mass = massOk and tonumber(mass) or nil
                  if nvx and finite(mass) and mass > 0 then
                    local currentTangential = dot(nvx - bvx, nvy - bvy, nvz - bvz, tx, ty, tz)
                    local desiredTangential = rollingSpeed * (node.radialLength / radius)
                    local deltaV = desiredTangential - currentTangential
                    local fx, fy, fz = 0, 0, 0
                    if math.abs(deltaV) > MIN_DELTA_V then
                      local magnitude = mass * deltaV / stepSeconds
                      fx, fy, fz = tx * magnitude, ty * magnitude, tz * magnitude
                      if math.abs(deltaV) > maxDeltaV then maxDeltaV = math.abs(deltaV) end
                    end
                    forceRows[#forceRows + 1] = { id = node.id, x = fx, y = fy, z = fz }
                    sumFx, sumFy, sumFz = sumFx + fx, sumFy + fy, sumFz + fz
                  else
                    failedNodes = failedNodes + 1
                  end
                end
              end

              if #forceRows >= MIN_WHEEL_NODES then
                -- Remove the mean force from the ring so the rotational impulse has no net
                -- translation component even when node spacing/masses are uneven.
                local meanFx, meanFy, meanFz = sumFx / #forceRows, sumFy / #forceRows, sumFz / #forceRows
                local appliedOnWheel = 0
                for _, row in ipairs(forceRows) do
                  local fx, fy, fz = row.x - meanFx, row.y - meanFy, row.z - meanFz
                  local forceLength = math.sqrt(fx * fx + fy * fy + fz * fz)
                  if forceLength > VECTOR_EPSILON then
                    local ok
                    if useTimed then
                      ok = pcall(obj.applyForceVectorTime, obj, row.id, float3(fx, fy, fz), stepSeconds)
                    else
                      ok = pcall(obj.applyForceVector, obj, row.id, float3(fx, fy, fz))
                    end
                    if ok then
                      appliedOnWheel = appliedOnWheel + 1
                      if forceLength > peakForce then peakForce = forceLength end
                    else
                      failedNodes = failedNodes + 1
                    end
                  end
                end
                if appliedOnWheel > 0 then
                  wheelCount = wheelCount + 1
                  nodeCount = nodeCount + appliedOnWheel
                else
                  skippedWheels = skippedWheels + 1
                end
              else
                skippedWheels = skippedWheels + 1
              end
            else
              skippedWheels = skippedWheels + 1
            end
          else
            skippedWheels = skippedWheels + 1
          end
        else
          skippedWheels = skippedWheels + 1
        end
      else
        skippedWheels = skippedWheels + 1
      end
    end
  end

  if wheelCount == 0 or nodeCount == 0 then
    return string.format("no-wheel-nodes (failed=%d skipped=%d)", failedNodes, skippedWheels)
  end
  return string.format(
    "applied wheels=%d nodes=%d rollingSpeed=%.1fm/s maxNodeDeltaV=%.1fm/s peakNodeForce=%.0fN failed=%d skipped=%d",
    wheelCount, nodeCount, rollingSpeed, maxDeltaV, peakForce, failedNodes, skippedWheels)
end

-- One graphics dt for the chassis velocity and the wheel spin, then a gear and engine speed
-- that match targetMps so the driven wheels are not shoved against a braked low gear.
function M.launchAtSpeed(targetMps, objectId)
  targetMps = tonumber(targetMps)
  if not finite(targetMps) or targetMps <= 0 or targetMps > 80 then return "invalid-target" end
  local dt = math.min(0.1, math.max(0.002, tonumber(lastDt) or 0.02))
  if not finite(dt) then dt = 0.02 end
  if obj and type(obj.getDirectionVector) == "function" and thrusters
      and type(thrusters.applyVelocity) == "function" and type(float3) == "function" then
    local dir = obj:getDirectionVector()
    if dir and finite(dir.x) and finite(dir.y) and finite(dir.z) then
      pcall(thrusters.applyVelocity, float3(dir.x * targetMps, dir.y * targetMps, dir.z * targetMps), dt)
    end
  end
  local roll = M.applyRollingSpeed(targetMps, dt)
  releaseBrakes(0) -- also drops the throttle prepLaunch cracked, so nothing lingers if the AI does not take over
  if wheels and type(wheels.wheels) == "table" then
    for _, wheel in pairs(wheels.wheels) do
      if type(wheel) == "table" and tonumber(wheel.radius) and tonumber(wheel.radius) > 0.08
          and wheel.angularVelocity ~= nil then
        local wdir = tonumber(wheel.wheelDir)
        if not finite(wdir) or wdir == 0 then wdir = 1 end
        pcall(function() wheel.angularVelocity = (targetMps / tonumber(wheel.radius)) * wdir end)
      end
    end
  end
  local gb = findGearbox()
  local gearIdx, engAV = nil, nil
  local downstream = gb and downstreamRatio(gb) or nil
  if gb and downstream and type(gb.gearRatios) == "table" then
    gearIdx, engAV = M.pickGear(gb.gearRatios, downstream, meanWheelRadius() or 0.2,
      targetMps, 140, 440)
    if gearIdx then shiftGear(gb, gearIdx) end
  end
  local eng = findEngine()
  if eng and engAV then
    pcall(function() eng.outputAV1 = engAV end)
  end
  return string.format("launched dt=%.4f gear=%s av=%.2f id=%s roll=%s",
    dt, tostring(gearIdx), engAV or 0, tostring(objectId), tostring(roll))
end

-- Report body speed, mean wheel speed, and the brake pedals back to the GE navgraph.
-- The callback goes through package.loaded because navgraph is required, not extensions.*.
function M.probeSettle(targetMps, objectId)
  if not (obj and type(obj.queueGameEngineLua) == "function" and type(obj.getDirectionVector) == "function"
      and type(obj.getVelocity) == "function") then
    return "no-probe-api"
  end
  local dir = obj:getDirectionVector()
  local vel = obj:getVelocity()
  local bodyMps = 0
  if dir and vel and finite(dir.x) and finite(dir.y) and finite(dir.z)
      and finite(vel.x) and finite(vel.y) and finite(vel.z) then
    local dl = math.sqrt(dir.x * dir.x + dir.y * dir.y + dir.z * dir.z)
    if dl > 1e-6 then
      bodyMps = (vel.x * dir.x + vel.y * dir.y + vel.z * dir.z) / dl
    end
  end
  local wheelMps, maxSlip, sum, n = bodyMps, 0, 0, 0
  if wheels and type(wheels.wheels) == "table" then
    for _, wheel in pairs(wheels.wheels) do
      if type(wheel) == "table" then
        local av, radius = tonumber(wheel.angularVelocity), tonumber(wheel.radius)
        if finite(av) and finite(radius) then
          local ws = math.abs(av * radius)
          sum, n = sum + ws, n + 1
          local sl = math.abs(ws - math.abs(bodyMps)) -- wheel speed is unsigned, so compare against |body|
          if sl > maxSlip then maxSlip = sl end
        end
      end
    end
    if n > 0 then wheelMps = bodyMps + maxSlip end
  end
  local values = electrics and electrics.values or nil
  local pbrake = values and tonumber(values.parkingbrake) or 0
  local brake = values and tonumber(values.brake) or 0
  if not finite(pbrake) then pbrake = 0 end
  if not finite(brake) then brake = 0 end
  if not finite(wheelMps) then wheelMps = bodyMps end
  objectId = tonumber(objectId)
  if not objectId and type(obj.getID) == "function" then
    local ok, id = pcall(obj.getID, obj)
    if ok then objectId = tonumber(id) end
  end
  objectId = objectId or -1
  local cmd = string.format(
    'local ng = package.loaded["beamngEarth/navgraph"]; if ng and ng.onLaunchReport then ng.onLaunchReport(%d, %.4f, %.4f, %.4f, %.4f) end',
    objectId, bodyMps, wheelMps, pbrake or 0, brake or 0)
  obj:queueGameEngineLua(cmd)
  return cmd
end

return M
