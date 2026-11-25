"""Self-contained patcher for BeamNG.Earth - Traffic and AutoDrive fixes.

Works against both frozen PyInstaller Windows executables and source checkouts,
delivering the 8 unified Traffic + AutoDrive mod files plus the optional
Ctrl+Shift+T 1-hotkey driving telemetry reporter without overwriting sibling
add-ons (such as Bridge, Rail & Tunnel Fixes on Ctrl+Shift+B).
"""
import argparse
import ast
import ctypes
import dis
import glob
import hashlib
import importlib.util
import json
import marshal
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile
import uuid
import zlib

TAG = "earthAutodriveFixes"
MARKER = "EARTH_AUTODRIVE_FIXES_0_1_0"
VERSION = "0.1.0"
MIN_SUPPORTED_EARTH_VERSION = "v1.9.1-beta.dev.424"
MAX_SOLVER_FAST_VERSION = 621
TIER_FULL_SOLVER = "full_solver"
TIER_CLIENT_AND_REPORTER = "client_and_reporter"
STATE_FOLDER_NAME = "BeamNG-Earth-Autodrive-Fixes"

MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"
COOKIE = struct.Struct("!8sIIii64s")
ENTRY = struct.Struct("!IIIIBc")
PYZ_MAGIC = b"PYZ\x00"

REQUIRED_UPSTREAM_MODULES = ("addons", "apppaths", "earthd", "roads", "rails", "routing")

MOD_GPS_LUA = "mod/beamng_earth/lua/ge/extensions/beamngEarth/gps.lua"
MOD_NAVGRAPH_LUA = "mod/beamng_earth/lua/ge/extensions/beamngEarth/navgraph.lua"
MOD_OVERTAKE_LUA = "mod/beamng_earth/lua/ge/extensions/beamngEarth/overtake.lua"
MOD_SETTINGS_LUA = "mod/beamng_earth/lua/ge/extensions/beamngEarth/settings.lua"
MOD_AUTODRIVE_LUA = "mod/beamng_earth/lua/vehicle/extensions/beamngEarth/autodrive.lua"
MOD_SPAWNMOTION_LUA = "mod/beamng_earth/lua/vehicle/extensions/beamngEarth/spawnMotion.lua"
MOD_EARTHMAP_VUE = "mod/beamng_earth/ui/ui-vue/mods/beamngEarth/EarthMap.vue"
MOD_EARTHMAP3D_VUE = "mod/beamng_earth/ui/ui-vue/mods/beamngEarth/EarthMap3d.vue"

MOD_FILES = (
    MOD_GPS_LUA,
    MOD_NAVGRAPH_LUA,
    MOD_OVERTAKE_LUA,
    MOD_SETTINGS_LUA,
    MOD_AUTODRIVE_LUA,
    MOD_SPAWNMOTION_LUA,
    MOD_EARTHMAP_VUE,
    MOD_EARTHMAP3D_VUE,
)
MOD_MARKER_FILE = MOD_GPS_LUA

ORIG_EARTHD_MODULE = "_taf_orig_earthd"
SERVER_REPORTER_MODULE = "traffic_autodrive_report"
MOD_TRAFFICREPORT_LUA = "mod/beamng_earth/lua/ge/extensions/beamngEarth/trafficreport.lua"
MOD_ACTIONS_JSON = "mod/beamng_earth/lua/ge/extensions/core/input/actions/beamngEarth.json"
MOD_KEYMAP_JSON = "mod/beamng_earth/settings/inputmaps/keyboard_beamngEarth.json"
MOD_SCRIPT_LUA = "mod/beamng_earth/scripts/beamng_earth/modScript.lua"
REPORTER_SHARED_MOD_FILES = (MOD_ACTIONS_JSON, MOD_KEYMAP_JSON, MOD_SCRIPT_LUA)

REPORT_ACTION_KEY = "beamngEarth_quickTrafficAutodriveReport"
REPORT_ACTION_DEF = {
    "cat": "gameplay",
    "order": 94,
    "ctx": "tlua",
    "onDown": "if beamngEarth_trafficreport then beamngEarth_trafficreport.captureSilent() end",
    "title": "BeamNG.Earth: quick Traffic and AutoDrive report",
    "desc": "Silently captures a screenshot and driving telemetry and sends a report to the Traffic and AutoDrive fixes queue (Ctrl+Shift+T).",
}
REPORT_BINDING = {"control": "ctrl shift t", "action": REPORT_ACTION_KEY}
REPORT_MODSCRIPT_LINE = "setExtensionUnloadMode('beamngEarth_trafficreport', 'manual')"


class PatchError(RuntimeError):
    pass


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {} if default is None else default


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _win_rel(rel_path):
    return rel_path.replace("/", "\\")


def _pack_entry_name(name):
    raw_name = name.encode("utf-8") + b"\x00"
    total = (ENTRY.size + len(raw_name) + 15) & ~15
    return total, raw_name.ljust(total - ENTRY.size, b"\x00")


def patch_actions_json_bytes(raw_bytes):
    doc = json.loads(raw_bytes.decode("utf-8-sig"))
    if not isinstance(doc, dict):
        raise PatchError("Unexpected input actions JSON format")
    doc[REPORT_ACTION_KEY] = dict(REPORT_ACTION_DEF)
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


def unpatch_actions_json_bytes(raw_bytes):
    doc = json.loads(raw_bytes.decode("utf-8-sig"))
    if not isinstance(doc, dict):
        return raw_bytes
    doc.pop(REPORT_ACTION_KEY, None)
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


def patch_keymap_json_bytes(raw_bytes):
    doc = json.loads(raw_bytes.decode("utf-8-sig"))
    if not isinstance(doc, dict) or not isinstance(doc.get("bindings"), list):
        raise PatchError("Unexpected keymap JSON format")
    bindings = [b for b in doc["bindings"] if not (isinstance(b, dict) and b.get("action") == REPORT_ACTION_KEY)]
    bindings.append(dict(REPORT_BINDING))
    doc["bindings"] = bindings
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


def unpatch_keymap_json_bytes(raw_bytes):
    doc = json.loads(raw_bytes.decode("utf-8-sig"))
    if not isinstance(doc, dict) or not isinstance(doc.get("bindings"), list):
        return raw_bytes
    doc["bindings"] = [b for b in doc["bindings"] if not (isinstance(b, dict) and b.get("action") == REPORT_ACTION_KEY)]
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


def patch_modscript_bytes(raw_bytes):
    text = raw_bytes.decode("utf-8-sig")
    if "beamngEarth_trafficreport" in text:
        return raw_bytes
    sep = "" if text.endswith(("\n", "\r\n")) else "\n"
    return (text + sep + REPORT_MODSCRIPT_LINE + "\n").encode("utf-8")


def unpatch_modscript_bytes(raw_bytes):
    text = raw_bytes.decode("utf-8-sig")
    crlf = "\r\n" in text
    lines = [ln for ln in text.replace("\r\n", "\n").splitlines() if "beamngEarth_trafficreport" not in ln]
    joined = "\n".join(lines) + ("\n" if lines else "")
    if crlf:
        joined = joined.replace("\n", "\r\n")
    return joined.encode("utf-8")


_PATH_PATTERNS = (
    re.compile(r"\bfile:///?[^\s\"'`<>()[\]{},;]+(?<![.!?:])", re.IGNORECASE),
    re.compile(r"(?:^|(?<=[\s(\"'`=,[\]{}]))\\\\[A-Za-z0-9_.$ -]+\\[A-Za-z0-9_.$ -]+(?:\\[^\s\\/\"'`<>|*?,;)\]}]+)*\\?(?<![.!?:])"),
    re.compile(r"%(?:USERPROFILE|APPDATA|LOCALAPPDATA|TEMP|TMP|HOMEDRIVE|HOMEPATH)%(?:\\+|/)[^\s\"'`<>|*?,;)\]}]+(?<![.!?:])", re.IGNORECASE),
    re.compile(r"(?:^|(?<=[\s(\"'`=,[\]{}]))[A-Za-z]:(?:\\+|/)(?:(?:[^\s\\/\"'`<>|*?,;)\]}]+(?: [^\s\\/\"'`<>|*?,;)\]}]+)*(?:\\+|/))*(?:[^\s\\/\"'`<>|*?,;)\]}]+(?:\\+|/)?(?<![.!?:]))?)?"),
    re.compile(r"(?:^|(?<=[\s(\"'`=,[\]{}]))/(?:Users|home|root|private/var|var/folders|mnt/[a-zA-Z]|tmp|etc|opt|proc)(?:/[^\s\\/\"'`<>|*?,;)\]}]+)+/?(?<![.!?:])"),
    re.compile(r"(?:^|(?<=[\s(\"'`=,[\]{}]))~(?:\\+|/)[^\s\"'`<>|*?,;)\]}]+(?<![.!?:])"),
)

