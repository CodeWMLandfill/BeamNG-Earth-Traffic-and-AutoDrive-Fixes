"""Self-contained patcher for BeamNG.Earth - Traffic and AutoDrive fixes.

Works against Windows PE executables, Linux Type 2 AppImages, PyInstaller ELF64
installations, BeamNG.Earth-Host server archives, and source checkouts,
delivering the 8 unified Traffic + AutoDrive mod files plus the optional
Ctrl+Shift+T 1-hotkey driving telemetry reporter without overwriting sibling
add-ons (such as Bridge, Rail & Tunnel Fixes on Ctrl+Shift+B).
"""
import argparse
import ast
import base64
import contextlib
import ctypes
import dis
import glob
import hashlib
import importlib.util
import io
import json
import marshal
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
import zlib

TAG = "earthAutodriveFixes"
MARKER = "EARTH_AUTODRIVE_FIXES_0_1_4"
MARKER_PREFIX = b"EARTH_AUTODRIVE_FIXES_"
VERSION = "0.1.4"
MIN_SUPPORTED_EARTH_VERSION = "v1.9.1-beta.dev.424"
TIER_FULL_SOLVER = "full_solver"
TIER_CLIENT_AND_REPORTER = "client_and_reporter"
STATE_FOLDER_NAME = "BeamNG-Earth-Autodrive-Fixes"
CARCHIVE_CACHE_ID_KEYS = ("CODE_ID", "REVISIT_ID")
TAF_CARCHIVE_BACKUP_KEY = "_taf_orig_mods.zlib"
APPIMAGE_WRAPPER_MAGIC = b"# EARTH_AUTODRIVE_FIXES_APPIMAGE_WRAPPER\n"

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


def _carchive_sep(entry_map):
    for k in entry_map:
        if "\\" in k:
            return "\\"
        if "/" in k:
            return "/"
    return "\\" if os.name == "nt" else "/"


def _carchive_key(entry_map, rel_path):
    posix_rel = rel_path.replace("\\", "/")
    win_rel = posix_rel.replace("/", "\\")
    if win_rel in entry_map:
        return win_rel
    if posix_rel in entry_map:
        return posix_rel
    return win_rel if _carchive_sep(entry_map) == "\\" else posix_rel


def _has_taf_marker(raw_bytes):
    return bool(raw_bytes and (MARKER.encode("ascii") in raw_bytes or MARKER_PREFIX in raw_bytes))


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

    for s_rel in ("settings/beamngearth.json", "settings/beamngEarth.json"):
        settings_file = udir / s_rel
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
                if out:
                    break
            except Exception:
                pass
    return out


def unpatch_settings_lua_bytes(raw_bytes):
    if not raw_bytes or b"function M.autodriveTune" not in raw_bytes:
        return raw_bytes
    text = raw_bytes.decode("utf-8-sig")
    crlf = "\r\n" in text
    norm = text.replace("\r\n", "\n")
    ext_block = "\n" + _SETTINGS_AUTODRIVE_EXT_LUA.strip("\n")
    if ext_block in norm:
        cleaned = norm.replace(ext_block, "", 1)
        if crlf:
            cleaned = cleaned.replace("\n", "\r\n")
        return cleaned.encode("utf-8")
    return raw_bytes


def patch_settings_lua_bytes(upstream_bytes, payload_bytes):
    """Preserve upstream Experimental switches on dev.456+ while injecting Autodrive Tune."""
    if not upstream_bytes or b"M.SCHEMA" not in upstream_bytes:
        return payload_bytes
    if b"EXPERIMENTAL_ORDER" not in upstream_bytes and b"M.ORDER" not in upstream_bytes:
        return payload_bytes
    base_bytes = unpatch_settings_lua_bytes(upstream_bytes)
    if b"function M.autodriveTune" in base_bytes:
        return base_bytes
    text = base_bytes.decode("utf-8-sig")
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


def _strip_existing_taf_marker_lines(raw_bytes):
    if not raw_bytes or MARKER_PREFIX not in raw_bytes:
        return raw_bytes
    crlf = b"\r\n" in raw_bytes
    lines = [ln for ln in raw_bytes.replace(b"\r\n", b"\n").split(b"\n") if MARKER_PREFIX not in ln]
    sep = b"\r\n" if crlf else b"\n"
    return sep.join(lines)


def _patched_mod_bytes(rel_path, raw_bytes, upstream_bytes=None, patch_tier=TIER_FULL_SOLVER):
    if patch_tier == TIER_CLIENT_AND_REPORTER:
        base = _strip_existing_taf_marker_lines(upstream_bytes if upstream_bytes is not None else raw_bytes)
        if rel_path == MOD_MARKER_FILE and MARKER.encode("ascii") not in base:
            sep = b"" if base.endswith(b"\n") else b"\n"
            return base + sep + f"-- {MARKER} (tier={TIER_CLIENT_AND_REPORTER})\n".encode("ascii")
        return base
    if rel_path == MOD_SETTINGS_LUA and upstream_bytes is not None:
        raw_bytes = patch_settings_lua_bytes(upstream_bytes, raw_bytes)
    raw_bytes = _strip_existing_taf_marker_lines(raw_bytes)
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
    for name in ("VERSION", "server/VERSION", "mod/beamng_earth/VERSION", "server\\VERSION", "mod\\beamng_earth\\VERSION"):
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
    if len(data) >= 4 and data[:4] == b"\x7fELF":
        return bytearray(data[:end]), False, None
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise PatchError("Unsupported executable format (expected Windows PE or Linux ELF)")
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


def _rebuild_elf64_with_carchive(orig_data, start, cookie_end, new_carchive):
    """Rebuild an ELF64 PyInstaller binary preserving trailing sections (.shstrtab) and Elf64_Shdr offsets."""
    if len(orig_data) >= 64 and orig_data[:4] == b"\x7fELF" and orig_data[4] == 2 and orig_data[5] == 1:
        e_shoff = struct.unpack_from("<Q", orig_data, 40)[0]
        e_shentsize, e_shnum = struct.unpack_from("<HH", orig_data, 58)
        sh_table_end = e_shoff + e_shnum * e_shentsize
        if e_shoff >= cookie_end and e_shentsize == 64 and e_shnum > 0 and sh_table_end <= len(orig_data):
            delta = len(new_carchive) - (cookie_end - start)
            between = orig_data[cookie_end:e_shoff]
            pre_sh_len = start + len(new_carchive) + len(between)
            align_pad = b"\x00" * ((-pre_sh_len) % 8)
            new_e_shoff = pre_sh_len + len(align_pad)
            sh_table = bytearray(orig_data[e_shoff:sh_table_end])
            for idx in range(e_shnum):
                off = idx * 64
                fields = list(struct.unpack_from("<IIQQQQIIQQ", sh_table, off))
                sh_offset, sh_size = fields[4], fields[5]
                if sh_offset == start and sh_offset + sh_size == cookie_end:
                    fields[5] = len(new_carchive)
                elif sh_offset >= cookie_end:
                    fields[4] = sh_offset + delta
                struct.pack_into("<IIQQQQIIQQ", sh_table, off, *fields)
            trailing = orig_data[sh_table_end:]
            out = bytearray(orig_data[:start]) + bytearray(new_carchive) + bytearray(between) + bytearray(align_pad) + sh_table + bytearray(trailing)
            struct.pack_into("<Q", out, 40, new_e_shoff)
            return bytes(out)
    return bytes(orig_data[:start]) + bytes(new_carchive)


def _salt_carchive_cache_id(orig_raw, entry_name, payload_sha, marker=MARKER, apply_fixes=True):
    """Deterministically derive an exact-length salted lowercase ASCII hex digest for CODE_ID / REVISIT_ID."""
    trimmed = orig_raw.rstrip(b"\r\n")
    trailing = orig_raw[len(trimmed):]
    target_len = len(trimmed)
    if target_len <= 0:
        return orig_raw
    mode_suffix = b"" if apply_fixes else b":nofixes"
    seed = (
        orig_raw
        + b":"
        + entry_name.encode("ascii")
        + b":"
        + marker.encode("ascii")
        + b":"
        + str(payload_sha or "").encode("ascii")
        + mode_suffix
    )
    hex_buf = hashlib.sha256(seed).hexdigest()
    round_idx = 1
    while len(hex_buf) < target_len:
        hex_buf += hashlib.sha256(seed + f":{round_idx}".encode("ascii")).hexdigest()
        round_idx += 1
    cand = hex_buf[:target_len].encode("ascii")
    if cand == trimmed:
        alt = hashlib.sha256(seed + b":alt").hexdigest()
        while len(alt) < target_len:
            alt += hashlib.sha256(seed + b":alt2").hexdigest()
        cand = alt[:target_len].encode("ascii")
    return cand + trailing