_SETTINGS_AUTODRIVE_EXT_LUA = """
-- Traffic and AutoDrive fixes extension block (preserves upstream Experimental switches)
M.SCHEMA = M.SCHEMA or {}
M.SCHEMA.autodriveSpeedMph = M.SCHEMA.autodriveSpeedMph or { kind = 'number', default = 0, min = 0, max = 140, label = 'Autodrive speed (mph)' }
M.SCHEMA.autodriveAggression = M.SCHEMA.autodriveAggression or { kind = 'number', default = 0.42, min = 0.25, max = 0.85, label = 'Autodrive aggression' }
M.SCHEMA.autodrivePass = M.SCHEMA.autodrivePass or { kind = 'bool', default = true, label = 'Autodrive pass slow cars' }
M.SCHEMA.autodriveAwareness = M.SCHEMA.autodriveAwareness or { kind = 'number', default = 0.35, min = 0.20, max = 0.65, label = 'Autodrive avoidance margin' }
M.SCHEMA.routeAvoidHighways = M.SCHEMA.routeAvoidHighways or { kind = 'bool', default = false, label = 'Avoid highways' }
if type(M.HIDDEN) == 'table' then
  M.HIDDEN.autodriveSpeedMph = true
  M.HIDDEN.autodriveAggression = true
  M.HIDDEN.autodrivePass = true
  M.HIDDEN.autodriveAwareness = true
  M.HIDDEN.routeAvoidHighways = true
end
M.MPH_TO_MS = M.MPH_TO_MS or 0.44704
M.AUTODRIVE_PRESETS = M.AUTODRIVE_PRESETS or {
  calm = { speedMph = 0, aggression = 0.32, pass = true },
  cruiser = { speedMph = 80, aggression = 0.42, pass = true },
  sport = { speedMph = 95, aggression = 0.58, pass = true },
  race = { speedMph = 140, aggression = 0.72, pass = true, awareness = 0.55 },
}
function M.snapAutodriveSpeed(v)
  local n = tonumber(v)
  if not n or n ~= n or n == math.huge or n == -math.huge then return nil, 'autodriveSpeedMph: not a number' end
  n = math.floor(n + 0.5)
  if n >= 140 then return 140 end
  if n <= 0 then return 0 end
  if n < 45 then return 45 end
  return n
end
function M.snapAutodriveCoef(key, v, min, max)
  local n = tonumber(v)
  if not n or n ~= n or n == math.huge or n == -math.huge then return nil, key .. ': not a number' end
  if n < min then n = min elseif n > max then n = max end
  return math.floor(n * 100 + 0.5) / 100
end
function M.autodrivePlan(mph)
  local n = M.snapAutodriveSpeed(mph == nil and 0 or mph) or 0
  if n >= 140 then return 140, nil, 'off' end
  if n <= 0 then return 0, nil, 'legal' end
  return n, n * M.MPH_TO_MS, 'limit'
end
function M.autodriveTune()
  local getFn = M.get or function(k) return M.SCHEMA[k] and M.SCHEMA[k].default end
  local function valOrDef(k)
    local v = getFn(k)
    if v == nil and M.SCHEMA[k] then return M.SCHEMA[k].default end
    return v
  end
  local mph, ms, mode = M.autodrivePlan(valOrDef('autodriveSpeedMph'))
  local passVal = valOrDef('autodrivePass')
  if passVal == nil then passVal = true end
  return {
    speedMph = mph, speedMs = ms, speedMode = mode,
    aggression = valOrDef('autodriveAggression') or 0.42,
    pass = passVal and true or false,
    awareness = valOrDef('autodriveAwareness') or 0.35,
  }
end
function M.tunePublic(tune)
  tune = tune or M.autodriveTune()
  return { speedMph = tune.speedMph, aggression = tune.aggression, pass = tune.pass, awareness = tune.awareness }
end
function M.applyAutodriveTune()
  local ok, gps = pcall(require, 'beamngEarth/gps')
  if ok and gps and gps.applyTune then gps.applyTune() end
end
local _adf_orig_validate = M.validate
function M.validate(key, v)
  if key == 'autodriveSpeedMph' then return M.snapAutodriveSpeed(v) end
  if key == 'autodriveAggression' then return M.snapAutodriveCoef(key, v, 0.25, 0.85) end
  if key == 'autodriveAwareness' then return M.snapAutodriveCoef(key, v, 0.20, 0.65) end
  if _adf_orig_validate then return _adf_orig_validate(key, v) end
  if key == 'autodrivePass' or key == 'routeAvoidHighways' then return v and true or false end
  return v
end
function M.patchAutodrive(patch)
  if type(patch) ~= 'table' then return M.autodriveTune() end
  local setFn = M.set or M.uiSet
  local function take(key, raw)
    if raw == nil then return end
    local val = M.validate(key, raw)
    if val ~= nil and setFn then setFn(key, val) end
  end
  take('autodriveSpeedMph', patch.speedMph)
  take('autodriveAggression', patch.aggression)
  take('autodrivePass', patch.pass)
  take('autodriveAwareness', patch.awareness)
  if M.save then M.save() end
  return M.autodriveTune()
end
if type(M.APPLY) == 'table' then
  M.APPLY.autodriveSpeedMph = M.applyAutodriveTune
  M.APPLY.autodriveAggression = M.applyAutodriveTune
  M.APPLY.autodrivePass = M.applyAutodriveTune
  M.APPLY.autodriveAwareness = M.applyAutodriveTune
end
local _adf_orig_uiReset = M.uiReset
if _adf_orig_uiReset then
  function M.uiReset(...)
    local res = _adf_orig_uiReset(...)
    M.applyAutodriveTune()
    return res
  end
end
function M.uiGpsAutodrive(on, mode)
  local gps = require('beamngEarth/gps')
  if mode == 'race' or mode == 'normal' then gps.setAutodriveMode(mode) end
  local ad = on ~= nil and gps.setAutodrive(on) or gps.autodrive()
  if gps.applyTune then gps.applyTune() end
  return { ok = true, autodrive = ad, autodriveMode = gps.autodriveMode(), tune = M.tunePublic() }
end
function M.uiGpsAutodriveTune(patch)
  local gps = require('beamngEarth/gps')
  if type(patch) == 'table' then
    if type(patch.preset) == 'string' then
      local preset = M.AUTODRIVE_PRESETS[patch.preset]
      if preset then
        patch = { speedMph = preset.speedMph, aggression = preset.aggression, pass = preset.pass,
          awareness = preset.awareness }
      end
    end
    M.patchAutodrive(patch)
  end
  if gps.applyTune then gps.applyTune() end
  return { ok = true, autodrive = gps.autodrive(), autodriveMode = gps.autodriveMode(), tune = M.tunePublic() }
end
if M.load then pcall(M.load) end
"""


def redact_paths(text):
    if not isinstance(text, str):
        return text
    out = text
    for pat in _PATH_PATTERNS:
        out = pat.sub("[redacted-path]", out)
    return out


def read_experimental_flags(user_dir=None):
    """Read active BeamNG.Earth experimental toggles safely with path redaction."""
    if not user_dir:
        return []
    udir = Path(user_dir)
    out = []
    try:
        import experimental  # type: ignore

        settings = experimental.read_settings(str(udir))
        chosen = experimental.chosen(settings)
        if isinstance(chosen, (list, tuple)):
            for item in chosen[:24]:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    flag_str = f"{item[0]}={item[1]}"
                else:
                    flag_str = str(item)
                cleaned = redact_paths(flag_str.strip())[:64]
                if cleaned and cleaned not in out:
                    out.append(cleaned)
            if out:
                return out
    except Exception:
        pass

    settings_file = udir / "settings" / "beamngEarth.json"
    if settings_file.is_file():
        try:
            doc = json.loads(settings_file.read_text(encoding="utf-8-sig"))
            if isinstance(doc, dict):
                for key in sorted(doc):
                    if (key == "roadSigns" or key.startswith("exp")) and doc.get(key) is True:
                        cleaned = redact_paths(str(key).strip())[:64]
                        if cleaned and cleaned not in out:
                            out.append(cleaned)
                        if len(out) >= 24:
                            break
        except Exception:
            pass
    return out


def patch_settings_lua_bytes(upstream_bytes, payload_bytes):
    """Preserve upstream Experimental switches on dev.456+ while injecting Autodrive Tune."""
    if not upstream_bytes or b"M.SCHEMA" not in upstream_bytes:
        return payload_bytes
    if b"EXPERIMENTAL_ORDER" not in upstream_bytes and b"M.ORDER" not in upstream_bytes:
        return payload_bytes
    if b"function M.autodriveTune" in upstream_bytes:
        return upstream_bytes
    text = upstream_bytes.decode("utf-8-sig")
    crlf = "\r\n" in text
    norm = text.replace("\r\n", "\n")
    idx = norm.rfind("\nreturn M")
    if idx < 0:
        return payload_bytes
    merged = norm[:idx] + "\n" + _SETTINGS_AUTODRIVE_EXT_LUA.strip("\n") + norm[idx:]
    if crlf:
        merged = merged.replace("\n", "\r\n")
    return merged.encode("utf-8")


def _extract_fast_min_version_from_code(fast_co):
    found = []
    pending_ints = []
    for inst in dis.get_instructions(fast_co):
        if inst.opname == "LOAD_CONST" and isinstance(inst.argval, int) and not isinstance(inst.argval, bool):
            pending_ints.append(inst.argval)
        elif inst.opname in ("STORE_NAME", "STORE_GLOBAL"):
            if inst.argval == "MIN_VERSION" and pending_ints:
                found.append(max(pending_ints))
            pending_ints.clear()
        elif inst.opname in ("POP_TOP", "IMPORT_NAME"):
            pending_ints.clear()
    return max(found) if found else None


def _extract_fast_min_version_from_source(src_text):
    try:
        tree = ast.parse(src_text, filename="fast.py")
    except SyntaxError:
        return None
    found = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "MIN_VERSION":
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, int) and not isinstance(node.value.value, bool):
                        found.append(node.value.value)
    return max(found) if found else None


def _mod_files_drifted(gps_bytes, autodrive_bytes, navgraph_bytes=b""):
    if gps_bytes and b"function M." in gps_bytes and b"function M.setDestination" not in gps_bytes:
        return True
    if autodrive_bytes and b"function M." in autodrive_bytes and b"function M.drive" not in autodrive_bytes:
        return True
    if navgraph_bytes and b"function M." in navgraph_bytes and b"function M.toNodes" not in navgraph_bytes:
        return True
    return False


def _patched_mod_bytes(rel_path, raw_bytes, upstream_bytes=None, patch_tier=TIER_FULL_SOLVER):
    if patch_tier == TIER_CLIENT_AND_REPORTER:
        base = upstream_bytes if upstream_bytes is not None else raw_bytes
        if rel_path == MOD_MARKER_FILE and MARKER.encode("ascii") not in base:
            sep = b"" if base.endswith(b"\n") else b"\n"
            return base + sep + f"-- {MARKER} (tier={TIER_CLIENT_AND_REPORTER})\n".encode("ascii")
        return base
    if rel_path == MOD_SETTINGS_LUA and upstream_bytes is not None:
        raw_bytes = patch_settings_lua_bytes(upstream_bytes, raw_bytes)
    if rel_path == MOD_MARKER_FILE and MARKER.encode("ascii") not in raw_bytes:
        sep = b"" if raw_bytes.endswith(b"\n") else b"\n"
        return raw_bytes + sep + f"-- {MARKER}\n".encode("ascii")
    return raw_bytes


def _payload_digest(payload_dir):
    if payload_dir is None:
        return ""
    payload_root = Path(payload_dir)
    h = hashlib.sha256()
    for p in sorted(payload_root.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
            h.update(p.relative_to(payload_root).as_posix().encode("utf-8"))
            h.update(p.read_bytes())
    return h.hexdigest()


def _parse_version_tuple(version_str):
    if not version_str or not isinstance(version_str, str):
        return None
    s = version_str.strip().lstrip("vV")
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[-.]?(alpha|beta|rc)?(?:\.?dev)?\.?(\d+)?)?", s, re.IGNORECASE)
    if not m:
        return None
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    tag = (m.group(4) or "").lower()
    dev = int(m.group(5)) if m.group(5) is not None else None
    is_dev = "dev" in s.lower()
    stage = 3 if not tag else {"alpha": 0, "beta": 1, "rc": 2}.get(tag, 1)
    if is_dev:
        return (major, minor, patch, stage, 1, dev or 0)
    return (major, minor, patch, stage, 2, 0)


def _version_looks_older(version_str, minimum_str=MIN_SUPPORTED_EARTH_VERSION):
    vt = _parse_version_tuple(version_str)
    mt = _parse_version_tuple(minimum_str)
    if vt is None or mt is None:
        return False
    # Post-dev release (e.g. 1.9.1) supersedes 1.9.1-beta.dev.424; plain 1.9.1-beta (before dev.424) is older.
    if vt[:4] == mt[:4] and vt[4] == 2 and mt[4] == 1 and "beta" in (version_str or "").lower() and "dev" not in (version_str or "").lower():
        return True
    return vt < mt


def archive_entries(data):
    pos = data.rfind(MAGIC)
    if pos < 0:
        raise PatchError("BeamNG.Earth bundle marker not found")
    if pos + COOKIE.size > len(data):
        raise PatchError("Corrupt BeamNG.Earth bundle header")
    _, total, toc_offset, toc_len, py, lib = COOKIE.unpack_from(data, pos)
    start = pos + COOKIE.size - total
    toc_end = start + toc_offset + toc_len
    if start < 0 or toc_offset < 0 or toc_len < 0 or toc_end > len(data):
        raise PatchError("Invalid BeamNG.Earth bundle table of contents")
    entries = []
    at = start + toc_offset
    while at < toc_end:
        if at + ENTRY.size > toc_end:
            raise PatchError("Truncated BeamNG.Earth bundle entry")
        n, epos, elen, raw, comp, kind = ENTRY.unpack_from(data, at)
        if n < ENTRY.size or at + n > toc_end or epos < 0 or elen < 0 or start + epos + elen > start + toc_offset:
            raise PatchError("Corrupt BeamNG.Earth bundle entry")
        namebytes = data[at + ENTRY.size:at + n]
        entries.append((n, epos, elen, raw, comp, kind, namebytes, namebytes.rstrip(b"\x00").decode("utf-8", "replace")))
        at += n
    return start, toc_offset, pos, py, lib, entries


def _read_carchive_version(data, start, entry_map):
    for name in ("VERSION", "server\\VERSION", "mod\\beamng_earth\\VERSION"):
        e = entry_map.get(name)
        if e is None:
            continue
        raw = data[start + e[1]:start + e[1] + e[2]]
        try:
            raw = zlib.decompress(raw) if e[4] else raw
            text = raw.decode("utf-8", "replace").strip()
            if text:
                return text.splitlines()[0].strip()
        except Exception:
            pass
    return ""


def _read_pyz_toc(pz):
    if len(pz) < 12 or pz[:4] != PYZ_MAGIC:
        raise PatchError("Unsupported embedded Python archive format")
    magic_bytes = pz[4:8]
    offset = struct.unpack_from("!i", pz, 8)[0]
    if offset <= 12 or offset >= len(pz):
        raise PatchError("Corrupt embedded Python archive table of contents")
    try:
        toc = dict(marshal.loads(pz[offset:]))
    except Exception as exc:
        raise PatchError(f"Could not read embedded Python table of contents: {exc}") from exc
    return offset, magic_bytes, toc


def _read_source_version(root):
    vf = root / "VERSION"
    if vf.is_file():
        for line in vf.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                return line.strip()
    ed = root / "server" / "earthd.py"
    if ed.is_file():
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', ed.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(1)
    return ""


def strip_signature(data, end):
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise PatchError("Unsupported executable format (expected Windows PE)")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if pe + 24 > len(data) or data[pe:pe + 4] != b"PE\x00\x00":
        raise PatchError("Corrupt Windows PE header")
    opt = pe + 24
    if opt + 2 > len(data):
        raise PatchError("Truncated PE optional header")
    magic = struct.unpack_from("<H", data, opt)[0]
    if magic not in (0x10B, 0x20B):
        raise PatchError("Unsupported PE optional header format")
    rvas = struct.unpack_from("<I", data, opt + (92 if magic == 0x10B else 108))[0]
    sec = opt + (128 if magic == 0x10B else 144) if rvas > 4 else None
    checksum = opt + 64
    out = bytearray(data[:end])
    signed = bool(sec and sec + 8 <= len(out) and struct.unpack_from("<II", out, sec)[1])
    if sec and sec + 8 <= len(out):
        struct.pack_into("<II", out, sec, 0, 0)
    return out, signed, checksum