def _read_carchive_cache_ids(data):
    """Extract {CODE_ID, REVISIT_ID} ASCII strings from a PyInstaller CArchive executable."""
    start, _toff, _cookie_pos, _py, _lib, entries = archive_entries(data)
    out = {}
    for entry in entries:
        name = entry[7]
        if name in CARCHIVE_CACHE_ID_KEYS:
            blob = data[start + entry[1]:start + entry[1] + entry[2]]
            raw = zlib.decompress(blob) if entry[4] else blob
            out[name] = raw.decode("ascii", "replace")
    return out


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
    del pz, offset, toc
    gps_body = _read_entry_bytes(data, start, entry_map.get(_carchive_key(entry_map, MOD_GPS_LUA)))
    if f"tier={TIER_CLIENT_AND_REPORTER}".encode("ascii") in gps_body:
        return TIER_CLIENT_AND_REPORTER
    if _has_taf_marker(gps_body):
        return TIER_FULL_SOLVER
    ad_body = _read_entry_bytes(data, start, entry_map.get(_carchive_key(entry_map, MOD_AUTODRIVE_LUA)))
    nav_body = _read_entry_bytes(data, start, entry_map.get(_carchive_key(entry_map, MOD_NAVGRAPH_LUA)))
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
    if _has_taf_marker(gps_body):
        return TIER_FULL_SOLVER
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
        "            _up = getattr(self, '_taf_user_path', None)",
        "            if _up is not None:",
        "                try:",
        f"                    {SERVER_REPORTER_MODULE}.submit(_srv, msg, user_path=_up)",
        "                except TypeError:",
        f"                    {SERVER_REPORTER_MODULE}.submit(_srv, msg)",
        "            else:",
        f"                {SERVER_REPORTER_MODULE}.submit(_srv, msg)",
        "            return None",
        "        res = _taf_orig_dispatch(self, msg)",
        "        if isinstance(msg, dict) and msg.get('type') == 'hello':",
        "            try:",
        "                if isinstance(msg.get('userPath'), str) and msg.get('userPath').strip():",
        "                    self._taf_user_path = msg.get('userPath')",
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
    if ORIG_EARTHD_MODULE in toc or SERVER_REPORTER_MODULE in toc:
        pz_data = unpatch_pyz_for_reporter(pz_data)
        offset = struct.unpack_from("!i", pz_data, 8)[0]
        raw_toc = marshal.loads(pz_data[offset:])
        toc = dict(raw_toc)
    if "earthd" not in toc:
        raise PatchError("Executable does not contain expected server module 'earthd'")
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
                    earthd_is_taf = "EARTH_AUTODRIVE_FIXES_" in repr(ec.co_consts)
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
        f"            {SERVER_REPORTER_MODULE}.submit(self.s, msg, user_path=getattr(self, '_taf_user_path', None))\n"
    )
    patched = norm.replace(anchor, hook + anchor, 1)
    hello_anchor = '            self.s.check_user_dir(self, msg.get("userPath"))\n'
    if hello_anchor in patched:
        hello_hook = (
            "            try:\n"
            "                if isinstance(msg.get(\"userPath\"), str) and msg.get(\"userPath\").strip():\n"
            "                    self._taf_user_path = msg.get(\"userPath\")\n"
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
    hooks = (
        (
            '        elif kind == "traffic_autodrive_report":\n'
            f"            import {SERVER_REPORTER_MODULE}\n"
            f"            {SERVER_REPORTER_MODULE}.submit(self.s, msg, user_path=getattr(self, '_taf_user_path', None))\n"
        ),
        (
            '        elif kind == "traffic_autodrive_report":\n'
            f"            import {SERVER_REPORTER_MODULE}\n"
            f"            {SERVER_REPORTER_MODULE}.submit(self.s, msg)\n"
        ),
        (
            "            try:\n"
            "                if isinstance(msg.get(\"userPath\"), str) and msg.get(\"userPath\").strip():\n"
            "                    self._taf_user_path = msg.get(\"userPath\")\n"
            f"                import {SERVER_REPORTER_MODULE}\n"
            f'                {SERVER_REPORTER_MODULE}.check_fixed_reports_async(self.s, user_path=msg.get("userPath"))\n'
            "            except Exception:\n"
            "                pass\n"
        ),
        (
            "            try:\n"
            f"                import {SERVER_REPORTER_MODULE}\n"
            f'                {SERVER_REPORTER_MODULE}.check_fixed_reports_async(self.s, user_path=msg.get("userPath"))\n'
            "            except Exception:\n"
            "                pass\n"
        ),
    )
    for h in hooks:
        norm = norm.replace(h, "")
    if crlf:
        norm = norm.replace("\n", "\r\n")
    return norm.encode("utf-8")


def _encode_embedded_taf_backup(mod_backups, cache_id_backups):
    doc = {
        "mods": {k: base64.b64encode(v).decode("ascii") for k, v in mod_backups.items()},
        "cacheIds": {k: base64.b64encode(v).decode("ascii") for k, v in cache_id_backups.items()},
    }
    return zlib.compress(json.dumps(doc, separators=(",", ":")).encode("utf-8"), 6)


def _decode_embedded_taf_backup(raw_blob):
    if not raw_blob:
        return {}, {}
    try:
        raw_json = zlib.decompress(raw_blob)
    except zlib.error:
        raw_json = raw_blob
    try:
        doc = json.loads(raw_json.decode("utf-8"))
        mods = {k: base64.b64decode(v) for k, v in (doc.get("mods") or {}).items()}
        cids = {k: base64.b64decode(v) for k, v in (doc.get("cacheIds") or {}).items()}
        return mods, cids
    except Exception:
        return {}, {}


def _has_brf_footprint_in_exe(exe_bytes):
    if b"EARTH_BRIDGE_RAIL_FIXES_" in exe_bytes:
        return True
    try:
        c_start, _toff, _cpos, _py, _lib, c_entries = archive_entries(exe_bytes)
        c_map = {e[7]: e for e in c_entries}
        if _carchive_key(c_map, "mod/beamng_earth/lua/ge/extensions/beamngEarth/bugreport.lua") in c_map:
            return True
        c_pyz = [e for e in c_entries if e[5] == b"z"]
        if len(c_pyz) == 1:
            pz_chk = _read_entry_bytes(exe_bytes, c_start, c_pyz[0])
            _, _, toc_chk = _read_pyz_toc(pz_chk)
            return any(k == "bridge_rail_report" or k.startswith("_brf_orig_") for k in toc_chk)
    except Exception:
        pass
    return False


def _has_taf_footprint_in_exe(exe_bytes):
    try:
        c_start, _toff, _cpos, _py, _lib, c_entries = archive_entries(exe_bytes)
        c_map = {e[7]: e for e in c_entries}
        if TAF_CARCHIVE_BACKUP_KEY in c_map or _carchive_key(c_map, MOD_TRAFFICREPORT_LUA) in c_map:
            return True
        m_ent = c_map.get(_carchive_key(c_map, MOD_MARKER_FILE))
        if m_ent is not None and _has_taf_marker(_read_entry_bytes(exe_bytes, c_start, m_ent)):
            return True
        c_pyz = [e for e in c_entries if e[5] == b"z"]
        if len(c_pyz) == 1:
            pz_chk = _read_entry_bytes(exe_bytes, c_start, c_pyz[0])
            _, _, toc_chk = _read_pyz_toc(pz_chk)
            if ORIG_EARTHD_MODULE in toc_chk or SERVER_REPORTER_MODULE in toc_chk:
                return True
    except Exception:
        pass
    return False


def _strip_taf_from_carchive_bytes(current_exe_bytes, backup_exe_bytes=None):
    """Surgically remove ONLY Traffic and AutoDrive fixes entries from current_exe_bytes in memory."""
    c_start, _c_toff, c_cookie_pos, c_py, c_lib, c_entries = archive_entries(current_exe_bytes)
    c_map = {e[7]: e for e in c_entries}
    cur_ver = _read_carchive_version(current_exe_bytes, c_start, c_map)

    emb_mods, emb_cids = {}, {}
    if TAF_CARCHIVE_BACKUP_KEY in c_map:
        emb_raw = _read_entry_bytes(current_exe_bytes, c_start, c_map[TAF_CARCHIVE_BACKUP_KEY])
        emb_mods, emb_cids = _decode_embedded_taf_backup(emb_raw)

    b_start, b_map = 0, {}
    if backup_exe_bytes:
        try:
            b_start, _bt, _bc, _bpy, _blib, b_entries = archive_entries(backup_exe_bytes)
            cand_bmap = {e[7]: e for e in b_entries}
            b_ver = _read_carchive_version(backup_exe_bytes, b_start, cand_bmap)
            if not cur_ver or not b_ver or cur_ver == b_ver:
                b_map = cand_bmap
        except Exception:
            b_map = {}

    has_brf = _has_brf_footprint_in_exe(current_exe_bytes)
    mod_key_to_rel = {_carchive_key(c_map, rel): rel for rel in MOD_FILES}
    tr_arc_key = _carchive_key(c_map, MOD_TRAFFICREPORT_LUA)
    act_arc_key = _carchive_key(c_map, MOD_ACTIONS_JSON)
    key_arc_key = _carchive_key(c_map, MOD_KEYMAP_JSON)
    ms_arc_key = _carchive_key(c_map, MOD_SCRIPT_LUA)

    c_cookie_end = c_cookie_pos + COOKIE.size
    header, _signed, checksum_at = strip_signature(current_exe_bytes, c_cookie_end)
    new_body = bytearray()
    new_toc = bytearray()

    for n, pos, length, raw, comp, kind, namebytes, name in c_entries:
        if name == TAF_CARCHIVE_BACKUP_KEY:
            continue
        if name == tr_arc_key and name not in b_map:
            continue
        out_pos = len(new_body)
        if name in mod_key_to_rel:
            rel = mod_key_to_rel[name]
            if rel in emb_mods:
                orig_mod_raw = emb_mods[rel]
                chunk = zlib.compress(orig_mod_raw, 6) if comp else orig_mod_raw
                new_body.extend(chunk)
                new_toc.extend(ENTRY.pack(n, out_pos, len(chunk), len(orig_mod_raw), comp, kind) + namebytes)
            elif name in b_map:
                be = b_map[name]
                chunk = backup_exe_bytes[b_start + be[1]:b_start + be[1] + be[2]]
                new_body.extend(chunk)
                new_toc.extend(ENTRY.pack(n, out_pos, be[2], be[3], be[4], be[5]) + namebytes)
            else:
                cur_mod = _read_entry_bytes(current_exe_bytes, c_start, (n, pos, length, raw, comp, kind, namebytes, name))
                cleaned_mod = _strip_existing_taf_marker_lines(cur_mod)
                if rel == MOD_SETTINGS_LUA:
                    cleaned_mod = unpatch_settings_lua_bytes(cleaned_mod)
                chunk = zlib.compress(cleaned_mod, 6) if comp else cleaned_mod
                new_body.extend(chunk)
                new_toc.extend(ENTRY.pack(n, out_pos, len(chunk), len(cleaned_mod), comp, kind) + namebytes)
        elif name in CARCHIVE_CACHE_ID_KEYS and not has_brf and (name in emb_cids or name in b_map):
            if name in emb_cids:
                cid_raw = emb_cids[name]
                chunk = zlib.compress(cid_raw, 6) if comp else cid_raw
                new_body.extend(chunk)
                new_toc.extend(ENTRY.pack(n, out_pos, len(chunk), len(cid_raw), comp, kind) + namebytes)
            else:
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
        elif name in (act_arc_key, key_arc_key, ms_arc_key):
            cur_raw = _read_entry_bytes(current_exe_bytes, c_start, (n, pos, length, raw, comp, kind, namebytes, name))
            if name == act_arc_key:
                un_raw = unpatch_actions_json_bytes(cur_raw)
            elif name == key_arc_key:
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
    new_carchive = new_body + new_toc + cookie
    if current_exe_bytes[:4] == b"\x7fELF":
        result_bytes = _rebuild_elf64_with_carchive(current_exe_bytes, c_start, c_cookie_end, new_carchive)
    else:
        result = bytearray(header[:c_start]) + new_carchive
        if checksum_at is not None:
            struct.pack_into("<I", result, checksum_at, pe_checksum(result, checksum_at))
        result_bytes = bytes(result)
    archive_entries(result_bytes)
    return result_bytes


def _exe_matches_desired_install(exe_bytes, payload_dir, apply_fixes, enable_reporter):
    """Content-based check: True when exe_bytes already carries this exact version/payload/options."""
    if payload_dir is None:
        return False, ""
    try:
        start, _toff, _cpos, py, _lib, entries = archive_entries(exe_bytes)
    except Exception:
        return False, ""
    entry_map = {e[7]: e for e in entries}
    gps_entry = entry_map.get(_carchive_key(entry_map, MOD_MARKER_FILE))
    if gps_entry is None:
        return False, ""
    gps_body = _read_entry_bytes(exe_bytes, start, gps_entry)
    if MARKER.encode("ascii") not in gps_body:
        return False, ""

    is_client_tier = f"tier={TIER_CLIENT_AND_REPORTER}".encode("ascii") in gps_body
    cur_tier = TIER_CLIENT_AND_REPORTER if is_client_tier else TIER_FULL_SOLVER
    if not apply_fixes and cur_tier != TIER_CLIENT_AND_REPORTER:
        return False, ""
    if apply_fixes and cur_tier != TIER_FULL_SOLVER:
        ad_b = _read_entry_bytes(exe_bytes, start, entry_map.get(_carchive_key(entry_map, MOD_AUTODRIVE_LUA)))
        nav_b = _read_entry_bytes(exe_bytes, start, entry_map.get(_carchive_key(entry_map, MOD_NAVGRAPH_LUA)))
        if not _mod_files_drifted(gps_body, ad_b, nav_b):
            return False, ""

    payload_root = Path(payload_dir)
    if apply_fixes and cur_tier == TIER_FULL_SOLVER:
        for rel in MOD_FILES:
            src = payload_root / rel
            if not src.is_file():
                return False, ""
            actual = _read_entry_bytes(exe_bytes, start, entry_map.get(_carchive_key(entry_map, rel)))
            if rel == MOD_SETTINGS_LUA:
                if b"M.SCHEMA" in actual and (b"M.ORDER" in actual or b"EXPERIMENTAL_ORDER" in actual):
                    if b"function M.autodriveTune" not in actual:
                        return False, ""
                elif actual != src.read_bytes():
                    return False, ""
            else:
                expected = _patched_mod_bytes(rel, src.read_bytes(), patch_tier=cur_tier)
                if actual != expected:
                    return False, ""

    cur_py = sys.version_info.major * 100 + sys.version_info.minor
    pyz_cands = [e for e in entries if e[5] == b"z"]
    toc = {}
    pz = b""
    off = 0
    if len(pyz_cands) == 1:
        try:
            pz = _read_entry_bytes(exe_bytes, start, pyz_cands[0])
            off, _m, toc = _read_pyz_toc(pz)
        except Exception:
            return False, ""
    has_tr_lua = _carchive_key(entry_map, MOD_TRAFFICREPORT_LUA) in entry_map
    has_pyz_rep = ORIG_EARTHD_MODULE in toc and SERVER_REPORTER_MODULE in toc
    if enable_reporter and py == cur_py:
        if not (has_tr_lua and has_pyz_rep):
            return False, ""
        tr_actual = _read_entry_bytes(exe_bytes, start, entry_map[_carchive_key(entry_map, MOD_TRAFFICREPORT_LUA)])
        if tr_actual != (payload_root / MOD_TRAFFICREPORT_LUA).read_bytes():
            return False, ""
        act_actual = _read_entry_bytes(exe_bytes, start, entry_map.get(_carchive_key(entry_map, MOD_ACTIONS_JSON)))
        if REPORT_ACTION_KEY.encode("ascii") not in act_actual:
            return False, ""
        if pz[4:8] == importlib.util.MAGIC_NUMBER and SERVER_REPORTER_MODULE in toc:
            rk, rp, rl = toc[SERVER_REPORTER_MODULE]
            if rk == 0 and 12 <= rp and rp + rl <= off:
                rco = marshal.loads(zlib.decompress(pz[rp:rp + rl]))
                if VERSION not in repr(rco.co_consts):
                    return False, ""
    elif not enable_reporter:
        if has_tr_lua or has_pyz_rep:
            return False, ""
    return True, cur_tier


def patch_executable_with_tier(
    data,
    payload_dir,
    apply_fixes=True,
    enable_reporter=False,
    payload_sha=None,
    require_mod_files=True,
    backup_hint_bytes=None,
):
    if _has_taf_footprint_in_exe(data):
        data = _strip_taf_from_carchive_bytes(data, backup_exe_bytes=backup_hint_bytes)

    start, toff, cookie_pos, py, lib, entries = archive_entries(data)
    entry_map = {e[7]: e for e in entries}
    if require_mod_files:
        for rel in MOD_FILES:
            arc_key = _carchive_key(entry_map, rel)
            if arc_key not in entry_map:
                raise PatchError(f"Expected bundled mod file missing in archive: {arc_key}")
        if enable_reporter:
            for rel in REPORTER_SHARED_MOD_FILES:
                arc_key = _carchive_key(entry_map, rel)
                if arc_key not in entry_map:
                    raise PatchError(f"Expected bundled mod file missing in archive: {arc_key}")

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
    eff_payload_sha = payload_sha if payload_sha is not None else _payload_digest(payload_dir)
    carchive_replacements = {}
    embedded_mod_backups = {}
    embedded_cid_backups = {}

    if enable_reporter and pyz_e is not None:
        cur_py = sys.version_info.major * 100 + sys.version_info.minor
        if py != cur_py:
            raise PatchError(
                f"Python {py // 100}.{py % 100} is required to patch the reporter into this executable "
                f"(running Python {cur_py // 100}.{cur_py % 100}). "
                "Run setup via BeamNG.Earth --run-addon or the 1-Click installer."
            )
        new_pz = patch_pyz_for_reporter(pz, payload_root, patch_tier=patch_tier)
        if pyz_e[4]:
            carchive_replacements[pyz_e[7]] = (zlib.compress(new_pz, 6), len(new_pz), 1, b"z")
        else:
            carchive_replacements[pyz_e[7]] = (new_pz, len(new_pz), 0, b"z")

    if apply_fixes or enable_reporter:
        for cache_key in CARCHIVE_CACHE_ID_KEYS:
            if cache_key in entry_map:
                orig_entry = entry_map[cache_key]
                orig_id_raw = _read_entry_bytes(data, start, orig_entry)
                embedded_cid_backups[cache_key] = orig_id_raw
                salted_id_raw = _salt_carchive_cache_id(
                    orig_id_raw,
                    cache_key,
                    eff_payload_sha,
                    marker=MARKER,
                    apply_fixes=bool(apply_fixes),
                )
                packed_id = zlib.compress(salted_id_raw, 6) if orig_entry[4] else salted_id_raw
                carchive_replacements[cache_key] = (packed_id, len(salted_id_raw), orig_entry[4], orig_entry[5])

    if require_mod_files and (apply_fixes or enable_reporter):
        for rel in MOD_FILES:
            arc_key = _carchive_key(entry_map, rel)
            if arc_key in entry_map:
                embedded_mod_backups[rel] = _read_entry_bytes(data, start, entry_map[arc_key])

        target_files = (
            MOD_FILES
            if (apply_fixes and patch_tier == TIER_FULL_SOLVER)
            else (MOD_MARKER_FILE,)
        )
        for rel in target_files:
            src = payload_root / rel
            if not src.is_file():
                raise PatchError(f"Payload is missing mod file: {rel}")
            arc_key = _carchive_key(entry_map, rel)
            upstream_bytes = embedded_mod_backups.get(rel)
            raw_bytes = _patched_mod_bytes(
                rel, src.read_bytes(), upstream_bytes=upstream_bytes, patch_tier=patch_tier
            )
            carchive_replacements[arc_key] = (zlib.compress(raw_bytes, 6), len(raw_bytes), 1, b"b")

        emb_blob = _encode_embedded_taf_backup(embedded_mod_backups, embedded_cid_backups)
        carchive_replacements[TAF_CARCHIVE_BACKUP_KEY] = (emb_blob, len(emb_blob), 0, b"b")

    if require_mod_files and enable_reporter:
        tr_src = payload_root / MOD_TRAFFICREPORT_LUA
        if not tr_src.is_file():
            raise PatchError(f"Payload is missing mod file: {MOD_TRAFFICREPORT_LUA}")
        tr_bytes = tr_src.read_bytes()
        act_key = _carchive_key(entry_map, MOD_ACTIONS_JSON)
        key_key = _carchive_key(entry_map, MOD_KEYMAP_JSON)
        ms_key = _carchive_key(entry_map, MOD_SCRIPT_LUA)
        tr_key = _carchive_key(entry_map, MOD_TRAFFICREPORT_LUA)
        act_bytes = patch_actions_json_bytes(_read_entry_bytes(data, start, entry_map[act_key]))
        key_bytes = patch_keymap_json_bytes(_read_entry_bytes(data, start, entry_map[key_key]))
        ms_bytes = patch_modscript_bytes(_read_entry_bytes(data, start, entry_map[ms_key]))
        carchive_replacements[tr_key] = (zlib.compress(tr_bytes, 6), len(tr_bytes), 1, b"b")
        carchive_replacements[act_key] = (zlib.compress(act_bytes, 6), len(act_bytes), 1, b"b")
        carchive_replacements[key_key] = (zlib.compress(key_bytes, 6), len(key_bytes), 1, b"b")
        carchive_replacements[ms_key] = (zlib.compress(ms_bytes, 6), len(ms_bytes), 1, b"b")

    cookie_end = cookie_pos + COOKIE.size
    header, signed, checksum_at = strip_signature(data, cookie_end)

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
    new_carchive = new_body + new_toc + cookie
    if data[:4] == b"\x7fELF":
        result_bytes = _rebuild_elf64_with_carchive(data, start, cookie_end, new_carchive)
    else:
        result = bytearray(header[:start]) + new_carchive
        if checksum_at is not None:
            struct.pack_into("<I", result, checksum_at, pe_checksum(result, checksum_at))
        result_bytes = bytes(result)
    archive_entries(result_bytes)
    return result_bytes, signed, patch_tier


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


def _splice_original_mod_files(current_exe_bytes, backup_exe_bytes=None):
    """Restore Traffic and AutoDrive fixes entries while preserving stacked sibling add-ons."""
    if not _has_taf_footprint_in_exe(current_exe_bytes):
        return None
    if not _has_brf_footprint_in_exe(current_exe_bytes) and backup_exe_bytes is not None:
        return None
    return _strip_taf_from_carchive_bytes(current_exe_bytes, backup_exe_bytes=backup_exe_bytes)


def _copy_file_mode_safe(src, dst):
    try:
        shutil.copymode(src, dst)
    except OSError:
        pass


def _write_backup_atomic(backup_path, raw_bytes, mode_ref=None):
    tmp = backup_path.with_name(f"{backup_path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_bytes(raw_bytes)
    if mode_ref is not None:
        _copy_file_mode_safe(mode_ref, tmp)
    os.replace(tmp, backup_path)


def _exe_is_authenticode_signed(data):
    if not data:
        return False
    try:
        _start, _toff, cookie_pos, _py, _lib, _entries = archive_entries(data)
        _out, signed, _cs = strip_signature(data, cookie_pos + COOKIE.size)
        return bool(signed)
    except Exception:
        return False


def _same_earth_build_carchive(a_bytes, b_bytes):
    """Return True if two PyInstaller binaries belong to the exact same upstream BeamNG.Earth build."""
    if not a_bytes or not b_bytes:
        return False
    try:
        a_start, _at, _ac, a_py, _al, a_entries = archive_entries(a_bytes)
        b_start, _bt, _bc, b_py, _bl, b_entries = archive_entries(b_bytes)
    except PatchError:
        return False
    if a_py != b_py:
        return False
    a_map = {e[7]: e for e in a_entries}
    b_map = {e[7]: e for e in b_entries}
    if _read_carchive_version(a_bytes, a_start, a_map) != _read_carchive_version(b_bytes, b_start, b_map):
        return False
    if "base_library.zip" in a_map and "base_library.zip" in b_map:
        ae, be = a_map["base_library.zip"], b_map["base_library.zip"]
        return a_bytes[a_start + ae[1]:a_start + ae[1] + ae[2]] == b_bytes[b_start + be[1]:b_start + be[1] + be[2]]
    a_pyz = [e for e in a_entries if e[5] == b"z"]
    b_pyz = [e for e in b_entries if e[5] == b"z"]
    if len(a_pyz) == 1 and len(b_pyz) == 1:
        try:
            apz = _read_entry_bytes(a_bytes, a_start, a_pyz[0])
            bpz = _read_entry_bytes(b_bytes, b_start, b_pyz[0])
            _, _, atoc = _read_pyz_toc(apz)
            _, _, btoc = _read_pyz_toc(bpz)
            for ref_mod in ("fast", "areaprops"):
                if ref_mod in atoc and ref_mod in btoc:
                    ak, ap, al = atoc[ref_mod]
                    bk, bp, bl = btoc[ref_mod]
                    if (ak, al) != (bk, bl) or apz[ap:ap + al] != bpz[bp:bp + bl]:
                        return False
            return True
        except Exception:
            return False
    return True


def _find_pristine_sibling_backup(target, ref_bytes):
    sib_backup = target.with_name(target.name + ".earthBridgeRailFixes.original")
    if not sib_backup.is_file():
        return None
    try:
        raw = sib_backup.read_bytes()
    except OSError:
        return None
    if (
        not _has_taf_footprint_in_exe(raw)
        and not _has_brf_footprint_in_exe(raw)
        and _same_earth_build_carchive(raw, ref_bytes)
    ):
        return raw
    return None


def _prepare_exe(target, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter=False):
    sidecar = target.with_name(target.name + f".{TAG}.json")
    backup = target.with_name(target.name + f".{TAG}.original")
    before = target.read_bytes()
    previous = read_json(sidecar, {})
    digest = sha256_bytes(before)
    original_sha = digest
    payload_sha = _payload_digest(payload_dir) if payload_dir is not None else previous.get("payloadSha", "")
    backup_bytes = backup.read_bytes() if backup.is_file() else None
    backup_valid_for_prev = bool(
        backup_bytes is not None
        and previous.get("originalSha")
        and sha256_bytes(backup_bytes) == previous.get("originalSha")
    )

    if operation == "restore":
        has_taf = _has_taf_footprint_in_exe(before)
        if not has_taf:
            if not previous and not backup.is_file():
                raise PatchError("No recorded Traffic and AutoDrive fixes patch for this executable")
            _cleanup_stale_pending(target, previous)
            record = dict(
                previous,
                version=VERSION,
                mode="exe",
                state="restored",
                operation="restore",
                target=str(target),
                sidecar=str(sidecar),
                status=str(status),
                config=str(config),
                message="Original executable is already active.",
                token=token,
            )
            atomic_json(sidecar, record)
            atomic_json(status, record)
            return record

        has_brf = _has_brf_footprint_in_exe(before)
        same_build_pristine = (
            backup_bytes
            if (
                backup_bytes is not None
                and not _has_taf_footprint_in_exe(backup_bytes)
                and not _has_brf_footprint_in_exe(backup_bytes)
                and _same_earth_build_carchive(backup_bytes, before)
            )
            else _find_pristine_sibling_backup(target, before)
        )
        if (
            backup_bytes is not None
            and backup_valid_for_prev
            and digest == previous.get("patchedSha")
            and (not has_brf or _has_brf_footprint_in_exe(backup_bytes))
        ):
            after = backup_bytes
            original_sha = previous["originalSha"]
            orig_cache_ids = dict(previous.get("originalCacheIds") or _read_carchive_cache_ids(after))
            salted_cache_ids = dict(previous.get("saltedCacheIds") or {})
            signed = bool(previous.get("originalSigned")) or _exe_is_authenticode_signed(after)
        elif not has_brf and same_build_pristine is not None:
            if (
                backup_bytes is same_build_pristine
                and previous.get("originalSha")
                and sha256_bytes(backup_bytes) != previous.get("originalSha")
            ):
                raise PatchError("Original executable backup is missing or checksum-mismatched")
            after = same_build_pristine
            original_sha = sha256_bytes(after)
            orig_cache_ids = dict(previous.get("originalCacheIds") or _read_carchive_cache_ids(after))
            salted_cache_ids = dict(previous.get("saltedCacheIds") or {})
            signed = bool(previous.get("originalSigned")) or _exe_is_authenticode_signed(after)
        else:
            eff_backup_hint = (
                backup_bytes
                if (backup_bytes is not None and _same_earth_build_carchive(backup_bytes, before))
                else same_build_pristine
            )
            after = _strip_taf_from_carchive_bytes(before, backup_exe_bytes=eff_backup_hint)
            original_sha = (
                previous["originalSha"]
                if backup_valid_for_prev
                else (sha256_bytes(same_build_pristine) if same_build_pristine is not None else sha256_bytes(after))
            )
            orig_cache_ids = dict(previous.get("originalCacheIds") or _read_carchive_cache_ids(eff_backup_hint or after))
            salted_cache_ids = dict(previous.get("saltedCacheIds") or {})
            signed = bool(previous.get("originalSigned")) or _exe_is_authenticode_signed(same_build_pristine)
        patch_tier = previous.get("patchTier", TIER_FULL_SOLVER)
    else:
        already_ok, cur_tier = _exe_matches_desired_install(
            before, payload_dir, apply_fixes=apply_fixes, enable_reporter=enable_reporter
        )
        pristine_ref = (
            backup_bytes
            if (
                backup_bytes is not None
                and not _has_taf_footprint_in_exe(backup_bytes)
                and not _has_brf_footprint_in_exe(backup_bytes)
                and _same_earth_build_carchive(backup_bytes, before)
            )
            else _find_pristine_sibling_backup(target, before)
        )
        if already_ok:
            _cleanup_stale_pending(target, previous)
            if not backup.is_file() and pristine_ref is not None:
                _write_backup_atomic(backup, pristine_ref, mode_ref=target)
                backup_bytes = pristine_ref
                original_sha = sha256_bytes(pristine_ref)
            elif not backup.is_file() and not _has_brf_footprint_in_exe(before):
                reconstructed = _strip_taf_from_carchive_bytes(before, backup_exe_bytes=None)
                _write_backup_atomic(backup, reconstructed, mode_ref=target)
                backup_bytes = reconstructed
                original_sha = sha256_bytes(reconstructed)
            elif pristine_ref is not None:
                original_sha = sha256_bytes(pristine_ref)
            elif backup_valid_for_prev:
                original_sha = previous["originalSha"]
            elif backup_bytes is not None:
                original_sha = sha256_bytes(backup_bytes)
            else:
                original_sha = previous.get("originalSha", digest)
            orig_signed_val = (
                bool(previous["originalSigned"])
                if "originalSigned" in previous
                else _exe_is_authenticode_signed(pristine_ref if pristine_ref is not None else backup_bytes)
            )
            record = {
                "version": VERSION,
                "mode": "exe",
                "state": "installed",
                "operation": "install",
                "patchTier": cur_tier,
                "target": str(target),
                "backup": str(backup),
                "pending": "",
                "expectedSha": digest,
                "replacementSha": digest,
                "originalSha": original_sha,
                "patchedSha": digest,
                "payloadSha": payload_sha,
                "originalSigned": orig_signed_val,
                "originalCacheIds": previous.get("originalCacheIds") or (
                    _read_carchive_cache_ids(pristine_ref if pristine_ref is not None else backup_bytes)
                    if (pristine_ref is not None or backup_bytes is not None)
                    else {}
                ),
                "saltedCacheIds": _read_carchive_cache_ids(before),
                "applyFixes": bool(apply_fixes),
                "enableReporter": bool(enable_reporter),
                "sidecar": str(sidecar),
                "status": str(status),
                "config": str(config),
                "token": token,
                "serverPid": server_pid,
                "message": "Traffic and AutoDrive fixes are installed.",
            }
            atomic_json(sidecar, record)
            atomic_json(status, record)
            return record

        has_taf = _has_taf_footprint_in_exe(before)
        has_brf = _has_brf_footprint_in_exe(before)
        if has_taf:
            if not has_brf and backup_valid_for_prev and _same_earth_build_carchive(backup_bytes, before):
                base_input = backup_bytes
                original_sha = previous["originalSha"]
            else:
                eff_backup_hint = (
                    backup_bytes
                    if (backup_bytes is not None and _same_earth_build_carchive(backup_bytes, before))
                    else pristine_ref
                )
                base_input = _strip_taf_from_carchive_bytes(before, backup_exe_bytes=eff_backup_hint)
                if pristine_ref is not None and (not backup.is_file() or backup_bytes != pristine_ref):
                    _write_backup_atomic(backup, pristine_ref, mode_ref=target)
                    backup_bytes = pristine_ref
                    original_sha = sha256_bytes(pristine_ref)
                elif not backup.is_file() and not has_brf:
                    _write_backup_atomic(backup, base_input, mode_ref=target)
                    backup_bytes = base_input
                    original_sha = sha256_bytes(base_input)
                elif backup_valid_for_prev:
                    original_sha = previous["originalSha"]
                elif backup_bytes is not None:
                    original_sha = sha256_bytes(backup_bytes)
                else:
                    original_sha = sha256_bytes(base_input)
        else:
            base_input = before
            sib_pristine = _find_pristine_sibling_backup(target, before) if has_brf else None
            if sib_pristine is not None:
                if not backup.is_file() or sha256_bytes(backup_bytes) != sha256_bytes(sib_pristine):
                    _write_backup_atomic(backup, sib_pristine, mode_ref=target)
                    backup_bytes = sib_pristine
                original_sha = sha256_bytes(sib_pristine)
            elif not has_brf or not backup.is_file():
                if not backup.is_file() or not backup_valid_for_prev or digest != previous.get("originalSha"):
                    _write_backup_atomic(backup, before, mode_ref=target)
                    backup_bytes = before
                original_sha = sha256_bytes(backup_bytes)
            else:
                original_sha = previous.get("originalSha") or sha256_bytes(backup_bytes)

        orig_cache_ids = _read_carchive_cache_ids(backup_bytes if backup_bytes is not None else base_input)
        after, signed_patch, patch_tier = patch_executable_with_tier(
            base_input,
            payload_dir,
            apply_fixes=apply_fixes,
            enable_reporter=enable_reporter,
            payload_sha=payload_sha,
            backup_hint_bytes=backup_bytes,
        )
        signed = (
            bool(previous["originalSigned"])
            if "originalSigned" in previous
            else (_exe_is_authenticode_signed(backup_bytes) if backup_bytes is not None else signed_patch)
        )
        salted_cache_ids = _read_carchive_cache_ids(after)

    _cleanup_stale_pending(target, previous)
    pending = target.with_name(f"{target.name}.{TAG}.{uuid.uuid4().hex}.pending")
    with pending.open("xb") as stream:
        stream.write(after)
    _copy_file_mode_safe(target, pending)
    if sha256_bytes(pending.read_bytes()) != sha256_bytes(after):
        raise PatchError("Staged patch checksum verification failed")

    patched_sha = sha256_bytes(after) if operation == "install" else previous.get("patchedSha", "")
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
        "originalCacheIds": orig_cache_ids,
        "saltedCacheIds": salted_cache_ids,
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


def _source_matches_desired_install(root, planned):
    for rel, expected_bytes in planned:
        dest = root / rel
        if not dest.is_file():
            return False
        cur = dest.read_bytes()
        if rel in REPORTER_SHARED_MOD_FILES:
            if rel == MOD_ACTIONS_JSON and REPORT_ACTION_KEY.encode("ascii") not in cur:
                return False
            if rel == MOD_KEYMAP_JSON and REPORT_ACTION_KEY.encode("ascii") not in cur:
                return False
            if rel == MOD_SCRIPT_LUA and b"beamngEarth_trafficreport" not in cur:
                return False
        elif rel == "server/earthd.py":
            if SERVER_REPORTER_MODULE.encode("ascii") not in cur and MARKER.encode("ascii") not in cur:
                return False
        elif cur != expected_bytes:
            return False
    if not any(r == MOD_TRAFFICREPORT_LUA for r, _ in planned):
        if (root / MOD_TRAFFICREPORT_LUA).exists():
            return False
    return True


def _prepare_source(root, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter=False):
    state_dir = root / f".{TAG}"
    sidecar = state_dir / "receipt.json"
    backup_dir = state_dir / "backups"
    previous = read_json(sidecar, {})
    payload_sha = _payload_digest(payload_dir) if payload_dir is not None else previous.get("payloadSha", "")

    if operation == "restore":
        if not previous or not previous.get("files"):
            gps_f = root / MOD_MARKER_FILE
            if gps_f.is_file() and not _has_taf_marker(gps_f.read_bytes()):
                record = {
                    "version": VERSION,
                    "mode": "source",
                    "state": "restored",
                    "operation": "restore",
                    "target": str(root),
                    "sidecar": str(sidecar),
                    "status": str(status),
                    "config": str(config),
                    "token": token,
                    "message": "Original source files are already active.",
                }
                atomic_json(sidecar, record)
                atomic_json(status, record)
                return record
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
                    cur_sha = sha256_bytes(dest.read_bytes())
                    items.append({
                        "rel": rel,
                        "action": "delete",
                        "expectedSha": cur_sha,
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
            cur = root / rel
            cur_b = cur.read_bytes()
            if rel == MOD_ACTIONS_JSON:
                return unpatch_actions_json_bytes(cur_b)
            if rel == MOD_KEYMAP_JSON:
                return unpatch_keymap_json_bytes(cur_b)
            if rel == MOD_SCRIPT_LUA:
                return unpatch_modscript_bytes(cur_b)
            if rel == "server/earthd.py":
                return unpatch_source_earthd_bytes(cur_b)
            return cur_b

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

    if _source_matches_desired_install(root, planned):
        _cleanup_stale_stage(state_dir, previous)
        record = dict(
            previous,
            version=VERSION,
            mode="source",
            state="installed",
            operation="install",
            patchTier=patch_tier,
            target=str(root),
            sidecar=str(sidecar),
            status=str(status),
            config=str(config),
            token=token,
            serverPid=server_pid,
            payloadSha=payload_sha,
            applyFixes=bool(apply_fixes),
            enableReporter=bool(enable_reporter),
            message="Traffic and AutoDrive fixes are already active in this source checkout.",
        )
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
        cur_exists = dest.is_file()
        cur_bytes = dest.read_bytes() if cur_exists else b""
        cur_sha = sha256_bytes(cur_bytes) if cur_exists else ""
        if prev_entry and not prev_entry["added"]:
            bfile = backup_dir / rel
            if bfile.is_file() and sha256_bytes(bfile.read_bytes()) == prev_entry["originalSha"]:
                orig_sha = prev_entry["originalSha"]
            else:
                orig_sha = cur_sha
                bfile.parent.mkdir(parents=True, exist_ok=True)
                bfile.write_bytes(cur_bytes)
            added = False
            expected_sha = cur_sha
        elif prev_entry and prev_entry["added"]:
            orig_sha = ""
            added = True
            expected_sha = cur_sha
        elif cur_exists:
            orig_bytes = _unpatch_shared_source_file(rel, cur_bytes) or _strip_existing_taf_marker_lines(cur_bytes)
            if rel == MOD_SETTINGS_LUA:
                orig_bytes = unpatch_settings_lua_bytes(orig_bytes)
            orig_sha = sha256_bytes(orig_bytes)
            bfile = backup_dir / rel
            if not bfile.is_file():
                bfile.parent.mkdir(parents=True, exist_ok=True)
                bfile.write_bytes(orig_bytes)
            else:
                orig_sha = sha256_bytes(bfile.read_bytes())
            added = False
            expected_sha = cur_sha
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
            if dest.is_file():
                cur_b = dest.read_bytes()
                spliced = _unpatch_shared_source_file(prev_rel, cur_b)
                if spliced is not None:
                    sfile = stage_dir / prev_rel
                    sfile.parent.mkdir(parents=True, exist_ok=True)
                    sfile.write_bytes(spliced)
                    items.append({
                        "rel": prev_rel,
                        "action": "write",
                        "expectedSha": sha256_bytes(cur_b),
                        "replacementSha": sha256_bytes(spliced),
                        "staged": str(sfile),
                    })
                elif bfile.is_file() and sha256_bytes(bfile.read_bytes()) == prev_entry["originalSha"]:
                    items.append({
                        "rel": prev_rel,
                        "action": "write",
                        "expectedSha": sha256_bytes(cur_b),
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


def _clean_posix_env():
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("_PYI_", "_MEIPASS"))
        and k not in ("PYTHONHOME", "PYTHONPATH", "TCL_LIBRARY", "TK_LIBRARY", "BNGEARTH_PARENT")
    }
    if "LD_LIBRARY_PATH_ORIG" in env:
        orig_ld = env.pop("LD_LIBRARY_PATH_ORIG")
        if orig_ld:
            env["LD_LIBRARY_PATH"] = orig_ld
        else:
            env.pop("LD_LIBRARY_PATH", None)
    elif getattr(sys, "frozen", False):
        env.pop("LD_LIBRARY_PATH", None)
    return env


def _valid_squashfs_superblock(header_buf, offset, total_file_size):
    if offset < 64 or offset + 96 > len(header_buf):
        return False
    if header_buf[offset:offset + 4] != b"hsqs":
        return False
    s_major, s_minor = struct.unpack_from("<HH", header_buf, offset + 28)
    bytes_used = struct.unpack_from("<Q", header_buf, offset + 40)[0]
    if (s_major, s_minor) != (4, 0):
        return False
    return 0 < bytes_used <= (total_file_size - offset)


def _find_appimage_squashfs_offset(path_or_bytes):
    """Locate the Type 2 AppImage SquashFS superblock dynamically without hardcoded offsets."""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        total_size = len(path_or_bytes)
        buf = bytes(path_or_bytes[:min(total_size, 4 * 1024 * 1024)])
    else:
        p = Path(path_or_bytes)
        try:
            total_size = p.stat().st_size
            with p.open("rb") as f:
                buf = f.read(min(total_size, 4 * 1024 * 1024))
        except OSError:
            return None
    if len(buf) < 128 or buf[:4] != b"\x7fELF":
        return None
    if buf[4] == 2 and buf[5] == 1:
        e_shoff = struct.unpack_from("<Q", buf, 40)[0]
        e_shentsize, e_shnum = struct.unpack_from("<HH", buf, 58)
        cand = e_shoff + e_shnum * e_shentsize
        if _valid_squashfs_superblock(buf, cand, total_size):
            return cand
    elif buf[4] == 1 and buf[5] == 1:
        e_shoff = struct.unpack_from("<I", buf, 32)[0]
        e_shentsize, e_shnum = struct.unpack_from("<HH", buf, 46)
        cand = e_shoff + e_shnum * e_shentsize
        if _valid_squashfs_superblock(buf, cand, total_size):
            return cand
    pos = 64
    while True:
        idx = buf.find(b"hsqs", pos)
        if idx < 0:
            break
        if _valid_squashfs_superblock(buf, idx, total_size):
            return idx
        pos = idx + 4
    return None


def _parse_appimage_wrapper(path_or_bytes):
    if isinstance(path_or_bytes, (bytes, bytearray)):
        head = bytes(path_or_bytes[:4096])
    else:
        try:
            with Path(path_or_bytes).open("rb") as f:
                head = f.read(4096)
        except OSError:
            return None
    if APPIMAGE_WRAPPER_MAGIC not in head:
        return None
    meta = {}
    for line in head.decode("utf-8", errors="replace").splitlines():
        if line.startswith("# APPDIR_ROOT="):
            meta["appdir"] = line.split("=", 1)[1].strip()
        elif line.startswith("# EARTH_VERSION="):
            meta["earthVersion"] = line.split("=", 1)[1].strip()
        elif line.startswith("# PATCH_TIER="):
            meta["patchTier"] = line.split("=", 1)[1].strip()
    return meta


def _is_host_zip_file(path):
    try:
        if not zipfile.is_zipfile(path):
            return False
        with zipfile.ZipFile(path, "r") as zf:
            names = set(zf.namelist())
            return any(n.endswith("Resources/Client/beamng_earth.zip") for n in names)
    except Exception:
        return False


def _resolve_target_path(target):
    target_path = Path(target).resolve()
    if target_path.is_dir():
        if (target_path / "server" / "earthd.py").is_file():
            return target_path, "source"
        if (target_path / "Resources" / "Client" / "beamng_earth.zip").is_file():
            return target_path, "host_dir"
        return target_path, "source"
    if target_path.is_file():
        try:
            with target_path.open("rb") as f:
                head = f.read(4096)
        except OSError as exc:
            raise PatchError(f"Could not read target: {exc}") from exc
        if (
            _parse_appimage_wrapper(head) is not None
            or target_path.name.lower().endswith(".appimage")
            or (head[:4] == b"\x7fELF" and head[8:11] == b"AI\x02")
            or _find_appimage_squashfs_offset(target_path) is not None
        ):
            return target_path, "appimage"
        if target_path.suffix.lower() == ".zip" and _is_host_zip_file(target_path):
            return target_path, "host_zip"
        return target_path, "exe"
    raise PatchError(f"Target not found: {target_path}")


def _patch_client_mod_zip_bytes(orig_zip_bytes, payload_dir, apply_fixes=True, enable_reporter=False, patch_tier=TIER_FULL_SOLVER):
    """Rewrite mod/beamng_earth entries inside a BeamNG.drive client mod .zip cleanly."""
    payload_root = Path(payload_dir)
    with zipfile.ZipFile(io.BytesIO(orig_zip_bytes), "r") as zin:
        orig_infos = zin.infolist()
        orig_map = {zi.filename.replace("\\", "/"): zin.read(zi.filename) for zi in orig_infos}

    prefix = ""
    if any(k.startswith("lua/ge/extensions/beamngEarth/") for k in orig_map):
        prefix = ""
    elif any(k.startswith("mod/beamng_earth/lua/") for k in orig_map):
        prefix = "mod/beamng_earth/"

    replacements = {}
    if apply_fixes or enable_reporter:
        target_files = (
            MOD_FILES
            if (apply_fixes and patch_tier == TIER_FULL_SOLVER)
            else (MOD_MARKER_FILE,)
        )
        for rel in target_files:
            short = rel.removeprefix("mod/beamng_earth/")
            zkey = prefix + short
            src = payload_root / rel
            if not src.is_file():
                raise PatchError(f"Payload is missing mod file: {rel}")
            replacements[zkey] = _patched_mod_bytes(
                rel, src.read_bytes(), upstream_bytes=orig_map.get(zkey), patch_tier=patch_tier
            )

    if enable_reporter:
        act_key = prefix + MOD_ACTIONS_JSON.removeprefix("mod/beamng_earth/")
        km_key = prefix + MOD_KEYMAP_JSON.removeprefix("mod/beamng_earth/")
        ms_key = prefix + MOD_SCRIPT_LUA.removeprefix("mod/beamng_earth/")
        tr_key = prefix + MOD_TRAFFICREPORT_LUA.removeprefix("mod/beamng_earth/")
        tr_src = payload_root / MOD_TRAFFICREPORT_LUA
        if tr_src.is_file() and act_key in orig_map and km_key in orig_map and ms_key in orig_map:
            replacements[tr_key] = tr_src.read_bytes()
            replacements[act_key] = patch_actions_json_bytes(orig_map[act_key])
            replacements[km_key] = patch_keymap_json_bytes(orig_map[km_key])
            replacements[ms_key] = patch_modscript_bytes(orig_map[ms_key])

    out_buf = io.BytesIO()
    written = set()
    with zipfile.ZipFile(io.BytesIO(orig_zip_bytes), "r") as zin:
        with zipfile.ZipFile(out_buf, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for zi in zin.infolist():
                norm_name = zi.filename.replace("\\", "/")
                if norm_name in replacements:
                    new_zi = zipfile.ZipInfo(zi.filename, date_time=zi.date_time)
                    new_zi.compress_type = zipfile.ZIP_DEFLATED
                    new_zi.external_attr = zi.external_attr
                    zout.writestr(new_zi, replacements[norm_name])
                    written.add(norm_name)
                else:
                    zout.writestr(zi, zin.read(zi.filename))
            for norm_name, raw_b in sorted(replacements.items()):
                if norm_name not in written:
                    new_zi = zipfile.ZipInfo(norm_name, date_time=(2026, 1, 1, 0, 0, 0))
                    new_zi.compress_type = zipfile.ZIP_DEFLATED
                    zout.writestr(new_zi, raw_b)
    return out_buf.getvalue()


def _extract_appimage_to_dir(appimage_path, dest_dir, subset_paths=None):
    """Extract a Type 2 AppImage into dest_dir via unsquashfs, --appimage-extract, or synthetic test zip."""
    appimage_path = Path(appimage_path).resolve()
    dest_dir = Path(dest_dir)
    if dest_dir.exists():
        shutil.rmtree(dest_dir, ignore_errors=True)
    sqfs_off = _find_appimage_squashfs_offset(appimage_path)

    # Synthetic unit-test AppImage trailer hook (allows full offline testing on Windows without unsquashfs)
    if sqfs_off is not None:
        try:
            with appimage_path.open("rb") as f:
                f.seek(sqfs_off + 96)
                probe = f.read(4)
                if probe == b"PK\x03\x04":
                    f.seek(sqfs_off + 96)
                    zf_bytes = f.read()
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    with zipfile.ZipFile(io.BytesIO(zf_bytes), "r") as zf:
                        for zi in zf.infolist():
                            rel = Path(zi.filename)
                            if rel.is_absolute() or ".." in rel.parts:
                                continue
                            out_p = dest_dir / rel
                            if zi.is_dir():
                                out_p.mkdir(parents=True, exist_ok=True)
                            else:
                                out_p.parent.mkdir(parents=True, exist_ok=True)
                                out_p.write_bytes(zf.read(zi.filename))
                                mode_bits = (zi.external_attr >> 16) & 0o777
                                if mode_bits:
                                    try:
                                        os.chmod(out_p, mode_bits)
                                    except OSError:
                                        pass
                    return dest_dir, sqfs_off
        except Exception:
            pass

    unsquashfs = shutil.which("unsquashfs")
    if unsquashfs and sqfs_off is not None:
        cmd = [unsquashfs, "-no-xattrs", "-f", "-o", str(sqfs_off), "-d", str(dest_dir), str(appimage_path)]
        if subset_paths:
            cmd.extend(subset_paths)
        res = subprocess.run(cmd, env=_clean_posix_env(), capture_output=True, text=True, timeout=180)
        if res.returncode == 0 and dest_dir.is_dir():
            return dest_dir, sqfs_off

    if os.name != "nt":
        with tempfile.TemporaryDirectory(dir=str(dest_dir.parent)) as work_tmp:
            work_path = Path(work_tmp)
            run_bin = appimage_path
            if not os.access(run_bin, os.X_OK):
                run_bin = work_path / "extract_runner.AppImage"
                shutil.copy2(appimage_path, run_bin)
                os.chmod(run_bin, 0o755)
            cmd = [str(run_bin), "--appimage-extract"]
            res = subprocess.run(
                cmd,
                cwd=str(work_path),
                env=dict(_clean_posix_env(), APPIMAGE_EXTRACT_AND_RUN="1"),
                capture_output=True,
                text=True,
                timeout=180,
            )
            extracted = work_path / "squashfs-root"
            if res.returncode == 0 and extracted.is_dir():
                shutil.move(str(extracted), str(dest_dir))
                return dest_dir, sqfs_off

    raise PatchError(
        "Could not extract Linux AppImage (install squashfs-tools or ensure the AppImage has execute permission)."
    )


def _locate_appdir_bundle(appdir_root):
    """Return (elf_bin, internal_dir) inside an extracted AppImage squashfs-root."""
    appdir_root = Path(appdir_root)
    cand_dir = appdir_root / "usr" / "lib" / "BeamNG.Earth"
    if (cand_dir / "BeamNG.Earth").is_file() and (cand_dir / "_internal").is_dir():
        return cand_dir / "BeamNG.Earth", cand_dir / "_internal"
    for internal in appdir_root.rglob("_internal"):
        if internal.is_dir() and (internal / "mod" / "beamng_earth").is_dir():
            sib = internal.parent / "BeamNG.Earth"
            if sib.is_file():
                return sib, internal
    apprun = appdir_root / "AppRun"
    if apprun.is_file():
        return apprun, appdir_root
    return None, None


def _inspect_extracted_appdir(appdir_root):
    elf_bin, internal_dir = _locate_appdir_bundle(appdir_root)
    if elf_bin is None or internal_dir is None:
        return {
            "compatible": False,
            "state": "incompatible",
            "reason": "AppImage does not contain an expected BeamNG.Earth bundle.",
            "earthVersion": "",
            "pythonVersion": "",
            "pythonMatches": False,
            "patched": False,
            "patchTier": "",
            "missingModules": [],
            "missingModFiles": [],
        }

    ver_text = ""
    vf = internal_dir / "VERSION"
    if vf.is_file():
        ver_text = vf.read_text(encoding="utf-8", errors="replace").strip().splitlines()[0].strip()

    elf_data = elf_bin.read_bytes()
    start, _toff, _cpos, py, _lib, entries = archive_entries(elf_data)
    py_ver_str = f"{py // 100}.{py % 100}"
    cur_py = sys.version_info.major * 100 + sys.version_info.minor
    entry_map = {e[7]: e for e in entries}
    if not ver_text:
        ver_text = _read_carchive_version(elf_data, start, entry_map)

    pyz_candidates = [e for e in entries if e[5] == b"z"]
    if len(pyz_candidates) != 1:
        return {
            "compatible": False,
            "state": "incompatible",
            "reason": "AppImage ELF binary is missing embedded PYZ archive.",
            "earthVersion": ver_text,
            "pythonVersion": py_ver_str,
            "pythonMatches": py == cur_py,
            "patched": False,
            "patchTier": "",
            "missingModules": [],
            "missingModFiles": [],
        }

    pz = _read_entry_bytes(elf_data, start, pyz_candidates[0])
    off, _magic, toc = _read_pyz_toc(pz)
    missing_mods = [m for m in REQUIRED_UPSTREAM_MODULES if m not in toc]
    missing_mod_files = [rel for rel in MOD_FILES if not (internal_dir / rel).is_file()]
    gps_file = internal_dir / MOD_MARKER_FILE
    gps_bytes = gps_file.read_bytes() if gps_file.is_file() else b""
    patched = _has_taf_marker(gps_bytes)

    if missing_mods or missing_mod_files:
        parts = []
        if missing_mods:
            parts.append("missing server modules: " + ", ".join(missing_mods))
        if missing_mod_files:
            parts.append("missing mod files: " + ", ".join(missing_mod_files))
        return {
            "compatible": False,
            "state": "incompatible",
            "reason": (
                f"Incompatible BeamNG.Earth AppImage ({ver_text or 'unknown'}: {'; '.join(parts)}). "
                f"Requires BeamNG.Earth {MIN_SUPPORTED_EARTH_VERSION} or newer."
            ),
            "earthVersion": ver_text,
            "pythonVersion": py_ver_str,
            "pythonMatches": py == cur_py,
            "patched": patched,
            "patchTier": "",
            "missingModules": missing_mods,
            "missingModFiles": missing_mod_files,
        }

    if _version_looks_older(ver_text):
        return {
            "compatible": False,
            "state": "incompatible",
            "reason": f"BeamNG.Earth {ver_text} is too old. Requires {MIN_SUPPORTED_EARTH_VERSION} or newer.",
            "earthVersion": ver_text,
            "pythonVersion": py_ver_str,
            "pythonMatches": py == cur_py,
            "patched": patched,
            "patchTier": "",
            "missingModules": [],
            "missingModFiles": [],
        }

    patch_tier = TIER_FULL_SOLVER
    if f"tier={TIER_CLIENT_AND_REPORTER}".encode("ascii") in gps_bytes:
        patch_tier = TIER_CLIENT_AND_REPORTER
    elif not patched:
        ad_b = (internal_dir / MOD_AUTODRIVE_LUA).read_bytes()
        nav_b = (internal_dir / MOD_NAVGRAPH_LUA).read_bytes()
        if _mod_files_drifted(gps_bytes, ad_b, nav_b):
            patch_tier = TIER_CLIENT_AND_REPORTER

    return {
        "compatible": True,
        "state": "installed" if patched else "unpatched",
        "reason": "",
        "earthVersion": ver_text,
        "pythonVersion": py_ver_str,
        "pythonMatches": py == cur_py,
        "patched": patched,
        "patchTier": patch_tier,
        "missingModules": [],
        "missingModFiles": [],
    }


def _patch_extracted_appdir(appdir_root, payload_dir, apply_fixes=True, enable_reporter=False, payload_sha=None):
    """Patch _internal mod files, _internal/{CODE_ID,REVISIT_ID}, and BeamNG.Earth ELF PYZ in an extracted AppDir."""
    elf_bin, internal_dir = _locate_appdir_bundle(appdir_root)
    if elf_bin is None or internal_dir is None:
        raise PatchError("AppImage bundle layout could not be located after extraction")
    inspected = _inspect_extracted_appdir(appdir_root)
    if not inspected["compatible"]:
        raise PatchError(inspected["reason"] or "Incompatible BeamNG.Earth AppImage")

    patch_tier = inspected["patchTier"] if apply_fixes else TIER_CLIENT_AND_REPORTER
    payload_root = Path(payload_dir)
    eff_payload_sha = payload_sha if payload_sha is not None else _payload_digest(payload_dir)

    orig_cache_ids = {}
    salted_cache_ids = {}
    for cache_key in CARCHIVE_CACHE_ID_KEYS:
        cid_file = internal_dir / cache_key
        if cid_file.is_file():
            orig_raw = cid_file.read_bytes()
            orig_cache_ids[cache_key] = orig_raw.decode("ascii", "replace")
            salted_raw = _salt_carchive_cache_id(
                orig_raw, cache_key, eff_payload_sha, marker=MARKER, apply_fixes=bool(apply_fixes)
            )
            cid_file.write_bytes(salted_raw)
            salted_cache_ids[cache_key] = salted_raw.decode("ascii", "replace")

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
            dest = internal_dir / rel
            upstream_b = dest.read_bytes() if dest.is_file() else None
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(
                _patched_mod_bytes(rel, src.read_bytes(), upstream_bytes=upstream_b, patch_tier=patch_tier)
            )

    reporter_applied = False
    if enable_reporter:
        elf_bytes = elf_bin.read_bytes()
        _s, _t, _c, py, _l, _e = archive_entries(elf_bytes)
        cur_py = sys.version_info.major * 100 + sys.version_info.minor
        if py == cur_py and all((internal_dir / r).is_file() for r in REPORTER_SHARED_MOD_FILES):
            rebuilt_elf, _signed, _et = patch_executable_with_tier(
                elf_bytes,
                payload_root,
                apply_fixes=apply_fixes,
                enable_reporter=True,
                payload_sha=eff_payload_sha,
                require_mod_files=False,
            )
            elf_mode = stat.S_IMODE(elf_bin.stat().st_mode) or 0o755
            elf_bin.write_bytes(rebuilt_elf)
            try:
                os.chmod(elf_bin, elf_mode)
            except OSError:
                pass
            tr_src = payload_root / MOD_TRAFFICREPORT_LUA
            if tr_src.is_file():
                (internal_dir / MOD_TRAFFICREPORT_LUA).write_bytes(tr_src.read_bytes())
            act_p = internal_dir / MOD_ACTIONS_JSON
            km_p = internal_dir / MOD_KEYMAP_JSON
            ms_p = internal_dir / MOD_SCRIPT_LUA
            act_p.write_bytes(patch_actions_json_bytes(act_p.read_bytes()))
            km_p.write_bytes(patch_keymap_json_bytes(km_p.read_bytes()))
            ms_p.write_bytes(patch_modscript_bytes(ms_p.read_bytes()))
            reporter_applied = True

    return patch_tier, inspected["earthVersion"], orig_cache_ids, salted_cache_ids, reporter_applied


def _build_appimage_wrapper_bytes(appdir_path, earth_version, patch_tier):
    q_appdir = str(Path(appdir_path).resolve()).replace('"', '\\"')
    lines = [
        "#!/usr/bin/env sh",
        "# EARTH_AUTODRIVE_FIXES_APPIMAGE_WRAPPER",
        f"# APPDIR_ROOT={Path(appdir_path).resolve()}",
        f"# EARTH_VERSION={earth_version}",
        f"# PATCH_TIER={patch_tier}",
        'APPIMAGE="$(readlink -f "$0" 2>/dev/null || printf \'%s\' "$0")"',
        "export APPIMAGE",
        f'exec "{q_appdir}/AppRun" "$@"',
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _try_repack_appimage(pristine_appimage, sqfs_off, stage_appdir, out_pending):
    mode_pref = os.environ.get("BNE_ADF_APPIMAGE_MODE", "auto").strip().lower()
    if mode_pref == "wrapper":
        return False
    mksquashfs = shutil.which("mksquashfs")
    if not mksquashfs or sqfs_off is None:
        return False
    sqfs_tmp = out_pending.with_name(f"{out_pending.name}.sqfs")
    try:
        for comp_args in (["-comp", "zstd", "-Xcompression-level", "3"], ["-comp", "gzip"]):
            if sqfs_tmp.exists():
                sqfs_tmp.unlink()
            cmd = [
                mksquashfs,
                str(stage_appdir),
                str(sqfs_tmp),
                "-root-owned",
                "-noappend",
                "-no-exports",
                "-no-xattrs",
                *comp_args,
            ]
            res = subprocess.run(cmd, env=_clean_posix_env(), capture_output=True, text=True, timeout=300)
            if res.returncode == 0 and sqfs_tmp.is_file():
                with Path(pristine_appimage).open("rb") as fin:
                    runtime_prefix = fin.read(sqfs_off)
                with out_pending.open("xb") as fout:
                    fout.write(runtime_prefix)
                    with sqfs_tmp.open("rb") as fsq:
                        shutil.copyfileobj(fsq, fout, length=1024 * 1024)
                try:
                    os.chmod(out_pending, 0o755)
                except OSError:
                    pass
                return True
    finally:
        if sqfs_tmp.exists():
            try:
                sqfs_tmp.unlink()
            except OSError:
                pass
    return False


def _prepare_appimage(target, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter=False):
    sidecar = target.with_name(target.name + f".{TAG}.json")
    backup = target.with_name(target.name + f".{TAG}.original")
    previous = read_json(sidecar, {})
    digest = sha256_file(target)
    original_sha = digest
    payload_sha = _payload_digest(payload_dir) if payload_dir is not None else previous.get("payloadSha", "")
    state_parent = Path(status).resolve().parent
    state_parent.mkdir(parents=True, exist_ok=True)
    appdir_id = sha256_bytes(str(target).encode("utf-8"))[:10]
    appdir_store = state_parent / f"patched_appdir_{appdir_id}"

    if operation == "restore":
        if not previous:
            raise PatchError("No recorded Traffic and AutoDrive fixes patch for this AppImage")
        if digest == previous.get("originalSha"):
            _cleanup_stale_pending(target, previous)
            if appdir_store.is_dir():
                shutil.rmtree(appdir_store, ignore_errors=True)
            record = dict(previous, state="restored", message="Original AppImage is already active.", token=token)
            atomic_json(sidecar, record)
            atomic_json(status, record)
            return record
        if not backup.is_file() or sha256_file(backup) != previous.get("originalSha"):
            raise PatchError("Original AppImage backup is missing or checksum-mismatched")
        if digest != previous.get("patchedSha"):
            raise PatchError("AppImage changed after patching; restoration aborted to protect updates")
        _cleanup_stale_pending(target, previous)
        pending = target.with_name(f"{target.name}.{TAG}.{uuid.uuid4().hex}.pending")
        shutil.copy2(backup, pending)
        try:
            os.chmod(pending, 0o755)
        except OSError:
            pass
        rep_sha = sha256_file(pending)
        if rep_sha != previous["originalSha"]:
            raise PatchError("Staged restore checksum verification failed")
        record = {
            "version": VERSION,
            "mode": "appimage",
            "state": "pending",
            "operation": "restore",
            "patchTier": previous.get("patchTier", TIER_FULL_SOLVER),
            "target": str(target),
            "backup": str(backup),
            "pending": str(pending),
            "patchedAppDir": previous.get("patchedAppDir", str(appdir_store)),
            "expectedSha": digest,
            "replacementSha": rep_sha,
            "originalSha": previous["originalSha"],
            "patchedSha": previous["patchedSha"],
            "originalCacheIds": previous.get("originalCacheIds", {}),
            "saltedCacheIds": previous.get("saltedCacheIds", {}),
            "payloadSha": payload_sha,
            "applyFixes": False,
            "enableReporter": False,
            "sidecar": str(sidecar),
            "status": str(status),
            "config": str(config),
            "token": token,
            "serverPid": server_pid,
            "message": "Original AppImage restore staged.",
        }
        atomic_json(sidecar, record)
        atomic_json(status, record)
        return record

    pristine_path = target
    refresh_appimage_backup = False
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
        if not backup.is_file() or sha256_file(backup) != previous.get("originalSha"):
            raise PatchError("Original AppImage backup could not be verified; existing patch preserved")
        pristine_path = backup
        original_sha = previous["originalSha"]
    elif backup.exists() and (
        not previous
        or sha256_file(backup) != previous.get("originalSha")
        or digest != previous.get("originalSha")
    ):
        cur_info = inspect_target(target)
        if not cur_info.get("patched"):
            refresh_appimage_backup = True
        elif backup.is_file() and (not previous or sha256_file(backup) == previous.get("originalSha")):
            pristine_path = backup
            original_sha = sha256_file(backup)
        else:
            raise PatchError("Existing backup belongs to a different AppImage build; it was preserved")

    stage_parent = state_parent / f"appimg_work_{uuid.uuid4().hex[:8]}"
    stage_parent.mkdir(parents=True, exist_ok=True)
    stage_appdir = stage_parent / "squashfs-root"
    _cleanup_stale_pending(target, previous)
    pending = target.with_name(f"{target.name}.{TAG}.{uuid.uuid4().hex}.pending")

    try:
        _extracted, sqfs_off = _extract_appimage_to_dir(pristine_path, stage_appdir)
        patch_tier, earth_ver, orig_cache_ids, salted_cache_ids, rep_applied = _patch_extracted_appdir(
            stage_appdir,
            payload_dir,
            apply_fixes=apply_fixes,
            enable_reporter=enable_reporter,
            payload_sha=payload_sha,
        )
        if not backup.exists() or refresh_appimage_backup:
            shutil.copy2(pristine_path, backup)
            try:
                os.chmod(backup, 0o755)
            except OSError:
                pass
        if sha256_file(backup) != original_sha:
            raise PatchError("AppImage backup verification failed; original untouched")

        repacked = _try_repack_appimage(pristine_path, sqfs_off, stage_appdir, pending)
        used_appdir = ""
        if not repacked:
            if appdir_store.exists():
                shutil.rmtree(appdir_store, ignore_errors=True)
            shutil.move(str(stage_appdir), str(appdir_store))
            wrapper_bytes = _build_appimage_wrapper_bytes(appdir_store, earth_ver, patch_tier)
            with pending.open("xb") as f:
                f.write(wrapper_bytes)
            try:
                os.chmod(pending, 0o755)
            except OSError:
                pass
            used_appdir = str(appdir_store)
        elif appdir_store.exists():
            shutil.rmtree(appdir_store, ignore_errors=True)
    finally:
        if stage_parent.exists():
            shutil.rmtree(stage_parent, ignore_errors=True)

    rep_sha = sha256_file(pending)
    record = {
        "version": VERSION,
        "mode": "appimage",
        "appimagePackaging": "squashfs" if repacked else "wrapper",
        "state": "pending",
        "operation": "install",
        "patchTier": patch_tier,
        "target": str(target),
        "backup": str(backup),
        "pending": str(pending),
        "patchedAppDir": used_appdir,
        "expectedSha": digest,
        "replacementSha": rep_sha,
        "originalSha": original_sha,
        "patchedSha": rep_sha,
        "originalCacheIds": orig_cache_ids,
        "saltedCacheIds": salted_cache_ids,
        "payloadSha": payload_sha,
        "applyFixes": bool(apply_fixes),
        "enableReporter": bool(rep_applied),
        "sidecar": str(sidecar),
        "status": str(status),
        "config": str(config),
        "token": token,
        "serverPid": server_pid,
        "message": "Traffic and AutoDrive fixes staged for Linux AppImage.",
    }
    atomic_json(sidecar, record)
    atomic_json(status, record)
    return record


def _prepare_host(target, mode, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter=False):
    """Patch or restore BeamNG.Earth-Host directory or .zip archive (Resources/Client/beamng_earth.zip)."""
    if mode == "host_dir":
        mod_zip = target / "Resources" / "Client" / "beamng_earth.zip"
        state_dir = target / f".{TAG}"
        sidecar = state_dir / "receipt.json"
        backup = state_dir / "beamng_earth.zip.original"
    else:
        mod_zip = target
        sidecar = target.with_name(target.name + f".{TAG}.json")
        backup = target.with_name(target.name + f".{TAG}.original")

    previous = read_json(sidecar, {})
    before = mod_zip.read_bytes()
    digest = sha256_bytes(before)
    original_sha = digest
    payload_sha = _payload_digest(payload_dir) if payload_dir is not None else previous.get("payloadSha", "")

    if operation == "restore":
        if not previous:
            raise PatchError("No recorded Traffic and AutoDrive fixes patch for this Host target")
        if digest == previous.get("originalSha"):
            _cleanup_stale_pending(mod_zip, previous)
            record = dict(previous, state="restored", message="Original Host archive is already active.", token=token)
            atomic_json(sidecar, record)
            atomic_json(status, record)
            return record
        if not backup.is_file() or sha256_file(backup) != previous.get("originalSha"):
            raise PatchError("Original Host backup is missing or checksum-mismatched")
        if digest != previous.get("patchedSha"):
            raise PatchError("Host archive changed after patching; restoration aborted")
        after = backup.read_bytes()
        original_sha = previous["originalSha"]
    else:
        pristine = before
        if previous and digest == previous.get("patchedSha"):
            if (
                previous.get("payloadSha") == payload_sha
                and previous.get("applyFixes") == bool(apply_fixes)
                and previous.get("enableReporter", False) == bool(enable_reporter)
            ):
                record = dict(previous, state="installed", token=token, serverPid=server_pid)
                atomic_json(sidecar, record)
                atomic_json(status, record)
                return record
            if not backup.is_file() or sha256_file(backup) != previous.get("originalSha"):
                raise PatchError("Original Host backup could not be verified")
            pristine = backup.read_bytes()
            original_sha = previous["originalSha"]

        if mode == "host_dir":
            after = _patch_client_mod_zip_bytes(
                pristine, payload_dir, apply_fixes=apply_fixes, enable_reporter=enable_reporter
            )
        else:
            out_buf = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(pristine), "r") as zin:
                with zipfile.ZipFile(out_buf, "w", compression=zipfile.ZIP_DEFLATED) as zout:
                    for zi in zin.infolist():
                        raw_item = zin.read(zi.filename)
                        if zi.filename.replace("\\", "/").endswith("Resources/Client/beamng_earth.zip"):
                            raw_item = _patch_client_mod_zip_bytes(
                                raw_item, payload_dir, apply_fixes=apply_fixes, enable_reporter=enable_reporter
                            )
                        new_zi = zipfile.ZipInfo(zi.filename, date_time=zi.date_time)
                        new_zi.compress_type = zi.compress_type
                        new_zi.external_attr = zi.external_attr
                        zout.writestr(new_zi, raw_item)
            after = out_buf.getvalue()

        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            with backup.open("xb") as f:
                f.write(pristine)
        if sha256_file(backup) != original_sha:
            raise PatchError("Host backup checksum verification failed")

    _cleanup_stale_pending(mod_zip, previous)
    pending = mod_zip.with_name(f"{mod_zip.name}.{TAG}.{uuid.uuid4().hex}.pending")
    with pending.open("xb") as f:
        f.write(after)
    rep_sha = sha256_bytes(after)
    patched_sha = rep_sha if operation == "install" else previous["patchedSha"]
    record = {
        "version": VERSION,
        "mode": mode,
        "state": "pending",
        "operation": operation,
        "patchTier": TIER_FULL_SOLVER,
        "target": str(mod_zip),
        "hostTarget": str(target),
        "backup": str(backup),
        "pending": str(pending),
        "expectedSha": digest,
        "replacementSha": rep_sha,
        "originalSha": original_sha,
        "patchedSha": patched_sha,
        "payloadSha": payload_sha,
        "applyFixes": bool(apply_fixes),
        "enableReporter": bool(enable_reporter) if operation == "install" else False,
        "sidecar": str(sidecar),
        "status": str(status),
        "config": str(config),
        "token": token,
        "serverPid": server_pid,
        "message": "Host archive patch staged." if operation == "install" else "Host archive restore staged.",
    }
    atomic_json(sidecar, record)
    atomic_json(status, record)
    return record


def _pid_alive_check(pid):
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = k32.OpenProcess(0x1000, False, pid)
            if not handle:
                return ctypes.get_last_error() == 5
            try:
                code = ctypes.c_uint32(0)
                if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == 259
                return True
            finally:
                k32.CloseHandle(handle)
        except Exception:
            return False
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        try:
            txt = proc_stat.read_text(encoding="utf-8", errors="replace")
            rparen = txt.rfind(")")
            if rparen > 0 and txt[rparen + 2:rparen + 3] == "Z":
                return False
        except OSError:
            pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


@contextlib.contextmanager
def _acquire_patch_lock(resolved_target, timeout_s=60.0, stale_age_s=120.0):
    target_hash = sha256_bytes(str(resolved_target).encode("utf-8"))[:12]
    lock_path = Path(tempfile.gettempdir()) / f"bne_companion_patch_{target_hash}.lock"
    deadline = time.monotonic() + timeout_s
    acquired = False
    while time.monotonic() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, json.dumps({"pid": os.getpid(), "ts": time.time()}).encode("utf-8"))
            finally:
                os.close(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                doc = read_json(lock_path, {})
                ts = float(doc.get("ts") or 0.0)
                lpid = int(doc.get("pid") or 0)
                if (ts and (time.time() - ts) > stale_age_s) or (lpid > 0 and lpid != os.getpid() and not _pid_alive_check(lpid)):
                    lock_path.unlink(missing_ok=True)
                    continue
            except Exception:
                pass
            time.sleep(0.05)
    if not acquired:
        raise PatchError("Timed out waiting for sibling add-on patch lock.")
    try:
        yield lock_path
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def prepare(target, config, status, payload_dir=None, operation="install", token="", server_pid=0,
            apply_fixes=True, enable_reporter=False):
    resolved_target, mode = _resolve_target_path(target)
    if not apply_fixes and not enable_reporter and operation == "install":
        operation = "restore"
    with _acquire_patch_lock(resolved_target):
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
        if mode == "appimage":
            return _prepare_appimage(
                resolved_target, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter
            )
        if mode in ("host_dir", "host_zip"):
            return _prepare_host(
                resolved_target, mode, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter
            )
        return _prepare_source(
            resolved_target, config, status, payload_dir, operation, token, server_pid, apply_fixes, enable_reporter
        )


def apply_now(record):
    """Apply staged exe, appimage, host, or source changes immediately (offline CLI / unit tests)."""
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
        if sha256_file(target) != record["expectedSha"] or sha256_file(pending) != record["replacementSha"]:
            raise PatchError("Files changed after preparation; refusing to overwrite")
        orig_mode = stat.S_IMODE(target.stat().st_mode)
        try:
            if orig_mode:
                try:
                    os.chmod(pending, orig_mode)
                except OSError:
                    pass
            os.replace(pending, target)
            if orig_mode:
                try:
                    os.chmod(target, orig_mode)
                except OSError:
                    pass
        except PermissionError as exc:
            raise PatchError("BeamNG.Earth is currently running. Close BeamNG.Earth completely and try again.") from exc
        except OSError as exc:
            if getattr(exc, "winerror", None) in (5, 32):
                raise PatchError("BeamNG.Earth is currently running. Close BeamNG.Earth completely and try again.") from exc
            raise
        if record.get("mode") == "appimage" and record.get("operation") == "restore":
            p_appdir = record.get("patchedAppDir")
            if p_appdir and Path(p_appdir).is_dir():
                shutil.rmtree(p_appdir, ignore_errors=True)

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
    """Read-only inspection of a BeamNG.Earth executable, Linux AppImage, Host archive, or source directory.

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
                patched = _has_taf_marker(marker_file.read_bytes())
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

    if mode == "appimage":
        backup = resolved.with_name(f"{resolved.name}.{TAG}.original")
        sidecar = read_json(resolved.with_name(f"{resolved.name}.{TAG}.json"), {})
        info["hasBackup"] = bool(backup.is_file() and sidecar)
        wmeta = _parse_appimage_wrapper(resolved)
        if wmeta is not None:
            appdir_p = Path(wmeta.get("appdir") or sidecar.get("patchedAppDir") or "")
            if appdir_p.is_dir():
                sub = _inspect_extracted_appdir(appdir_p)
                info.update(sub)
                info["state"] = "installed" if (info["patched"] and info["hasBackup"]) else ("patched_no_backup" if info["patched"] else "unpatched")
                return info
        try:
            with tempfile.TemporaryDirectory() as tmp_d:
                subset = [
                    "usr/lib/BeamNG.Earth/BeamNG.Earth",
                    "usr/lib/BeamNG.Earth/_internal/VERSION",
                    "usr/lib/BeamNG.Earth/_internal/mod/beamng_earth",
                ]
                ext_dir, _off = _extract_appimage_to_dir(resolved, Path(tmp_d) / "sqfs", subset_paths=subset)
                sub = _inspect_extracted_appdir(ext_dir)
                info.update(sub)
                if info["compatible"]:
                    info["state"] = (
                        "installed"
                        if (info["patched"] and info["hasBackup"])
                        else ("patched_no_backup" if info["patched"] else "unpatched")
                    )
                return info
        except Exception as exc:
            info["reason"] = str(exc)
            info["state"] = "incompatible"
            return info

    if mode in ("host_dir", "host_zip"):
        try:
            if mode == "host_dir":
                mzip_bytes = (resolved / "Resources" / "Client" / "beamng_earth.zip").read_bytes()
                sidecar = read_json(resolved / f".{TAG}" / "receipt.json", {})
                has_b = bool(sidecar and (resolved / f".{TAG}" / "beamng_earth.zip.original").is_file())
            else:
                with zipfile.ZipFile(resolved, "r") as zf:
                    zname = [n for n in zf.namelist() if n.replace("\\", "/").endswith("Resources/Client/beamng_earth.zip")][0]
                    mzip_bytes = zf.read(zname)
                sidecar = read_json(resolved.with_name(f"{resolved.name}.{TAG}.json"), {})
                has_b = bool(sidecar and resolved.with_name(f"{resolved.name}.{TAG}.original").is_file())
            with zipfile.ZipFile(io.BytesIO(mzip_bytes), "r") as mzf:
                mnames = {n.replace("\\", "/"): n for n in mzf.namelist()}
                missing_mf = [
                    rel for rel in MOD_FILES
                    if rel.removeprefix("mod/beamng_earth/") not in mnames and rel not in mnames
                ]
                gps_k = mnames.get(MOD_MARKER_FILE.removeprefix("mod/beamng_earth/")) or mnames.get(MOD_MARKER_FILE)
                gps_b = mzf.read(gps_k) if gps_k else b""
            info["pythonVersion"] = f"{sys.version_info.major}.{sys.version_info.minor}"
            info["pythonMatches"] = True
            info["missingModFiles"] = missing_mf
            info["patched"] = _has_taf_marker(gps_b)
            info["hasBackup"] = has_b
            if missing_mf:
                info["state"] = "incompatible"
                info["reason"] = "Host client mod zip is missing expected BeamNG.Earth mod files."
                return info
            info["compatible"] = True
            info["patchTier"] = TIER_FULL_SOLVER
            info["state"] = "installed" if (info["patched"] and has_b) else ("patched_no_backup" if info["patched"] else "unpatched")
            return info
        except Exception as exc:
            info["reason"] = str(exc)
            info["state"] = "incompatible"
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

    missing_mod_files = [rel for rel in MOD_FILES if _carchive_key(entry_map, rel) not in entry_map]
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

    marker_entry = entry_map.get(_carchive_key(entry_map, MOD_MARKER_FILE))
    patched = False
    if marker_entry is not None:
        try:
            raw_m = data[start + marker_entry[1]:start + marker_entry[1] + marker_entry[2]]
            body_m = zlib.decompress(raw_m) if marker_entry[4] else raw_m
            patched = _has_taf_marker(body_m)
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
    found = []
    if os.name == "nt":
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

    proc_root = Path("/proc")
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                env_raw = (entry / "environ").read_bytes()
                for item in env_raw.split(b"\x00"):
                    if item.startswith(b"APPIMAGE="):
                        ap = Path(item[9:].decode("utf-8", "replace"))
                        if ap.is_file():
                            found.append(ap)
                exe_p = (entry / "exe").resolve()
                if exe_p.name.lower().startswith("beamng.earth") and "/tmp/.mount_" not in str(exe_p):
                    found.append(exe_p)
            except OSError:
                continue
    return found


def _is_candidate_exe(path):
    name = path.name.lower()
    if (
        f".{TAG.lower()}." in name
        or ".earthbridgerailfixes." in name
        or ".original" in name
        or ".pending" in name
        or ".swap." in name
        or name.endswith(".json")
        or name.endswith(".sha256")
    ):
        return False
    if not name.startswith("beamng.earth"):
        return False
    if name.endswith(".exe") or name.endswith(".appimage"):
        return path.is_file()
    if os.name != "nt" and path.is_file() and not path.suffix:
        return True
    return False


def discover_targets(extra_paths=None, include_system=True):
    """Discover BeamNG.Earth executables, AppImages, Host folders, and source checkouts."""
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
            elif (d / "Resources" / "Client" / "beamng_earth.zip").is_file():
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
        for folder in ("Desktop", "Downloads", "Documents", "Applications", ".local/bin"):
            _scan_dir(home / folder, recurse_earth_dirs=True)
        if os.name == "nt":
            local_app = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
            _scan_dir(local_app / "beamng-earth", recurse_earth_dirs=True)
            _scan_dir(local_app / "Programs", recurse_earth_dirs=True)
            for env_key in ("ProgramFiles", "ProgramFiles(x86)"):
                pf = os.environ.get(env_key)
                if pf:
                    _scan_dir(Path(pf), recurse_earth_dirs=True)
        else:
            cache_base = Path(os.environ.get("XDG_CACHE_HOME") or (home / ".cache"))
            _scan_dir(cache_base / "beamng-earth", recurse_earth_dirs=True)

    results = []
    for cand in candidates:
        info = inspect_target(cand)
        if info["mode"] in ("exe", "appimage", "source", "host_dir", "host_zip") and info["state"] != "invalid":
            results.append(info)
    results.sort(key=lambda x: (not x["compatible"], x["mode"] not in ("exe", "appimage"), x["target"].lower()))
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


def _linux_homes(base_home=None):
    """Return user home folders (~ first, plus parent homes holding .local/share/BeamNG inside containers)."""
    home = Path(base_home).resolve() if base_home is not None else Path.home().resolve()
    out = [home]
    marks = (
        Path(".local") / "share" / "BeamNG",
        Path(".local") / "share" / "BeamNG.drive",
        Path(".var") / "app" / "com.valvesoftware.Steam",
    )
    cur = home
    while cur.parent != cur:
        cur = cur.parent
        if any((cur / m).is_dir() for m in marks):
            out.append(cur)
    return out


def _beamng_log_mtime(path):
    try:
        return (Path(path) / "beamng.log").stat().st_mtime
    except OSError:
        return -1.0


def discover_user_dirs(include_system=True, extra_paths=None, linux_home=None, xdg_data_home=None, xdg_cache_home=None):
    """Discover native BeamNG.drive user folders on Windows and Linux in upstream priority order."""
    pinned = []
    for p in (extra_paths or ()):
        pinned.append(Path(p))

    native_auto = []
    is_linux_mode = linux_home is not None or sys.platform.startswith("linux")
    if include_system and os.name == "nt" and not is_linux_mode:
        local = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        app_json = read_json(local / "beamng-earth" / "app.json", {})
        told = app_json.get("gameUserDir")
        if isinstance(told, str) and told:
            tp = Path(told)
            if (tp / "mods").is_dir() or tp.is_dir():
                pinned.append(tp)
        ini_uf = _read_ini_kv(local / "BeamNG" / "BeamNG.drive.ini").get("userFolder")
        if ini_uf:
            ufp = Path(ini_uf)
            if (ufp / "current").is_dir():
                pinned.append(ufp / "current")
            elif ufp.is_dir():
                pinned.append(ufp)
        pinned.append(local / "BeamNG" / "BeamNG.drive" / "current")
        for legacy in sorted(glob.glob(str(local / "BeamNG.drive" / "[0-9]*")), reverse=True):
            pinned.append(Path(legacy))
    elif include_system and is_linux_mode:
        if linux_home is None:
            try:
                import apppaths  # type: ignore
                saved = apppaths.saved_user_dir()
                if isinstance(saved, str) and saved.strip():
                    pinned.append(Path(saved.strip()))
            except Exception:
                pass
        homes = _linux_homes(linux_home)
        cache_dir = Path(xdg_cache_home) if xdg_cache_home is not None else Path(
            os.environ.get("XDG_CACHE_HOME") or (homes[0] / ".cache")
        )
        app_json = read_json(cache_dir / "beamng-earth" / "app.json", {})
        told = app_json.get("gameUserDir")
        if isinstance(told, str) and told.strip():
            pinned.append(Path(told.strip()))

        raw_native = []
        primary_data = Path(xdg_data_home) if xdg_data_home is not None else Path(
            os.environ.get("XDG_DATA_HOME") or (homes[0] / ".local" / "share")
        )
        data_roots = [primary_data]
        for h in homes:
            for dr in (
                h / ".local" / "share",
                h / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share",
                h / ".var" / "app" / "com.valvesoftware.Steam" / "data",
                h / "snap" / "steam" / "common" / ".local" / "share",
            ):
                if dr not in data_roots:
                    data_roots.append(dr)

        for dr in data_roots:
            raw_native.append(dr / "BeamNG" / "BeamNG.drive" / "current")
            for ver_d in sorted(glob.glob(str(dr / "BeamNG" / "BeamNG.drive" / "[0-9]*")), reverse=True):
                raw_native.append(Path(ver_d))
            raw_native.append(dr / "BeamNG.drive" / "current")
            for leg_d in sorted(glob.glob(str(dr / "BeamNG.drive" / "[0-9]*")), reverse=True):
                raw_native.append(Path(leg_d))

        existing_native = [p for p in raw_native if p.is_dir()]
        if any(_beamng_log_mtime(p) >= 0 for p in existing_native):
            existing_native.sort(key=lambda p: -_beamng_log_mtime(p))
        native_auto.extend(existing_native)

    out, seen = [], set()
    for c in [*pinned, *native_auto]:
        try:
            rc = c.resolve()
        except OSError:
            continue
        if not rc.is_dir():
            continue
        key = str(rc).lower() if (os.name == "nt" and not is_linux_mode) else str(rc)
        if key not in seen:
            seen.add(key)
            out.append(rc)
    return out


def _atomic_write_bytes(path, raw_bytes):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_bytes(raw_bytes)
    os.replace(tmp, path)


def _sync_unpacked_user_mod_dir(
    mod_root,
    payload_root,
    state_root,
    *,
    operation="install",
    apply_fixes=True,
    enable_reporter=False,
    patch_tier=TIER_FULL_SOLVER,
):
    mod_root = Path(mod_root)
    if not mod_root.is_dir():
        return False
    norm_key = str(mod_root.resolve()).lower() if os.name == "nt" else str(mod_root.resolve())
    dir_id = sha256_bytes(norm_key.encode("utf-8"))[:12]
    bdir = Path(state_root) / "unpacked_mod_backups" / dir_id

    shared_rels = [
        (r.removeprefix("mod/beamng_earth/"), r)
        for r in (MOD_TRAFFICREPORT_LUA, *REPORTER_SHARED_MOD_FILES)
    ]

    if operation == "restore":
        for rel in MOD_FILES:
            short = rel.removeprefix("mod/beamng_earth/")
            dest = mod_root / short
            bfile = bdir / short
            meta = read_json(bdir / f"{short}.meta.json", {})
            if bfile.is_file():
                _atomic_write_bytes(dest, bfile.read_bytes())
            elif meta and meta.get("existed") is False and dest.is_file():
                dest.unlink(missing_ok=True)
            elif dest.is_file():
                cur_b = dest.read_bytes()
                cleaned = _strip_existing_taf_marker_lines(cur_b)
                if rel == MOD_SETTINGS_LUA:
                    cleaned = unpatch_settings_lua_bytes(cleaned)
                if cleaned != cur_b:
                    _atomic_write_bytes(dest, cleaned)

        for short, full_rel in shared_rels:
            dest = mod_root / short
            if not dest.is_file():
                continue
            if full_rel == MOD_TRAFFICREPORT_LUA:
                dest.unlink(missing_ok=True)
            elif full_rel == MOD_ACTIONS_JSON:
                cur_b = dest.read_bytes()
                bfile = bdir / short
                if b"beamngEarth_quickBridgeRailReport" in cur_b or not bfile.is_file():
                    _atomic_write_bytes(dest, unpatch_actions_json_bytes(cur_b))
                else:
                    _atomic_write_bytes(dest, bfile.read_bytes())
            elif full_rel == MOD_KEYMAP_JSON:
                cur_b = dest.read_bytes()
                bfile = bdir / short
                if b"beamngEarth_quickBridgeRailReport" in cur_b or not bfile.is_file():
                    _atomic_write_bytes(dest, unpatch_keymap_json_bytes(cur_b))
                else:
                    _atomic_write_bytes(dest, bfile.read_bytes())
            elif full_rel == MOD_SCRIPT_LUA:
                cur_b = dest.read_bytes()
                bfile = bdir / short
                if b"beamngEarth_bugreport" in cur_b or not bfile.is_file():
                    _atomic_write_bytes(dest, unpatch_modscript_bytes(cur_b))
                else:
                    _atomic_write_bytes(dest, bfile.read_bytes())

        if bdir.is_dir():
            shutil.rmtree(bdir, ignore_errors=True)
        return True

    if payload_root is None:
        return False

    bdir.mkdir(parents=True, exist_ok=True)
    target_mod_rels = (
        (MOD_MARKER_FILE,)
        if (not apply_fixes or patch_tier == TIER_CLIENT_AND_REPORTER)
        else MOD_FILES
    )
    for rel in target_mod_rels:
        short = rel.removeprefix("mod/beamng_earth/")
        src = payload_root / rel
        if not src.is_file():
            continue
        dest = mod_root / short
        bfile = bdir / short
        meta_file = bdir / f"{short}.meta.json"
        cur_bytes = dest.read_bytes() if dest.is_file() else None
        is_pristine_cur = bool(
            cur_bytes is not None
            and not _has_taf_marker(cur_bytes)
            and (rel != MOD_SETTINGS_LUA or b"function M.autodriveTune" not in cur_bytes)
        )
        if is_pristine_cur:
            _atomic_write_bytes(bfile, cur_bytes)
            atomic_json(meta_file, {"rel": short, "existed": True, "originalSha": sha256_bytes(cur_bytes)})
        elif not meta_file.is_file():
            atomic_json(meta_file, {"rel": short, "existed": dest.is_file(), "originalSha": ""})

        upstream_b = bfile.read_bytes() if bfile.is_file() else cur_bytes
        patched_b = _patched_mod_bytes(rel, src.read_bytes(), upstream_bytes=upstream_b, patch_tier=patch_tier)
        if cur_bytes != patched_b:
            _atomic_write_bytes(dest, patched_b)

    if enable_reporter and all((mod_root / r.removeprefix("mod/beamng_earth/")).is_file() for r in REPORTER_SHARED_MOD_FILES):
        for short, full_rel in shared_rels:
            dest = mod_root / short
            bfile = bdir / short
            meta_file = bdir / f"{short}.meta.json"
            if full_rel == MOD_TRAFFICREPORT_LUA:
                src = payload_root / full_rel
                if src.is_file():
                    if not meta_file.is_file():
                        atomic_json(meta_file, {"rel": short, "existed": False, "originalSha": ""})
                    _atomic_write_bytes(dest, src.read_bytes())
                continue
            cur_b = dest.read_bytes()
            if full_rel == MOD_ACTIONS_JSON:
                clean_b = unpatch_actions_json_bytes(cur_b)
                if not bfile.is_file():
                    _atomic_write_bytes(bfile, clean_b)
                    atomic_json(meta_file, {"rel": short, "existed": True, "originalSha": sha256_bytes(clean_b)})
                _atomic_write_bytes(dest, patch_actions_json_bytes(cur_b))
            elif full_rel == MOD_KEYMAP_JSON:
                clean_b = unpatch_keymap_json_bytes(cur_b)
                if not bfile.is_file():
                    _atomic_write_bytes(bfile, clean_b)
                    atomic_json(meta_file, {"rel": short, "existed": True, "originalSha": sha256_bytes(clean_b)})
                _atomic_write_bytes(dest, patch_keymap_json_bytes(cur_b))
            elif full_rel == MOD_SCRIPT_LUA:
                clean_b = unpatch_modscript_bytes(cur_b)
                if not bfile.is_file():
                    _atomic_write_bytes(bfile, clean_b)
                    atomic_json(meta_file, {"rel": short, "existed": True, "originalSha": sha256_bytes(clean_b)})
                _atomic_write_bytes(dest, patch_modscript_bytes(cur_b))
    elif not enable_reporter:
        tr_dest = mod_root / MOD_TRAFFICREPORT_LUA.removeprefix("mod/beamng_earth/")
        if tr_dest.is_file():
            tr_dest.unlink(missing_ok=True)
        for full_rel, unpatch_fn in (
            (MOD_ACTIONS_JSON, unpatch_actions_json_bytes),
            (MOD_KEYMAP_JSON, unpatch_keymap_json_bytes),
            (MOD_SCRIPT_LUA, unpatch_modscript_bytes),
        ):
            dest = mod_root / full_rel.removeprefix("mod/beamng_earth/")
            if dest.is_file():
                cur_b = dest.read_bytes()
                un_b = unpatch_fn(cur_b)
                if un_b != cur_b:
                    _atomic_write_bytes(dest, un_b)
    return True


def sync_discovered_user_mods(
    payload_dir,
    state_root,
    *,
    user_dirs=None,
    operation="install",
    apply_fixes=True,
    enable_reporter=False,
    patch_tier=TIER_FULL_SOLVER,
):
    """Sync or restore BOTH <user_dir>/mods/unpacked/beamng_earth/ and <user_dir>/mods/beamng_earth.zip."""
    payload_root = Path(payload_dir) if payload_dir else None
    state_root = Path(state_root)
    udirs = list(user_dirs) if user_dirs is not None else discover_user_dirs()
    synced = []
    for udir in udirs:
        udir = Path(udir)
        unpacked_root = udir / "mods" / "unpacked" / "beamng_earth"
        if unpacked_root.is_dir():
            if _sync_unpacked_user_mod_dir(
                unpacked_root,
                payload_root,
                state_root,
                operation=operation,
                apply_fixes=apply_fixes,
                enable_reporter=enable_reporter,
                patch_tier=patch_tier,
            ):
                synced.append(str(unpacked_root))

        mod_zip = udir / "mods" / "beamng_earth.zip"
        if not mod_zip.is_file():
            continue
        dir_id = sha256_bytes(str(mod_zip.resolve()).encode("utf-8"))[:12]
        bdir = state_root / "zip_mod_backups" / dir_id
        bfile = bdir / "beamng_earth.zip.original"
        meta_file = bdir / "meta.json"
        if operation == "restore":
            meta = read_json(meta_file, {})
            if bfile.is_file() and meta.get("originalSha") and sha256_file(bfile) == meta["originalSha"]:
                tmp_z = mod_zip.with_name(f"{mod_zip.name}.{uuid.uuid4().hex}.tmp")
                shutil.copy2(bfile, tmp_z)
                os.replace(tmp_z, mod_zip)
                shutil.rmtree(bdir, ignore_errors=True)
                synced.append(str(mod_zip))
            continue

        if payload_root is None:
            continue
        cur_bytes = mod_zip.read_bytes()
        try:
            with zipfile.ZipFile(io.BytesIO(cur_bytes), "r") as zchk:
                names = {n.replace("\\", "/"): n for n in zchk.namelist()}
                gk = names.get(MOD_MARKER_FILE.removeprefix("mod/beamng_earth/")) or names.get(MOD_MARKER_FILE)
                already_ours = bool(gk and _has_taf_marker(zchk.read(gk)))
        except Exception:
            continue

        if not already_ours:
            bdir.mkdir(parents=True, exist_ok=True)
            bfile.write_bytes(cur_bytes)
            atomic_json(meta_file, {"originalSha": sha256_bytes(cur_bytes), "modZip": str(mod_zip)})
            base_bytes = cur_bytes
        elif bfile.is_file():
            base_bytes = bfile.read_bytes()
        else:
            base_bytes = cur_bytes

        patched_zip = _patch_client_mod_zip_bytes(
            base_bytes,
            payload_root,
            apply_fixes=apply_fixes,
            enable_reporter=enable_reporter,
            patch_tier=patch_tier,
        )
        tmp_z = mod_zip.with_name(f"{mod_zip.name}.{uuid.uuid4().hex}.tmp")
        tmp_z.write_bytes(patched_zip)
        os.replace(tmp_z, mod_zip)
        synced.append(str(mod_zip))
    return synced


def _default_state_paths(target):
    if os.name == "nt":
        base_root = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
    else:
        base_root = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    base = base_root / STATE_FOLDER_NAME
    digest = hashlib.sha256(str(Path(target).resolve()).encode("utf-8")).hexdigest()[:12]
    return base / f"control-{digest}.json", base / f"status-{digest}.json"


def install_target(target, payload_dir, config_path=None, status_path=None,
                   apply_fixes=True, enable_reporter=False, apply_immediately=True):
    """Verify compatibility, stage, and optionally apply patches to a target."""
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

    if mode == "host_dir":
        state_dir = resolved / f".{TAG}"
        sidecar = read_json(state_dir / "receipt.json", {})
        mod_zip = resolved / "Resources" / "Client" / "beamng_earth.zip"
        if sidecar.get("originalSha") and sha256_file(mod_zip) != sidecar["originalSha"]:
            raise PatchError("Cannot delete backup until original Host archive is restored")
        if state_dir.is_dir():
            shutil.rmtree(state_dir)
            removed = True
        return removed

    sidecar_path = resolved.with_name(resolved.name + f".{TAG}.json")
    backup_path = resolved.with_name(resolved.name + f".{TAG}.original")
    sidecar = read_json(sidecar_path, {})
    digest = sha256_file(resolved)
    stacked_restored = (
        sidecar.get("state") == "restored"
        and digest == sidecar.get("replacementSha")
        and not inspect_target(resolved).get("patched")
    )
    if sidecar.get("originalSha") and digest != sidecar.get("originalSha") and not stacked_restored:
        raise PatchError("Cannot delete backup while patched target is still active")
    if not sidecar and inspect_target(resolved).get("patched"):
        raise PatchError("Cannot delete backup while target is patched")
    _cleanup_stale_pending(resolved, sidecar)
    p_appdir = sidecar.get("patchedAppDir")
    if p_appdir and Path(p_appdir).is_dir():
        shutil.rmtree(p_appdir, ignore_errors=True)
    for p in (backup_path, sidecar_path):
        if p.is_file():
            p.unlink()
            removed = True
    return removed


def restore_target(target, payload_dir=None, config_path=None, status_path=None,
                   apply_immediately=True, cleanup=False):
    """Stage and optionally apply restoration of the original target."""
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