def pe_checksum(data, checksum_at):
    if len(data) % 2:
        data = data + b"\x00"
    value = 0
    for i in range(0, len(data), 1 << 20):
        words = struct.unpack_from("<" + "H" * (min(len(data) - i, 1 << 20) // 2), data, i)
        value += sum(words)
    value -= sum(struct.unpack_from("<HH", data, checksum_at))
    while value >> 16:
        value = (value & 0xFFFF) + (value >> 16)
    return value + len(data)


def _read_entry_bytes(data, start, entry):
    if entry is None:
        return b""
    raw = data[start + entry[1]:start + entry[1] + entry[2]]
    return zlib.decompress(raw) if entry[4] else raw


def _detect_exe_patch_tier(data, start, entry_map, pz, offset, toc):
    gps_body = _read_entry_bytes(data, start, entry_map.get(_win_rel(MOD_GPS_LUA)))
    if f"tier={TIER_CLIENT_AND_REPORTER}".encode("ascii") in gps_body:
        return TIER_CLIENT_AND_REPORTER
    if MARKER.encode("ascii") in gps_body:
        return TIER_FULL_SOLVER
    if len(pz) >= 8 and pz[4:8] == importlib.util.MAGIC_NUMBER and "fast" in toc:
        try:
            k, p_off, p_len = toc["fast"]
            if k == 0 and 12 <= p_off and p_off + p_len <= offset:
                fast_co = marshal.loads(zlib.decompress(pz[p_off:p_off + p_len]))
                fast_min = _extract_fast_min_version_from_code(fast_co)
                if fast_min is not None and fast_min > MAX_SOLVER_FAST_VERSION:
                    return TIER_CLIENT_AND_REPORTER
        except Exception:
            pass
    ad_body = _read_entry_bytes(data, start, entry_map.get(_win_rel(MOD_AUTODRIVE_LUA)))
    nav_body = _read_entry_bytes(data, start, entry_map.get(_win_rel(MOD_NAVGRAPH_LUA)))
    if _mod_files_drifted(gps_body, ad_body, nav_body):
        return TIER_CLIENT_AND_REPORTER
    return TIER_FULL_SOLVER


def _detect_source_patch_tier(root, sidecar=None):
    root = Path(root)
    if sidecar and sidecar.get("state") == "installed" and sidecar.get("patchTier") in (TIER_FULL_SOLVER, TIER_CLIENT_AND_REPORTER):
        return sidecar["patchTier"]
    gps_file = root / MOD_GPS_LUA
    gps_body = gps_file.read_bytes() if gps_file.is_file() else b""
    if f"tier={TIER_CLIENT_AND_REPORTER}".encode("ascii") in gps_body:
        return TIER_CLIENT_AND_REPORTER
    if MARKER.encode("ascii") in gps_body:
        return TIER_FULL_SOLVER
    fast_file = root / "server" / "fast.py"
    if fast_file.is_file():
        fast_min = _extract_fast_min_version_from_source(fast_file.read_text(encoding="utf-8", errors="replace"))
        if fast_min is not None and fast_min > MAX_SOLVER_FAST_VERSION:
            return TIER_CLIENT_AND_REPORTER
    ad_file = root / MOD_AUTODRIVE_LUA
    ad_body = ad_file.read_bytes() if ad_file.is_file() else b""
    nav_file = root / MOD_NAVGRAPH_LUA
    nav_body = nav_file.read_bytes() if nav_file.is_file() else b""
    if _mod_files_drifted(gps_body, ad_body, nav_body):
        return TIER_CLIENT_AND_REPORTER
    return TIER_FULL_SOLVER


def _has_earthd_dispatch_hook(earthd_co):
    for c in earthd_co.co_consts:
        if hasattr(c, "co_name") and c.co_name == "Connection":
            for m in c.co_consts:
                if (
                    hasattr(m, "co_name")
                    and m.co_name == "dispatch"
                    and getattr(m, "co_argcount", 0) == 2
                    and (getattr(m, "co_flags", 0) & 0x180) == 0
                ):
                    return True
    return False


def _verify_earthd_dispatch_hook(earthd_co, pz_data=None, offset=0, toc=None):
    if _has_earthd_dispatch_hook(earthd_co):
        return True
    if pz_data is not None and toc:
        for alt_mod in ("_brf_orig_earthd",):
            if alt_mod in toc:
                ak, ap, al = toc[alt_mod]
                if ak == 0 and 12 <= ap and ap + al <= offset:
                    alt_co = marshal.loads(zlib.decompress(pz_data[ap:ap + al]))
                    if _has_earthd_dispatch_hook(alt_co):
                        return True
    raise PatchError("Unsupported upstream earthd layout (missing Connection.dispatch hook point)")


def _build_taf_earthd_wrapper_src(patch_tier=TIER_FULL_SOLVER):
    lines = [
        "import sys as _taf_sys",
        f"import {ORIG_EARTHD_MODULE} as _taf_orig",
        f"_earth_traffic_autodrive_fixes_patch = {MARKER!r}",
        f"_TAF_PATCH_TIER = {patch_tier!r}",
        "_taf_conn = getattr(_taf_orig, 'Connection', None)",
        "_taf_orig_dispatch = getattr(_taf_conn, 'dispatch', None) if _taf_conn is not None else None",
        "if _taf_conn is None or not callable(_taf_orig_dispatch):",
        "    raise RuntimeError('earthd.Connection.dispatch hook point missing')",
        "if not getattr(_taf_orig_dispatch, '_taf_wrapped', False):",
        "    def _taf_dispatch(self, msg):",
        "        if getattr(self, 'closing', False):",
        "            return None",
        "        if isinstance(msg, dict) and msg.get('type') == 'traffic_autodrive_report':",
        f"            import {SERVER_REPORTER_MODULE}",
        "            _srv = getattr(self, 's', None) or getattr(self, 'server', None)",
        f"            {SERVER_REPORTER_MODULE}.submit(_srv, msg)",
        "            return None",
        "        res = _taf_orig_dispatch(self, msg)",
        "        if isinstance(msg, dict) and msg.get('type') == 'hello':",
        "            try:",
        f"                import {SERVER_REPORTER_MODULE}",
        "                _srv = getattr(self, 's', None) or getattr(self, 'server', None)",
        f"                {SERVER_REPORTER_MODULE}.check_fixed_reports_async(_srv, user_path=msg.get('userPath'))",
        "            except Exception:",
        "                pass",
        "        return res",
        "    _taf_dispatch._taf_wrapped = True",
        "    _taf_conn.dispatch = _taf_dispatch",
        "# Keep Connection.dispatch signature visible in co_consts so sibling add-on",
        "# dispatch-hook verifiers succeed if installed on top of this wrapper.",
        "class Connection:",
        "    def dispatch(self, msg):",
        "        return None",
        "del Connection",
        "_taf_mod_name = __name__",
        "_taf_orig._earth_traffic_autodrive_fixes_patch = _earth_traffic_autodrive_fixes_patch",
        "_taf_orig._TAF_PATCH_TIER = _TAF_PATCH_TIER",
        "globals().update(_taf_orig.__dict__)",
        "globals()['__name__'] = _taf_mod_name",
        "if _taf_mod_name == '__main__':",
        "    _taf_orig.main()",
        "else:",
        "    _taf_sys.modules[_taf_mod_name] = _taf_orig",
        "",
    ]
    return "\n".join(lines)


def patch_pyz_for_reporter(pz_data, payload_dir, patch_tier=TIER_FULL_SOLVER):
    if len(pz_data) < 12 or pz_data[:4] != PYZ_MAGIC or pz_data[4:8] != importlib.util.MAGIC_NUMBER:
        raise PatchError("Embedded Python version does not match; run setup through BeamNG.Earth's --run-addon")
    offset = struct.unpack_from("!i", pz_data, 8)[0]
    if offset <= 12 or offset >= len(pz_data):
        raise PatchError("Corrupt embedded Python archive table of contents")
    raw_toc = marshal.loads(pz_data[offset:])
    toc = dict(raw_toc)
    if "earthd" not in toc:
        raise PatchError("Executable does not contain expected server module 'earthd'")
    if ORIG_EARTHD_MODULE in toc or SERVER_REPORTER_MODULE in toc:
        raise PatchError("A Traffic and AutoDrive fixes PYZ patch is already present without a matching receipt")
    e_kind, e_pos, e_len = toc["earthd"]
    if e_kind != 0 or e_pos < 12 or e_pos + e_len > offset:
        raise PatchError("Unsupported PYZ entry layout for 'earthd'")
    earthd_code = marshal.loads(zlib.decompress(pz_data[e_pos:e_pos + e_len]))
    _verify_earthd_dispatch_hook(earthd_code, pz_data=pz_data, offset=offset, toc=toc)

    rep_path = Path(payload_dir) / "server" / f"{SERVER_REPORTER_MODULE}.py"
    if not rep_path.is_file():
        raise PatchError(f"Payload is missing server/{SERVER_REPORTER_MODULE}.py")

    toc[ORIG_EARTHD_MODULE] = toc["earthd"]
    wrapper_src = _build_taf_earthd_wrapper_src(patch_tier=patch_tier)
    wrapper_code = compile(wrapper_src, earthd_code.co_filename or "earthd.py", "exec")
    rep_code = compile(rep_path.read_text(encoding="utf-8"), f"{SERVER_REPORTER_MODULE}.py", "exec")

    compiled = {
        "earthd": zlib.compress(marshal.dumps(wrapper_code), 6),
        SERVER_REPORTER_MODULE: zlib.compress(marshal.dumps(rep_code), 6),
    }
    result = bytearray(pz_data[:offset])
    cursor = offset
    for mod, blob in compiled.items():
        toc[mod] = (0, cursor, len(blob))
        result.extend(blob)
        cursor += len(blob)
    struct.pack_into("!i", result, 8, cursor)
    result.extend(marshal.dumps(list(toc.items()) if isinstance(raw_toc, list) else toc))
    return bytes(result)


def unpatch_pyz_for_reporter(pz_data):
    """Unhook _taf_orig_earthd and traffic_autodrive_report cleanly even if another add-on wrapped on top."""
    if len(pz_data) < 12 or pz_data[:4] != PYZ_MAGIC:
        return pz_data
    offset = struct.unpack_from("!i", pz_data, 8)[0]
    if offset <= 12 or offset >= len(pz_data):
        return pz_data
    raw_toc = marshal.loads(pz_data[offset:])
    toc = dict(raw_toc)
    if ORIG_EARTHD_MODULE not in toc and SERVER_REPORTER_MODULE not in toc:
        return pz_data
    orig_entry = toc.get(ORIG_EARTHD_MODULE)
    if orig_entry is not None:
        earthd_is_taf = False
        if pz_data[4:8] == importlib.util.MAGIC_NUMBER and "earthd" in toc:
            try:
                ek, ep, el = toc["earthd"]
                if ek == 0 and 12 <= ep and ep + el <= offset:
                    ec = marshal.loads(zlib.decompress(pz_data[ep:ep + el]))
                    earthd_is_taf = MARKER in repr(ec.co_consts)
            except Exception:
                earthd_is_taf = False
        if earthd_is_taf or "_brf_orig_earthd" not in toc:
            toc["earthd"] = orig_entry
        else:
            toc["_brf_orig_earthd"] = orig_entry
    toc.pop(ORIG_EARTHD_MODULE, None)
    toc.pop(SERVER_REPORTER_MODULE, None)
    result = bytearray(pz_data[:offset])
    struct.pack_into("!i", result, 8, offset)
    result.extend(marshal.dumps(list(toc.items()) if isinstance(raw_toc, list) else toc))
    return bytes(result)


def patch_source_earthd_bytes(raw_bytes):
    text = raw_bytes.decode("utf-8-sig")
    if SERVER_REPORTER_MODULE in text:
        return raw_bytes
    crlf = "\r\n" in text
    norm = text.replace("\r\n", "\n")
    anchor = '        elif kind == "terrain_patch":'
    if anchor not in norm:
        raise PatchError("Could not locate earthd.dispatch anchor for Traffic and AutoDrive reporter")
    hook = (
        '        elif kind == "traffic_autodrive_report":\n'
        f"            import {SERVER_REPORTER_MODULE}\n"
        f"            {SERVER_REPORTER_MODULE}.submit(self.s, msg)\n"
    )
    patched = norm.replace(anchor, hook + anchor, 1)
    hello_anchor = '            self.s.check_user_dir(self, msg.get("userPath"))\n'
    if hello_anchor in patched:
        hello_hook = (
            "            try:\n"
            f"                import {SERVER_REPORTER_MODULE}\n"
            f'                {SERVER_REPORTER_MODULE}.check_fixed_reports_async(self.s, user_path=msg.get("userPath"))\n'
            "            except Exception:\n"
            "                pass\n"
        )
        patched = patched.replace(hello_anchor, hello_anchor + hello_hook, 1)
    if crlf:
        patched = patched.replace("\n", "\r\n")
    return patched.encode("utf-8")


def unpatch_source_earthd_bytes(raw_bytes):
    text = raw_bytes.decode("utf-8-sig")
    if SERVER_REPORTER_MODULE not in text:
        return raw_bytes
    crlf = "\r\n" in text
    norm = text.replace("\r\n", "\n")
    hook = (
        '        elif kind == "traffic_autodrive_report":\n'
        f"            import {SERVER_REPORTER_MODULE}\n"
        f"            {SERVER_REPORTER_MODULE}.submit(self.s, msg)\n"
    )
    hello_hook = (
        "            try:\n"
        f"                import {SERVER_REPORTER_MODULE}\n"
        f'                {SERVER_REPORTER_MODULE}.check_fixed_reports_async(self.s, user_path=msg.get("userPath"))\n'
        "            except Exception:\n"
        "                pass\n"
    )
    norm = norm.replace(hook, "").replace(hello_hook, "")
    if crlf:
        norm = norm.replace("\n", "\r\n")
    return norm.encode("utf-8")


def patch_executable_with_tier(data, payload_dir, apply_fixes=True, enable_reporter=False):
    start, toff, cookie_pos, py, lib, entries = archive_entries(data)
    entry_map = {e[7]: e for e in entries}
    for rel in MOD_FILES:
        win_key = _win_rel(rel)
        if win_key not in entry_map:
            raise PatchError(f"Expected bundled mod file missing in archive: {win_key}")
    if enable_reporter:
        for rel in REPORTER_SHARED_MOD_FILES:
            win_key = _win_rel(rel)
            if win_key not in entry_map:
                raise PatchError(f"Expected bundled mod file missing in archive: {win_key}")

    pyz_candidates = [e for e in entries if e[5] == b"z"]
    patch_tier = TIER_FULL_SOLVER
    pyz_e = None
    pz = b""
    if len(pyz_candidates) == 1:
        pyz_e = pyz_candidates[0]
        raw_pyz = data[start + pyz_e[1]:start + pyz_e[1] + pyz_e[2]]
        pz = zlib.decompress(raw_pyz) if pyz_e[4] else raw_pyz
        off, _magic, toc = _read_pyz_toc(pz)
        patch_tier = _detect_exe_patch_tier(data, start, entry_map, pz, off, toc)
    elif enable_reporter:
        raise PatchError("Expected one embedded Python archive in executable")
    if not apply_fixes:
        patch_tier = TIER_CLIENT_AND_REPORTER

    payload_root = Path(payload_dir)
    carchive_replacements = {}
    if enable_reporter and pyz_e is not None:
        cur_py = sys.version_info.major * 100 + sys.version_info.minor
        if py != cur_py:
            raise PatchError(
                f"Python {py // 100}.{py % 100} is required to patch the reporter into this executable "
                f"(running Python {cur_py // 100}.{cur_py % 100}). "
                "Run setup via BeamNG.Earth.exe --run-addon or 1_Click_Install.bat."
            )
        new_pz = patch_pyz_for_reporter(pz, payload_root, patch_tier=patch_tier)
        if pyz_e[4]:
            carchive_replacements[pyz_e[7]] = (zlib.compress(new_pz, 6), len(new_pz), 1, b"z")
        else:
            carchive_replacements[pyz_e[7]] = (new_pz, len(new_pz), 0, b"z")

    if apply_fixes or enable_reporter:
        target_files = (
            MOD_FILES
            if (apply_fixes and patch_tier == TIER_FULL_SOLVER)
            else (MOD_MARKER_FILE,)
        )
        for rel in target_files:
            src = payload_root / rel
            if not src.is_file():
                raise PatchError(f"Payload is missing mod file: {rel}")
            upstream_bytes = _read_entry_bytes(data, start, entry_map.get(_win_rel(rel)))
            raw_bytes = _patched_mod_bytes(
                rel, src.read_bytes(), upstream_bytes=upstream_bytes, patch_tier=patch_tier
            )
            carchive_replacements[_win_rel(rel)] = (zlib.compress(raw_bytes, 6), len(raw_bytes), 1, b"b")

    if enable_reporter:
        tr_src = payload_root / MOD_TRAFFICREPORT_LUA
        if not tr_src.is_file():
            raise PatchError(f"Payload is missing mod file: {MOD_TRAFFICREPORT_LUA}")
        tr_bytes = tr_src.read_bytes()
        act_bytes = patch_actions_json_bytes(_read_entry_bytes(data, start, entry_map[_win_rel(MOD_ACTIONS_JSON)]))
        key_bytes = patch_keymap_json_bytes(_read_entry_bytes(data, start, entry_map[_win_rel(MOD_KEYMAP_JSON)]))
        ms_bytes = patch_modscript_bytes(_read_entry_bytes(data, start, entry_map[_win_rel(MOD_SCRIPT_LUA)]))
        carchive_replacements[_win_rel(MOD_TRAFFICREPORT_LUA)] = (zlib.compress(tr_bytes, 6), len(tr_bytes), 1, b"b")
        carchive_replacements[_win_rel(MOD_ACTIONS_JSON)] = (zlib.compress(act_bytes, 6), len(act_bytes), 1, b"b")
        carchive_replacements[_win_rel(MOD_KEYMAP_JSON)] = (zlib.compress(key_bytes, 6), len(key_bytes), 1, b"b")
        carchive_replacements[_win_rel(MOD_SCRIPT_LUA)] = (zlib.compress(ms_bytes, 6), len(ms_bytes), 1, b"b")

    header, signed, checksum_at = strip_signature(data, cookie_pos + COOKIE.size)

    new_body = bytearray()
    new_toc = bytearray()
    handled = set()
    for n, pos, length, raw, comp, kind, namebytes, name in entries:
        out_pos = len(new_body)
        if name in carchive_replacements:
            handled.add(name)
            chunk, raw_len, out_comp, out_kind = carchive_replacements[name]
            new_body.extend(chunk)
            new_toc.extend(ENTRY.pack(n, out_pos, len(chunk), raw_len, out_comp, out_kind) + namebytes)
        else:
            chunk = bytes(header[start + pos:start + pos + length])
            new_body.extend(chunk)
            new_toc.extend(ENTRY.pack(n, out_pos, length, raw, comp, kind) + namebytes)

    for name, (chunk, raw_len, out_comp, out_kind) in carchive_replacements.items():
        if name in handled:
            continue
        out_pos = len(new_body)
        total_n, namebytes = _pack_entry_name(name)
        new_body.extend(chunk)
        new_toc.extend(ENTRY.pack(total_n, out_pos, len(chunk), raw_len, out_comp, out_kind) + namebytes)

    cookie = COOKIE.pack(MAGIC, len(new_body) + len(new_toc) + COOKIE.size, len(new_body), len(new_toc), py, lib)
    result = bytearray(header[:start]) + new_body + new_toc + cookie
    struct.pack_into("<I", result, checksum_at, pe_checksum(result, checksum_at))
    archive_entries(result)
    return bytes(result), signed, patch_tier


def patch_executable(data, payload_dir, apply_fixes=True, enable_reporter=False):
    rebuilt, signed, _tier = patch_executable_with_tier(
        data, payload_dir, apply_fixes=apply_fixes, enable_reporter=enable_reporter
    )
    return rebuilt, signed


def _cleanup_stale_pending(target, previous):
    old_pending = previous.get("pending")
    if not old_pending:
        return
    p = Path(old_pending)
    prefix = f"{target.name}.{TAG}."
    if p.parent == target.parent and p.name.startswith(prefix) and p.name.endswith(".pending") and p.is_file():
        try:
            p.unlink()
        except OSError:
            pass


def _cleanup_stale_stage(state_dir, previous):
    old_stage = previous.get("stageDir")
    if not old_stage:
        return
    p = Path(old_stage)
    if p.parent == state_dir and p.name.startswith("stage-") and p.is_dir():
        shutil.rmtree(p, ignore_errors=True)


def _splice_original_mod_files(current_exe_bytes, backup_exe_bytes):
    """Restore Traffic and AutoDrive fixes entries into current_exe_bytes while preserving stacked sibling add-ons."""
    c_start, _c_toff, c_cookie_pos, c_py, c_lib, c_entries = archive_entries(current_exe_bytes)
    b_start, _b_toff, _b_cookie_pos, _b_py, _b_lib, b_entries = archive_entries(backup_exe_bytes)
    c_map = {e[7]: e for e in c_entries}
    b_map = {e[7]: e for e in b_entries}
    marker_entry = c_map.get(_win_rel(MOD_MARKER_FILE))
    if marker_entry is None or MARKER.encode("ascii") not in _read_entry_bytes(current_exe_bytes, c_start, marker_entry):
        return None
    has_brf = b"EARTH_BRIDGE_RAIL_FIXES_" in current_exe_bytes or _win_rel("mod/beamng_earth/lua/ge/extensions/beamngEarth/bugreport.lua") in c_map
    if not has_brf:
        c_pyz = [e for e in c_entries if e[5] == b"z"]
        if len(c_pyz) == 1:
            try:
                pz_chk = _read_entry_bytes(current_exe_bytes, c_start, c_pyz[0])
                _, _, toc_chk = _read_pyz_toc(pz_chk)
                has_brf = "bridge_rail_report" in toc_chk or "_brf_orig_earthd" in toc_chk
            except Exception:
                has_brf = False
    if not has_brf:
        return None
    mod_win_keys = {_win_rel(rel) for rel in MOD_FILES}
    if not mod_win_keys.issubset(b_map):
        return None
    tr_win_key = _win_rel(MOD_TRAFFICREPORT_LUA)
    act_win_key = _win_rel(MOD_ACTIONS_JSON)
    key_win_key = _win_rel(MOD_KEYMAP_JSON)
    ms_win_key = _win_rel(MOD_SCRIPT_LUA)

    header, _signed, checksum_at = strip_signature(current_exe_bytes, c_cookie_pos + COOKIE.size)
    new_body = bytearray()
    new_toc = bytearray()
    for n, pos, length, raw, comp, kind, namebytes, name in c_entries:
        if name == tr_win_key and name not in b_map:
            continue
        out_pos = len(new_body)
        if name in mod_win_keys:
            be = b_map[name]
            chunk = backup_exe_bytes[b_start + be[1]:b_start + be[1] + be[2]]
            new_body.extend(chunk)
            new_toc.extend(ENTRY.pack(n, out_pos, be[2], be[3], be[4], be[5]) + namebytes)
        elif kind == b"z":
            pz_cur = _read_entry_bytes(current_exe_bytes, c_start, (n, pos, length, raw, comp, kind, namebytes, name))
            pz_un = unpatch_pyz_for_reporter(pz_cur)
            if pz_un != pz_cur:
                chunk = zlib.compress(pz_un, 6) if comp else pz_un
                new_body.extend(chunk)
                new_toc.extend(ENTRY.pack(n, out_pos, len(chunk), len(pz_un), comp, kind) + namebytes)
            else:
                chunk = bytes(header[c_start + pos:c_start + pos + length])
                new_body.extend(chunk)
                new_toc.extend(ENTRY.pack(n, out_pos, length, raw, comp, kind) + namebytes)
        elif name in (act_win_key, key_win_key, ms_win_key):
            cur_raw = _read_entry_bytes(current_exe_bytes, c_start, (n, pos, length, raw, comp, kind, namebytes, name))
            if name == act_win_key:
                un_raw = unpatch_actions_json_bytes(cur_raw)
            elif name == key_win_key:
                un_raw = unpatch_keymap_json_bytes(cur_raw)
            else:
                un_raw = unpatch_modscript_bytes(cur_raw)
            chunk = zlib.compress(un_raw, 6) if comp else un_raw
            new_body.extend(chunk)
            new_toc.extend(ENTRY.pack(n, out_pos, len(chunk), len(un_raw), comp, kind) + namebytes)
        else:
            chunk = bytes(header[c_start + pos:c_start + pos + length])
            new_body.extend(chunk)
            new_toc.extend(ENTRY.pack(n, out_pos, length, raw, comp, kind) + namebytes)
    cookie = COOKIE.pack(MAGIC, len(new_body) + len(new_toc) + COOKIE.size, len(new_body), len(new_toc), c_py, c_lib)
    result = bytearray(header[:c_start]) + new_body + new_toc + cookie
    struct.pack_into("<I", result, checksum_at, pe_checksum(result, checksum_at))
    archive_entries(result)
    return bytes(result)


def _prepare_exe(target, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter=False):
    sidecar = target.with_name(target.name + f".{TAG}.json")
    backup = target.with_name(target.name + f".{TAG}.original")
    before = target.read_bytes()
    previous = read_json(sidecar, {})
    digest = sha256_bytes(before)
    original_sha = digest
    payload_sha = _payload_digest(payload_dir) if payload_dir is not None else previous.get("payloadSha", "")

    if operation == "restore":
        if not previous:
            raise PatchError("No recorded Traffic and AutoDrive fixes patch for this executable")
        if digest == previous.get("originalSha"):
            _cleanup_stale_pending(target, previous)
            record = dict(previous, state="restored", message="Original executable is already active.", token=token)
            atomic_json(sidecar, record)
            atomic_json(status, record)
            return record
        if not backup.is_file() or sha256_bytes(backup.read_bytes()) != previous.get("originalSha"):
            raise PatchError("Original executable backup is missing or checksum-mismatched")
        backup_bytes = backup.read_bytes()
        if digest != previous.get("patchedSha"):
            spliced = _splice_original_mod_files(before, backup_bytes)
            if spliced is None:
                raise PatchError("Executable changed after patching; restoration aborted to protect updates")
            after = spliced
            original_sha = previous["originalSha"]
            signed = False
        else:
            original_sha = previous["originalSha"]
            after = backup_bytes
            signed = previous.get("originalSigned", False)
    else:
        pristine = before
        if previous and digest == previous.get("patchedSha"):
            if (
                str(config) == previous.get("config")
                and previous.get("payloadSha") == payload_sha
                and previous.get("applyFixes") == bool(apply_fixes)
                and previous.get("enableReporter", False) == bool(enable_reporter)
            ):
                record = dict(
                    previous,
                    state="installed",
                    message="Traffic and AutoDrive fixes are installed. Restart BeamNG.Earth if you changed options.",
                    token=token,
                    serverPid=server_pid,
                )
                atomic_json(sidecar, record)
                atomic_json(status, record)
                return record
            if not backup.is_file() or sha256_bytes(backup.read_bytes()) != previous.get("originalSha"):
                raise PatchError("Original executable backup could not be verified; existing patch preserved")
            pristine = backup.read_bytes()
            original_sha = previous["originalSha"]
        elif backup.exists() and (
            not previous
            or sha256_bytes(backup.read_bytes()) != previous.get("originalSha")
            or digest != previous.get("originalSha")
        ):
            raise PatchError("Existing backup belongs to a different Earth build; it was preserved")

        after, signed, patch_tier = patch_executable_with_tier(
            pristine, payload_dir, apply_fixes=apply_fixes, enable_reporter=enable_reporter
        )
        if not backup.exists():
            with backup.open("xb") as stream:
                stream.write(pristine)
        if sha256_bytes(backup.read_bytes()) != original_sha:
            raise PatchError("Backup verification failed; original executable untouched")

    _cleanup_stale_pending(target, previous)
    pending = target.with_name(f"{target.name}.{TAG}.{uuid.uuid4().hex}.pending")
    with pending.open("xb") as stream:
        stream.write(after)
    if sha256_bytes(pending.read_bytes()) != sha256_bytes(after):
        raise PatchError("Staged patch checksum verification failed")

    patched_sha = sha256_bytes(after) if operation == "install" else previous["patchedSha"]
    eff_tier = patch_tier if operation == "install" else previous.get("patchTier", TIER_FULL_SOLVER)
    record = {
        "version": VERSION,
        "mode": "exe",
        "state": "pending",
        "operation": operation,
        "patchTier": eff_tier,
        "target": str(target),
        "backup": str(backup),
        "pending": str(pending),
        "expectedSha": digest,
        "replacementSha": sha256_bytes(after),
        "originalSha": original_sha,
        "patchedSha": patched_sha,
        "payloadSha": payload_sha,
        "originalSigned": signed,
        "applyFixes": bool(apply_fixes),
        "enableReporter": bool(enable_reporter) if operation == "install" else previous.get("enableReporter", False),
        "sidecar": str(sidecar),
        "status": str(status),
        "config": str(config),
        "token": token,
        "serverPid": server_pid,
        "message": (
            "Original executable restore staged."
            if operation == "restore"
            else "Traffic and AutoDrive fixes staged."
        ),
    }
    atomic_json(sidecar, record)
    atomic_json(status, record)
    return record


def _unpatch_shared_source_file(rel, cur_bytes):
    if rel == MOD_ACTIONS_JSON and b"beamngEarth_quickBridgeRailReport" in cur_bytes:
        return unpatch_actions_json_bytes(cur_bytes)
    if rel == MOD_KEYMAP_JSON and b"beamngEarth_quickBridgeRailReport" in cur_bytes:
        return unpatch_keymap_json_bytes(cur_bytes)
    if rel == MOD_SCRIPT_LUA and b"beamngEarth_bugreport" in cur_bytes:
        return unpatch_modscript_bytes(cur_bytes)
    if rel == "server/earthd.py" and b"bridge_rail_report" in cur_bytes:
        return unpatch_source_earthd_bytes(cur_bytes)
    return None


def _prepare_source(root, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter=False):
    state_dir = root / f".{TAG}"
    sidecar = state_dir / "receipt.json"
    backup_dir = state_dir / "backups"
    previous = read_json(sidecar, {})
    payload_sha = _payload_digest(payload_dir) if payload_dir is not None else previous.get("payloadSha", "")

    if operation == "restore":
        if not previous or not previous.get("files"):
            raise PatchError("No recorded Traffic and AutoDrive fixes source patch for this folder")
        items = []
        up_to_date = True
        stage_dir = None
        for entry in previous["files"]:
            rel = entry["rel"]
            dest = root / rel
            if entry["added"]:
                if dest.exists():
                    up_to_date = False
                    if sha256_bytes(dest.read_bytes()) != entry["patchedSha"]:
                        raise PatchError(f"{rel} changed after patching; restoration aborted")
                    items.append({
                        "rel": rel,
                        "action": "delete",
                        "expectedSha": entry["patchedSha"],
                        "replacementSha": "",
                        "staged": "",
                    })
            else:
                bfile = backup_dir / rel
                orig_sha = entry["originalSha"]
                if not bfile.is_file() or sha256_bytes(bfile.read_bytes()) != orig_sha:
                    raise PatchError(f"Backup missing or mismatched for {rel}")
                cur_bytes = dest.read_bytes() if dest.is_file() else b""
                cur_sha = sha256_bytes(cur_bytes) if dest.is_file() else ""
                if cur_sha != orig_sha:
                    up_to_date = False
                    if cur_sha != entry["patchedSha"]:
                        spliced = _unpatch_shared_source_file(rel, cur_bytes) if dest.is_file() else None
                        if spliced is None:
                            raise PatchError(f"{rel} changed after patching; restoration aborted")
                        if stage_dir is None:
                            _cleanup_stale_stage(state_dir, previous)
                            stage_dir = state_dir / f"stage-{uuid.uuid4().hex}"
                            stage_dir.mkdir(parents=True, exist_ok=True)
                        sfile = stage_dir / rel
                        sfile.parent.mkdir(parents=True, exist_ok=True)
                        sfile.write_bytes(spliced)
                        items.append({
                            "rel": rel,
                            "action": "write",
                            "expectedSha": cur_sha,
                            "replacementSha": sha256_bytes(spliced),
                            "staged": str(sfile),
                        })
                    else:
                        items.append({
                            "rel": rel,
                            "action": "write",
                            "expectedSha": cur_sha,
                            "replacementSha": orig_sha,
                            "staged": str(bfile),
                        })
        if up_to_date:
            record = dict(previous, state="restored", message="Original source files are already active.", token=token)
            atomic_json(sidecar, record)
            atomic_json(status, record)
            return record
        record = {
            "version": VERSION,
            "mode": "source",
            "state": "pending",
            "operation": "restore",
            "target": str(root),
            "stageDir": str(stage_dir) if stage_dir is not None else "",
            "files": previous["files"],
            "items": items,
            "sidecar": str(sidecar),
            "status": str(status),
            "config": str(config),
            "token": token,
            "serverPid": server_pid,
            "payloadSha": payload_sha,
            "applyFixes": False,
            "enableReporter": False,
            "message": "Original source restore staged.",
        }
        atomic_json(sidecar, record)
        atomic_json(status, record)
        return record

    payload_root = Path(payload_dir)
    patch_tier = _detect_source_patch_tier(root, sidecar=previous)
    if not apply_fixes:
        patch_tier = TIER_CLIENT_AND_REPORTER
    planned = []
    if apply_fixes or enable_reporter:
        target_files = (
            MOD_FILES
            if (apply_fixes and patch_tier == TIER_FULL_SOLVER)
            else (MOD_MARKER_FILE,)
        )
        for rel in target_files:
            src = payload_root / rel
            if not src.is_file():
                raise PatchError(f"Payload is missing mod file: {rel}")
            dest_cur = root / rel
            bfile_cur = backup_dir / rel
            upstream_bytes = (
                bfile_cur.read_bytes()
                if bfile_cur.is_file()
                else (dest_cur.read_bytes() if dest_cur.is_file() else None)
            )
            planned.append(
                (rel, _patched_mod_bytes(rel, src.read_bytes(), upstream_bytes=upstream_bytes, patch_tier=patch_tier))
            )

    if enable_reporter:
        for req_rel in REPORTER_SHARED_MOD_FILES:
            if not (root / req_rel).is_file():
                raise PatchError(f"Expected mod file missing in source tree: {req_rel}")
        rep_py = payload_root / "server" / f"{SERVER_REPORTER_MODULE}.py"
        tr_lua = payload_root / MOD_TRAFFICREPORT_LUA
        if not rep_py.is_file():
            raise PatchError(f"Payload is missing server/{SERVER_REPORTER_MODULE}.py")
        if not tr_lua.is_file():
            raise PatchError(f"Payload is missing {MOD_TRAFFICREPORT_LUA}")

        def _base_shared(rel):
            bfile = backup_dir / rel
            cur = root / rel
            if bfile.is_file():
                cur_b = cur.read_bytes() if cur.is_file() else b""
                prev_map = {e["rel"]: e for e in previous.get("files", [])}
                pe = prev_map.get(rel)
                if pe and cur_b and sha256_bytes(cur_b) == pe.get("patchedSha"):
                    return bfile.read_bytes()
            return cur.read_bytes()

        planned.append((f"server/{SERVER_REPORTER_MODULE}.py", rep_py.read_bytes()))
        planned.append((MOD_TRAFFICREPORT_LUA, tr_lua.read_bytes()))
        planned.append((MOD_ACTIONS_JSON, patch_actions_json_bytes(_base_shared(MOD_ACTIONS_JSON))))
        planned.append((MOD_KEYMAP_JSON, patch_keymap_json_bytes(_base_shared(MOD_KEYMAP_JSON))))
        planned.append((MOD_SCRIPT_LUA, patch_modscript_bytes(_base_shared(MOD_SCRIPT_LUA))))
        earthd_base = _base_shared("server/earthd.py")
        if b'elif kind == "terrain_patch":' in earthd_base:
            planned.append(("server/earthd.py", patch_source_earthd_bytes(earthd_base)))
        else:
            planned.append((f"server/{ORIG_EARTHD_MODULE}.py", earthd_base))
            planned.append(("server/earthd.py", _build_taf_earthd_wrapper_src(patch_tier=patch_tier).encode("utf-8")))

    if (
        previous
        and previous.get("state") == "installed"
        and previous.get("payloadSha") == payload_sha
        and previous.get("applyFixes") == bool(apply_fixes)
        and previous.get("enableReporter", False) == bool(enable_reporter)
    ):
        matches = True
        for entry in previous.get("files", []):
            dest = root / entry["rel"]
            if not dest.is_file() or sha256_bytes(dest.read_bytes()) != entry["patchedSha"]:
                matches = False
                break
        if matches:
            record = dict(previous, token=token, serverPid=server_pid, message="Traffic and AutoDrive fixes are already active in this source checkout.")
            atomic_json(sidecar, record)
            atomic_json(status, record)
            return record

    _cleanup_stale_stage(state_dir, previous)
    stage_dir = state_dir / f"stage-{uuid.uuid4().hex}"
    stage_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)

    prev_entries = {e["rel"]: e for e in previous.get("files", [])}
    file_records = []
    items = []
    for rel, new_bytes in planned:
        dest = root / rel
        prev_entry = prev_entries.get(rel)
        if prev_entry and not prev_entry["added"]:
            bfile = backup_dir / rel
            if not bfile.is_file() or sha256_bytes(bfile.read_bytes()) != prev_entry["originalSha"]:
                raise PatchError(f"Backup could not be verified for {rel}")
            if not dest.is_file() or sha256_bytes(dest.read_bytes()) != prev_entry["patchedSha"]:
                raise PatchError(f"{rel} changed since previous patch; restore first before re-patching")
            orig_sha = prev_entry["originalSha"]
            added = False
            expected_sha = prev_entry["patchedSha"]
        elif prev_entry and prev_entry["added"]:
            orig_sha = ""
            added = True
            expected_sha = sha256_bytes(dest.read_bytes()) if dest.is_file() else ""
        elif dest.is_file():
            orig_bytes = dest.read_bytes()
            orig_sha = sha256_bytes(orig_bytes)
            bfile = backup_dir / rel
            bfile.parent.mkdir(parents=True, exist_ok=True)
            bfile.write_bytes(orig_bytes)
            added = False
            expected_sha = orig_sha
        else:
            orig_sha = ""
            added = True
            expected_sha = ""

        sfile = stage_dir / rel
        sfile.parent.mkdir(parents=True, exist_ok=True)
        sfile.write_bytes(new_bytes)
        new_sha = sha256_bytes(new_bytes)
        file_records.append({
            "rel": rel,
            "added": added,
            "originalSha": orig_sha,
            "patchedSha": new_sha,
        })
        items.append({
            "rel": rel,
            "action": "write",
            "expectedSha": expected_sha,
            "replacementSha": new_sha,
            "staged": str(sfile),
        })

    for prev_rel, prev_entry in prev_entries.items():
        if prev_rel in {r for r, _ in planned}:
            continue
        dest = root / prev_rel
        if prev_entry["added"]:
            if dest.exists():
                items.append({
                    "rel": prev_rel,
                    "action": "delete",
                    "expectedSha": sha256_bytes(dest.read_bytes()),
                    "replacementSha": "",
                    "staged": "",
                })
        else:
            bfile = backup_dir / prev_rel
            if bfile.is_file() and sha256_bytes(bfile.read_bytes()) == prev_entry["originalSha"]:
                items.append({
                    "rel": prev_rel,
                    "action": "write",
                    "expectedSha": sha256_bytes(dest.read_bytes()) if dest.is_file() else "",
                    "replacementSha": prev_entry["originalSha"],
                    "staged": str(bfile),
                })

    record = {
        "version": VERSION,
        "mode": "source",
        "state": "pending",
        "operation": "install",
        "patchTier": patch_tier,
        "target": str(root),
        "stageDir": str(stage_dir),
        "files": file_records,
        "items": items,
        "sidecar": str(sidecar),
        "status": str(status),
        "config": str(config),
        "token": token,
        "serverPid": server_pid,
        "payloadSha": payload_sha,
        "applyFixes": bool(apply_fixes),
        "enableReporter": bool(enable_reporter),
        "message": "Traffic and AutoDrive fixes staged for source checkout.",
    }
    atomic_json(sidecar, record)
    atomic_json(status, record)
    return record


def _resolve_target_path(target):
    target_path = Path(target).resolve()
    if target_path.is_dir():
        return target_path, "source"
    if target_path.is_file():
        return target_path, "exe"
    raise PatchError(f"Target not found: {target_path}")


def prepare(target, config, status, payload_dir=None, operation="install", token="", server_pid=0,
            apply_fixes=True, enable_reporter=False):
    resolved_target, mode = _resolve_target_path(target)
    if not apply_fixes and not enable_reporter and operation == "install":
        operation = "restore"
    if operation != "restore":
        verify_compatibility(resolved_target)
        if payload_dir is None:
            raise PatchError("Payload directory is required for install")
        payload_dir = Path(payload_dir).resolve(strict=True)
    elif payload_dir is not None:
        p = Path(payload_dir)
        payload_dir = p.resolve() if p.exists() else None
    if mode == "exe":
        return _prepare_exe(
            resolved_target, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter
        )
    return _prepare_source(
        resolved_target, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter
    )


def apply_now(record):
    """Apply staged exe or source changes immediately (offline CLI / unit tests)."""
    state = read_json(record["config"], {})
    if record.get("token") and state.get("requestToken") != record["token"]:
        raise PatchError("A newer request superseded this patch")

    if record.get("mode") == "source":
        root = Path(record["target"])
        for item in record["items"]:
            dest = root / item["rel"]
            if item["expectedSha"]:
                if not dest.is_file() or sha256_bytes(dest.read_bytes()) != item["expectedSha"]:
                    raise PatchError(f"Target changed before swap ({item['rel']})")
            elif dest.exists():
                raise PatchError(f"File unexpectedly appeared before swap ({item['rel']})")
        for item in record["items"]:
            dest = root / item["rel"]
            try:
                if item["action"] == "delete":
                    if dest.exists():
                        dest.unlink()
                else:
                    staged = Path(item["staged"])
                    if sha256_bytes(staged.read_bytes()) != item["replacementSha"]:
                        raise PatchError(f"Staged file changed ({item['rel']})")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(staged, dest)
                    if sha256_bytes(dest.read_bytes()) != item["replacementSha"]:
                        raise PatchError(f"Verification failed after write ({item['rel']})")
            except PermissionError as exc:
                raise PatchError("BeamNG.Earth is currently locking files. Close BeamNG.Earth completely and try again.") from exc
        stage_dir = record.get("stageDir")
        if stage_dir and Path(stage_dir).is_dir():
            shutil.rmtree(stage_dir, ignore_errors=True)
    else:
        target, pending = Path(record["target"]), Path(record["pending"])
        if sha256_bytes(target.read_bytes()) != record["expectedSha"] or sha256_bytes(pending.read_bytes()) != record["replacementSha"]:
            raise PatchError("Files changed after preparation; refusing to overwrite")
        try:
            os.replace(pending, target)
        except PermissionError as exc:
            raise PatchError("BeamNG.Earth is currently running. Close BeamNG.Earth completely and try again.") from exc
        except OSError as exc:
            if getattr(exc, "winerror", None) in (5, 32):
                raise PatchError("BeamNG.Earth is currently running. Close BeamNG.Earth completely and try again.") from exc
            raise

    finished = dict(
        record,
        state="installed" if record["operation"] == "install" else "restored",
        message=(
            "Traffic and AutoDrive fixes installed."
            if record["operation"] == "install"
            else "Original BeamNG.Earth restored."
        ),
    )
    atomic_json(record["sidecar"], finished)
    atomic_json(record["status"], finished)
    return finished


def inspect_target(target):
    """Read-only inspection of a BeamNG.Earth executable or source directory.

    Safe to call from any Python 3.10+ interpreter before modifying anything.
    """
    info = {
        "target": str(target),
        "mode": "unknown",
        "compatible": False,
        "reason": "",
        "earthVersion": "",
        "pythonVersion": "",
        "pythonMatches": False,
        "signed": False,
        "patched": False,
        "patchTier": "",
        "hasBackup": False,
        "state": "invalid",
        "missingModules": [],
        "missingModFiles": [],
    }
    try:
        resolved, mode = _resolve_target_path(target)
    except PatchError as exc:
        info["reason"] = str(exc)
        return info

    info["target"] = str(resolved)
    info["mode"] = mode

    if mode == "source":
        if not (resolved / "server" / "earthd.py").is_file():
            info["reason"] = "Not a BeamNG.Earth source folder (server/earthd.py missing)."
            return info
        info["earthVersion"] = _read_source_version(resolved)
        info["pythonVersion"] = f"{sys.version_info.major}.{sys.version_info.minor}"
        info["pythonMatches"] = True
        missing_mods = [m for m in REQUIRED_UPSTREAM_MODULES if not (resolved / "server" / f"{m}.py").is_file()]
        missing_mod_files = [rel for rel in MOD_FILES if not (resolved / rel).is_file()]
        info["missingModules"] = missing_mods
        info["missingModFiles"] = missing_mod_files

        marker_file = resolved / MOD_MARKER_FILE
        patched = False
        if marker_file.is_file():
            try:
                patched = MARKER.encode("ascii") in marker_file.read_bytes()
            except OSError:
                patched = False
        info["patched"] = patched
        receipt = read_json(resolved / f".{TAG}" / "receipt.json", {})
        info["hasBackup"] = bool(receipt and (resolved / f".{TAG}" / "backups").is_dir())
        if missing_mods or missing_mod_files:
            parts = []
            if missing_mods:
                parts.append("missing server modules: " + ", ".join(missing_mods))
            if missing_mod_files:
                parts.append("missing mod files: " + ", ".join(missing_mod_files))
            ver = info["earthVersion"] or "unknown"
            info["state"] = "incompatible"
            info["reason"] = (
                f"Incompatible BeamNG.Earth source ({ver}: {'; '.join(parts)}). "
                f"Requires BeamNG.Earth {MIN_SUPPORTED_EARTH_VERSION} or newer."
            )
            return info
        if _version_looks_older(info["earthVersion"]):
            info["state"] = "incompatible"
            info["reason"] = (
                f"BeamNG.Earth {info['earthVersion']} is too old. "
                f"Requires {MIN_SUPPORTED_EARTH_VERSION} or newer."
            )
            return info
        info["compatible"] = True
        info["patchTier"] = _detect_source_patch_tier(resolved, sidecar=receipt)
        info["state"] = "installed" if patched else "unpatched"
        return info

    try:
        data = resolved.read_bytes()
        start, _toff, cookie_pos, py, _lib, entries = archive_entries(data)
    except (OSError, PatchError) as exc:
        info["reason"] = str(exc)
        return info

    info["pythonVersion"] = f"{py // 100}.{py % 100}"
    cur_py = sys.version_info.major * 100 + sys.version_info.minor
    info["pythonMatches"] = (py == cur_py)

    entry_map = {e[7]: e for e in entries}
    info["earthVersion"] = _read_carchive_version(data, start, entry_map)

    try:
        _hdr, signed, _cs = strip_signature(data, cookie_pos + COOKIE.size)
        info["signed"] = bool(signed)
    except PatchError:
        info["signed"] = False

    missing_mod_files = [rel for rel in MOD_FILES if _win_rel(rel) not in entry_map]
    info["missingModFiles"] = missing_mod_files

    pyz_candidates = [e for e in entries if e[5] == b"z"]
    if len(pyz_candidates) != 1:
        info["reason"] = "Executable does not contain a valid embedded Python archive."
        info["state"] = "incompatible"
        return info

    pyz_entry = pyz_candidates[0]
    raw_pyz = data[start + pyz_entry[1]:start + pyz_entry[1] + pyz_entry[2]]
    try:
        pz = zlib.decompress(raw_pyz) if pyz_entry[4] else raw_pyz
        _off, _magic, toc = _read_pyz_toc(pz)
    except (zlib.error, PatchError) as exc:
        info["reason"] = str(exc)
        info["state"] = "incompatible"
        return info

    missing_mods = [m for m in REQUIRED_UPSTREAM_MODULES if m not in toc]
    info["missingModules"] = missing_mods

    marker_entry = entry_map.get(_win_rel(MOD_MARKER_FILE))
    patched = False
    if marker_entry is not None:
        try:
            raw_m = data[start + marker_entry[1]:start + marker_entry[1] + marker_entry[2]]
            body_m = zlib.decompress(raw_m) if marker_entry[4] else raw_m
            patched = MARKER.encode("ascii") in body_m
        except Exception:
            patched = False
    info["patched"] = patched

    backup = resolved.with_name(f"{resolved.name}.{TAG}.original")
    sidecar = read_json(resolved.with_name(f"{resolved.name}.{TAG}.json"), {})
    info["hasBackup"] = bool(backup.is_file() and sidecar)

    if missing_mods or missing_mod_files:
        parts = []
        if missing_mods:
            parts.append("missing server modules: " + ", ".join(missing_mods))
        if missing_mod_files:
            parts.append("missing mod files: " + ", ".join(missing_mod_files))
        ver = info["earthVersion"] or "unknown"
        info["state"] = "incompatible"
        info["reason"] = (
            f"Incompatible BeamNG.Earth build ({ver}: {'; '.join(parts)}). "
            f"Requires BeamNG.Earth {MIN_SUPPORTED_EARTH_VERSION} or newer."
        )
        return info

    if _version_looks_older(info["earthVersion"]):
        info["state"] = "incompatible"
        info["reason"] = (
            f"BeamNG.Earth {info['earthVersion']} is too old. "
            f"Requires {MIN_SUPPORTED_EARTH_VERSION} or newer."
        )
        return info

    info["compatible"] = True
    info["patchTier"] = (
        sidecar.get("patchTier")
        if (patched and sidecar.get("patchTier") in (TIER_FULL_SOLVER, TIER_CLIENT_AND_REPORTER))
        else _detect_exe_patch_tier(data, start, entry_map, pz, _off, toc)
    )
    if patched and info["hasBackup"]:
        info["state"] = "installed"
    elif patched:
        info["state"] = "patched_no_backup"
    else:
        info["state"] = "unpatched"
    return info


def verify_compatibility(target):
    info = inspect_target(target)
    if not info["compatible"]:
        raise PatchError(info["reason"] or "Incompatible BeamNG.Earth build.")
    return info


def _running_earth_exes():
    if os.name != "nt":
        return []
    found = []
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        TH32CS_SNAPPROCESS = 0x00000002
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        MAX_PATH = 260

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.c_uint32),
                ("cntUsage", ctypes.c_uint32),
                ("th32ProcessID", ctypes.c_uint32),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", ctypes.c_uint32),
                ("cntThreads", ctypes.c_uint32),
                ("th32ParentProcessID", ctypes.c_uint32),
                ("pcPriClassBase", ctypes.c_int32),
                ("dwFlags", ctypes.c_uint32),
                ("szExeFile", ctypes.c_wchar * MAX_PATH),
            ]

        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == ctypes.c_void_p(-1).value:
            return []
        try:
            pe = PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = k32.Process32FirstW(snap, ctypes.byref(pe))
            while ok:
                exe_name = pe.szExeFile.lower()
                if exe_name.startswith("beamng.earth") and exe_name.endswith(".exe"):
                    proc = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pe.th32ProcessID)
                    if proc:
                        try:
                            buf = ctypes.create_unicode_buffer(1024)
                            size = ctypes.c_uint32(len(buf))
                            if k32.QueryFullProcessImageNameW(proc, 0, buf, ctypes.byref(size)):
                                found.append(Path(buf.value))
                        finally:
                            k32.CloseHandle(proc)
                ok = k32.Process32NextW(snap, ctypes.byref(pe))
        finally:
            k32.CloseHandle(snap)
    except Exception:
        return []
    return found


def _is_candidate_exe(path):
    name = path.name.lower()
    if not (name.startswith("beamng.earth") and name.endswith(".exe")):
        return False
    if (
        f".{TAG.lower()}." in name
        or ".earthbridgerailfixes." in name
        or ".original" in name
        or ".pending" in name
        or ".swap." in name
    ):
        return False
    return path.is_file()


def discover_targets(extra_paths=None, include_system=True):
    """Discover BeamNG.Earth executables and source checkouts, returning inspect_target dicts."""
    candidates = []
    seen = set()

    def _add(p):
        try:
            rp = Path(p).resolve()
        except OSError:
            return
        key = str(rp).lower() if os.name == "nt" else str(rp)
        if key not in seen:
            seen.add(key)
            candidates.append(rp)

    def _scan_dir(d, recurse_earth_dirs=False):
        try:
            d = Path(d)
            if not d.is_dir():
                return
            if (d / "server" / "earthd.py").is_file() and (d / "mod" / "beamng_earth").is_dir():
                _add(d)
            for child in sorted(d.iterdir()):
                if child.is_file() and _is_candidate_exe(child):
                    _add(child)
                elif recurse_earth_dirs and child.is_dir() and "beamng" in child.name.lower():
                    _scan_dir(child, recurse_earth_dirs=False)
        except OSError:
            pass

    for raw in (extra_paths or ()):
        p = Path(raw)
        if p.is_file():
            _add(p)
        elif p.is_dir():
            _scan_dir(p, recurse_earth_dirs=True)

    if include_system:
        for proc_exe in _running_earth_exes():
            _add(proc_exe)
        here = Path(__file__).resolve()
        for base in (Path.cwd(), Path.cwd().parent, here.parent.parent, here.parent.parent.parent):
            _scan_dir(base, recurse_earth_dirs=False)
        home = Path.home()
        for folder in ("Desktop", "Downloads", "Documents"):
            _scan_dir(home / folder, recurse_earth_dirs=True)
        local_app = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
        _scan_dir(local_app / "beamng-earth", recurse_earth_dirs=True)
        _scan_dir(local_app / "Programs", recurse_earth_dirs=True)
        for env_key in ("ProgramFiles", "ProgramFiles(x86)"):
            pf = os.environ.get(env_key)
            if pf:
                _scan_dir(Path(pf), recurse_earth_dirs=True)

    results = []
    for cand in candidates:
        info = inspect_target(cand)
        if info["mode"] in ("exe", "source") and info["state"] != "invalid":
            results.append(info)
    results.sort(key=lambda x: (not x["compatible"], x["mode"] != "exe", x["target"].lower()))
    return results


def _read_ini_kv(path):
    out = {}
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return out
    for enc in ("utf-8-sig", "mbcs", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith((";", "#", "[")):
            k, v = line.split("=", 1)
            out[k.strip()] = os.path.expandvars(v.strip())
    return out


def discover_user_dirs(include_system=True, extra_paths=None):
    """Discover BeamNG.drive user folders on Windows in upstream priority order."""
    candidates = []
    for p in (extra_paths or ()):
        candidates.append(Path(p))
    if include_system and os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        app_json = read_json(local / "beamng-earth" / "app.json", {})
        told = app_json.get("gameUserDir")
        if isinstance(told, str) and told:
            tp = Path(told)
            if (tp / "mods").is_dir():
                candidates.append(tp)
        ini_uf = _read_ini_kv(local / "BeamNG" / "BeamNG.drive.ini").get("userFolder")
        if ini_uf:
            ufp = Path(ini_uf)
            if (ufp / "current").is_dir():
                candidates.append(ufp / "current")
            elif ufp.is_dir():
                candidates.append(ufp)
        candidates.append(local / "BeamNG" / "BeamNG.drive" / "current")
        for legacy in sorted(glob.glob(str(local / "BeamNG.drive" / "[0-9]*")), reverse=True):
            candidates.append(Path(legacy))

    out, seen = [], set()
    for c in candidates:
        try:
            rc = c.resolve()
        except OSError:
            continue
        if not rc.is_dir():
            continue
        key = str(rc).lower() if os.name == "nt" else str(rc)
        if key not in seen:
            seen.add(key)
            out.append(rc)
    return out


def _default_state_paths(target):
    base = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / STATE_FOLDER_NAME
    digest = hashlib.sha256(str(Path(target).resolve()).encode("utf-8")).hexdigest()[:12]
    return base / f"control-{digest}.json", base / f"status-{digest}.json"


def install_target(target, payload_dir, config_path=None, status_path=None,
                   apply_fixes=True, enable_reporter=False, apply_immediately=True):
    """Verify compatibility, stage, and optionally apply patches to an exe or source dir."""
    verify_compatibility(target)
    if config_path is None or status_path is None:
        def_cfg, def_status = _default_state_paths(target)
        config_path = config_path or def_cfg
        status_path = status_path or def_status
    token = uuid.uuid4().hex
    atomic_json(config_path, {
        "enabled": bool(apply_fixes or enable_reporter),
        "modPath": str(Path(__file__).resolve()),
        "requestToken": token,
    })
    record = prepare(
        target,
        config_path,
        status_path,
        payload_dir,
        operation="install",
        token=token,
        apply_fixes=apply_fixes,
        enable_reporter=enable_reporter,
    )
    if apply_immediately and record.get("state") == "pending":
        record = apply_now(record)
    return record


def cleanup_backup(target):
    """Remove Traffic and AutoDrive fixes backup/receipt files once target is verified restored/unpatched."""
    resolved, mode = _resolve_target_path(target)
    removed = False
    if mode == "source":
        state_dir = resolved / f".{TAG}"
        sidecar = read_json(state_dir / "receipt.json", {})
        if sidecar and sidecar.get("state") not in ("restored", "cancelled"):
            for entry in sidecar.get("files", []):
                dest = resolved / entry["rel"]
                if entry.get("added") and dest.exists():
                    raise PatchError("Cannot delete backup while patched files are still present")
                if not entry.get("added") and entry.get("originalSha"):
                    if not dest.is_file() or sha256_bytes(dest.read_bytes()) != entry["originalSha"]:
                        raise PatchError("Cannot delete backup until original source files are restored")
        if state_dir.is_dir():
            shutil.rmtree(state_dir)
            removed = True
        return removed

    sidecar_path = resolved.with_name(resolved.name + f".{TAG}.json")
    backup_path = resolved.with_name(resolved.name + f".{TAG}.original")
    sidecar = read_json(sidecar_path, {})
    digest = sha256_bytes(resolved.read_bytes())
    stacked_restored = (
        sidecar.get("state") == "restored"
        and digest == sidecar.get("replacementSha")
        and not inspect_target(resolved).get("patched")
    )
    if sidecar.get("originalSha") and digest != sidecar.get("originalSha") and not stacked_restored:
        raise PatchError("Cannot delete backup while patched executable is still active")
    if not sidecar and inspect_target(resolved).get("patched"):
        raise PatchError("Cannot delete backup while executable is patched")
    _cleanup_stale_pending(resolved, sidecar)
    for p in (backup_path, sidecar_path):
        if p.is_file():
            p.unlink()
            removed = True
    return removed


def restore_target(target, payload_dir=None, config_path=None, status_path=None,
                   apply_immediately=True, cleanup=False):
    """Stage and optionally apply restoration of the original exe or source dir."""
    if config_path is None or status_path is None:
        def_cfg, def_status = _default_state_paths(target)
        config_path = config_path or def_cfg
        status_path = status_path or def_status
    token = uuid.uuid4().hex
    atomic_json(config_path, {
        "enabled": False,
        "modPath": str(Path(__file__).resolve()),
        "requestToken": token,
    })
    record = prepare(
        target,
        config_path,
        status_path,
        payload_dir,
        operation="restore",
        token=token,
    )
    if apply_immediately and record.get("state") == "pending":
        record = apply_now(record)
    if cleanup and record.get("state") == "restored":
        cleanup_backup(target)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--status", type=Path)
    parser.add_argument("--payload", type=Path, default=Path(__file__).resolve().parent.parent / "payload")
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--apply-now", action="store_true")
    parser.add_argument("--enable-reporter", action="store_true")
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args(argv)
    if args.inspect:
        info = inspect_target(args.target)
        print(json.dumps(info, indent=2))
        return 0 if info["compatible"] else 2
    if not args.config or not args.status:
        def_cfg, def_status = _default_state_paths(args.target)
        args.config = args.config or def_cfg
        args.status = args.status or def_status
    if args.restore:
        record = restore_target(
            args.target,
            payload_dir=args.payload,
            config_path=args.config,
            status_path=args.status,
            apply_immediately=args.apply_now,
            cleanup=args.cleanup and args.apply_now,
        )
    else:
        record = prepare(
            args.target,
            args.config,
            args.status,
            args.payload,
            operation="install",
            enable_reporter=args.enable_reporter,
        )
        if args.apply_now and record["state"] == "pending":
            record = apply_now(record)
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
