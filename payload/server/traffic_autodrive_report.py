"""Asynchronous 1-hotkey silent bug report uploader for BeamNG.Earth - Traffic and AutoDrive fixes."""
import base64
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import struct
import threading
import time
import urllib.error
import urllib.request
import zlib

log = logging.getLogger("earthd.traffic_autodrive_report")

DEFAULT_ENDPOINT = "https://beamng-earth-bridge-rail-fixes.yourlocalpcgamer.workers.dev/report"
FIXED_REPORTS_URL = (
    "https://raw.githubusercontent.com/CodeWMLandfill/"
    "BeamNG-Earth-Traffic-and-AutoDrive-Fixes/reports/proofs/fixed_reports.json"
)
ALLOWED_ISSUE_URL_PREFIX = "https://github.com/CodeWMLandfill/BeamNG-Earth-Traffic-and-AutoDrive-Fixes/issues/"
MAX_FIXED_REPORTS_BYTES = 65536
LATEST_RELEASE_API_URL = (
    "https://api.github.com/repos/CodeWMLandfill/"
    "BeamNG-Earth-Traffic-and-AutoDrive-Fixes/releases/latest"
)
ALLOWED_RELEASE_URL_PREFIX = (
    "https://github.com/CodeWMLandfill/"
    "BeamNG-Earth-Traffic-and-AutoDrive-Fixes/releases/"
)
MAX_RELEASE_JSON_BYTES = 65536
ADDON_ID = "beamng-earth-autodrive-fixes"
ADDON_OPTION_IDS = (
    "beamng-earth-autodrive-fixes",
    "beamng-earth-traffic-and-autod",
    "beamng-earth-traffic-and-autodrive-fixes",
)
ADDON_VERSION = "0.1.4"
EARTH_SUBDIRS = ("beamngearth", "beamngEarth")
REPORTS_SUBDIR = "traffic_autodrive_reports"
SHOT_FILENAMES = ("screenshot.jpg", "screenshot.jpeg", "screenshot.png")
MAX_SHOT_BYTES = int(1.45 * 1024 * 1024)
ALLOWED_KINDS = ("autodrive", "traffic", "overtake", "routing", "road", "other")
BUNDLED_FIXED_REPORTS = {}
_VERSION_RE = re.compile(r"^v?([0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9]+)*)$")
_FIXED_CHECK_LOCK = threading.Lock()
_FIXED_CHECKED_KEYS = set()
ALLOWED_KEYS = frozenset({
    "addonId", "lat", "lon", "kind", "summary", "note", "earthVersion", "addonVersion",
    "headingDeg", "altM", "speedMs", "anchorId", "camera", "nearest", "screenshot",
    "drivingTelemetry", "loading_incomplete", "loading_reason", "experimentalFlags",
})
_CORE_FALLBACK_KEYS = frozenset({
    "lat", "lon", "kind", "summary", "note", "earthVersion", "addonVersion",
    "headingDeg", "altM", "speedMs", "anchorId", "camera", "nearest", "screenshot",
})

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_SAFE_CHUNKS = frozenset({
    b"IHDR", b"PLTE", b"IDAT", b"IEND",
    b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"bKGD",
})
_JPEG_STANDALONE_MARKERS = frozenset({0x01, 0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8})

_PATH_PATTERNS = (
    re.compile(r"\bfile:///?[^\s\"'`<>()[\]{},;]+(?<![.!?:])", re.IGNORECASE),
    re.compile(r"(?:^|(?<=[\s(\"'`=,[\]{}]))\\\\[A-Za-z0-9_.$ -]+\\[A-Za-z0-9_.$ -]+(?:\\[^\s\\/\"'`<>|*?,;)\]}]+)*\\?(?<![.!?:])"),
    re.compile(r"%(?:USERPROFILE|APPDATA|LOCALAPPDATA|TEMP|TMP|HOMEDRIVE|HOMEPATH)%(?:\\+|/)[^\s\"'`<>|*?,;)\]}]+(?<![.!?:])", re.IGNORECASE),
    re.compile(r"(?:^|(?<=[\s(\"'`=,[\]{}]))[A-Za-z]:(?:\\+|/)(?:(?:[^\s\\/\"'`<>|*?,;)\]}]+(?: [^\s\\/\"'`<>|*?,;)\]}]+)*(?:\\+|/))*(?:[^\s\\/\"'`<>|*?,;)\]}]+(?:\\+|/)?(?<![.!?:]))?)?"),
    re.compile(r"(?:^|(?<=[\s(\"'`=,[\]{}]))/(?:Users|home|root|private/var|var/folders|mnt/[a-zA-Z]|tmp|etc|opt|proc)(?:/[^\s\\/\"'`<>|*?,;)\]}]+)+/?(?<![.!?:])"),
    re.compile(r"(?:^|(?<=[\s(\"'`=,[\]{}]))~(?:\\+|/)[^\s\"'`<>|*?,;)\]}]+(?<![.!?:])"),
)


def redact_paths(text):
    """Strip Windows/Unix/UNC/env-var/tilde/file:// paths from report text before upload."""
    if not isinstance(text, str):
        return text
    out = text
    for pat in _PATH_PATTERNS:
        out = pat.sub("[redacted-path]", out)
    return out


def strip_jpeg_metadata(data):
    """Strip JPEG APP0..APP15 and COM metadata markers while preserving image data."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 4 or data[:2] != b"\xff\xd8":
        raise ValueError("Not a JPEG byte stream")

    n = len(data)
    out = bytearray(b"\xff\xd8")
    pos = 2
    saw_eoi = False

    while pos < n:
        if data[pos] != 0xFF:
            raise ValueError("Expected JPEG marker")
        while pos < n and data[pos] == 0xFF:
            pos += 1
        if pos >= n:
            raise ValueError("Truncated JPEG marker")

        marker = data[pos]
        pos += 1

        if marker == 0x00:
            raise ValueError("Invalid JPEG marker 0xFF00")
        if marker == 0xD9:  # EOI
            out.extend(b"\xff\xd9")
            saw_eoi = True
            break
        if marker in _JPEG_STANDALONE_MARKERS:
            out.append(0xFF)
            out.append(marker)
            continue

        if pos + 2 > n:
            raise ValueError("Truncated JPEG segment length")
        seg_len = struct.unpack_from(">H", data, pos)[0]
        if seg_len < 2 or pos + seg_len > n:
            if (
                marker == 0xE0
                and seg_len == 0
                and data[-2:] == b"\xff\xd9"
                and all(b == 0 for b in data[4:-2])
            ):
                return b"\xff\xd8\xff\xd9"
            raise ValueError("Invalid JPEG segment length")

        is_meta = (0xE0 <= marker <= 0xEF) or (marker == 0xFE)
        if not is_meta:
            out.append(0xFF)
            out.append(marker)
            out.extend(data[pos : pos + seg_len])
        pos += seg_len

        if marker == 0xDA:  # SOS
            scan_start = pos
            while pos < n:
                idx = data.find(b"\xff", pos)
                if idx == -1:
                    raise ValueError("Truncated JPEG scan data without EOI")
                if idx + 1 >= n:
                    raise ValueError("Truncated JPEG scan marker")
                nxt = data[idx + 1]
                if nxt == 0x00 or (0xD0 <= nxt <= 0xD7):
                    pos = idx + 2
                    continue
                out.extend(data[scan_start:idx])
                pos = idx
                break
            else:
                raise ValueError("Truncated JPEG scan data without EOI")

    if not saw_eoi:
        raise ValueError("JPEG missing EOI marker")
    return bytes(out)


def strip_png_metadata(data):
    """Strip PNG ancillary metadata chunks (tEXt/iTXt/zTXt/eXIf/tIME/pHYs/iCCP) and recompute CRC32."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 33 or data[:8] != _PNG_SIG:
        raise ValueError("Not a PNG byte stream")

    n = len(data)
    pos = 8
    out = bytearray(_PNG_SIG)
    saw_ihdr = False
    saw_idat = False
    saw_iend = False

    while pos + 12 <= n:
        length, ctype = struct.unpack_from(">I4s", data, pos)
        if length > n - pos - 12:
            raise ValueError("Truncated PNG chunk")
        chunk_data = data[pos + 8 : pos + 8 + length]
        pos += 12 + length

        if not saw_ihdr:
            if ctype != b"IHDR" or length != 13:
                raise ValueError("PNG must start with valid IHDR chunk")
            saw_ihdr = True

        if ctype == b"IDAT":
            saw_idat = True

        if ctype in _PNG_SAFE_CHUNKS:
            crc = zlib.crc32(ctype + chunk_data) & 0xFFFFFFFF
            out.extend(struct.pack(">I4s", length, ctype))
            out.extend(chunk_data)
            out.extend(struct.pack(">I", crc))

        if ctype == b"IEND":
            if length != 0:
                raise ValueError("PNG IEND chunk must be empty")
            saw_iend = True
            break

    if not (saw_ihdr and saw_idat and saw_iend):
        raise ValueError("Incomplete PNG chunk structure")
    return bytes(out)


def _sanitize_value(val, depth=0):
    if depth > 4:
        return None
    if val is None or isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return round(val, 4) if math.isfinite(val) else None
    if isinstance(val, str):
        return redact_paths(val)[:200]
    if isinstance(val, (list, tuple)):
        return [_sanitize_value(v, depth + 1) for v in val[:24]]
    if isinstance(val, dict):
        clean = {}
        for k, v in list(val.items())[:24]:
            if isinstance(k, str) and k not in ("__proto__", "constructor", "prototype"):
                clean_key = redact_paths(k)[:64]
                if clean_key and clean_key not in ("__proto__", "constructor", "prototype"):
                    clean[clean_key] = _sanitize_value(v, depth + 1)
        return clean
    return None


def _compact_driving_summary(dt):
    """Build a short ASCII summary tag from drivingTelemetry for Worker fallback notes."""
    if not isinstance(dt, dict):
        return ""
    parts = []
    ad = dt.get("autodrive")
    if isinstance(ad, dict):
        parts.append(f"ad={bool(ad.get('active'))}")
        if isinstance(ad.get("mode"), str):
            parts.append(f"mode={ad['mode'][:12]}")
        if isinstance(ad.get("nilWps"), int):
            parts.append(f"nilWps={ad['nilWps']}")
    re_obj = dt.get("roadEdge")
    if isinstance(re_obj, dict):
        if isinstance(re_obj.get("lanes"), str):
            parts.append(f"lanes={re_obj['lanes'][:12]}")
        if isinstance(re_obj.get("halfM"), (int, float)):
            parts.append(f"halfM={re_obj['halfM']}")
        if isinstance(re_obj.get("latOffM"), (int, float)):
            parts.append(f"latOffM={re_obj['latOffM']}")
    tune = dt.get("tune")
    if isinstance(tune, dict) and isinstance(tune.get("speedMph"), (int, float)):
        parts.append(f"tuneMph={tune['speedMph']}")
    nt = dt.get("nearbyTraffic")
    if isinstance(nt, list):
        parts.append(f"trafficN={len(nt)}")
    if not parts:
        return ""
    return "[driving: " + " ".join(parts)[:180] + "]"


def _read_addon_options():
    try:
        import apppaths
        addons_root = Path(apppaths.data_dir()) / "addons"
        for aid in ADDON_OPTION_IDS:
            opts_path = addons_root / aid / "options.json"
            if opts_path.is_file():
                data = json.loads(opts_path.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return {}


def read_experimental_flags(user_dir=None):
    """Return sanitized list of active BeamNG.Earth experimental switch IDs via guarded import."""
    raw_flags = []
    try:
        import experimental
        settings_fn = getattr(experimental, "read_settings", None)
        chosen_fn = getattr(experimental, "chosen", None)
        settings = settings_fn(str(user_dir) if user_dir else "") if callable(settings_fn) else {}
        if not isinstance(settings, dict):
            settings = {}
        raw_flags = list(chosen_fn(settings)) if callable(chosen_fn) else []
    except Exception:
        raw_flags = []
    if not raw_flags and user_dir:
        udir = Path(user_dir)
        for s_rel in ("settings/beamngearth.json", "settings/beamngEarth.json"):
            sf = udir / s_rel
            if sf.is_file():
                try:
                    doc = json.loads(sf.read_text(encoding="utf-8-sig"))
                    if isinstance(doc, dict):
                        raw_flags = [k for k in sorted(doc) if (k == "roadSigns" or k.startswith("exp")) and doc.get(k) is True]
                        if raw_flags:
                            break
                except Exception:
                    pass
    out = []
    for item in raw_flags[:24]:
        if isinstance(item, str) and item.strip():
            cleaned = redact_paths(item.strip())[:64]
            if cleaned and cleaned not in out:
                out.append(cleaned)
    return out


def _prepare_screenshot_data_url(shot_path):
    """Read screenshot JPEG/PNG, strip all metadata, and shrink via Pillow if > MAX_SHOT_BYTES."""
    if not shot_path or not shot_path.is_file():
        return None
    raw = shot_path.read_bytes()
    if not raw:
        return None

    is_jpeg = raw[:3] == b"\xff\xd8\xff"
    is_png = raw[:8] == _PNG_SIG
    if is_jpeg:
        try:
            clean = strip_jpeg_metadata(raw)
            if len(clean) <= MAX_SHOT_BYTES:
                return f"data:image/jpeg;base64,{base64.b64encode(clean).decode('ascii')}"
        except ValueError as err:
            log.debug("traffic_autodrive_report: JPEG strip fallback (%s)", err)
    elif is_png:
        try:
            clean = strip_png_metadata(raw)
            if len(clean) <= MAX_SHOT_BYTES:
                return f"data:image/png;base64,{base64.b64encode(clean).decode('ascii')}"
        except ValueError as err:
            log.debug("traffic_autodrive_report: PNG strip fallback (%s)", err)

    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as im:
            im = im.convert("RGB")
            for scale in (1.0, 0.75, 0.5):
                w, h = max(320, int(im.width * scale)), max(180, int(im.height * scale))
                resized = im if scale == 1.0 else im.resize((w, h), Image.Resampling.LANCZOS)
                for q in (82, 68, 52):
                    buf = io.BytesIO()
                    resized.save(buf, format="JPEG", quality=q, optimize=True)
                    jpg = strip_jpeg_metadata(buf.getvalue())
                    if len(jpg) <= MAX_SHOT_BYTES:
                        return f"data:image/jpeg;base64,{base64.b64encode(jpg).decode('ascii')}"
    except Exception as err:
        log.debug("traffic_autodrive_report: screenshot encode skipped (%s)", err)
    return None


def build_payload(msg, shot_path=None, options=None, user_dir=None):
    """Build a Worker-compatible sanitized report dict from incoming Lua message."""
    raw = dict(msg.get("payload") or {})
    opts = options if options is not None else _read_addon_options()
    kind = str(raw.get("kind") or opts.get("reportCategory") or "autodrive").lower()
    if kind not in ALLOWED_KINDS:
        kind = "autodrive"

    lat, lon = float(raw["lat"]), float(raw["lon"])
    if not (math.isfinite(lat) and -90.0 <= lat <= 90.0 and math.isfinite(lon) and -180.0 <= lon <= 180.0):
        raise ValueError("Invalid coordinates")
    payload = {
        "addonId": ADDON_ID,
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "kind": kind,
        "summary": redact_paths(str(raw.get("summary") or f"In-game {kind} report @ {lat:.5f}, {lon:.5f}"))[:200],
        "addonVersion": ADDON_VERSION,
    }
    for key in ("note", "earthVersion", "anchorId", "loading_reason"):
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            max_len = 2000 if key == "note" else (64 if key == "earthVersion" else 128)
            cleaned = redact_paths(val.strip())[:max_len]
            if cleaned:
                payload[key] = cleaned
    if isinstance(raw.get("loading_incomplete"), bool):
        payload["loading_incomplete"] = raw["loading_incomplete"]
    for key, lo, hi in (("headingDeg", -360.0, 360.0), ("altM", -1000.0, 100000.0), ("speedMs", -1000.0, 1000.0)):
        val = raw.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(float(val)):
            payload[key] = max(lo, min(hi, float(val)))
    for key in ("camera", "nearest", "drivingTelemetry"):
        val = raw.get(key)
        if isinstance(val, dict):
            cleaned = _sanitize_value(val)
            if isinstance(cleaned, dict) and cleaned:
                payload[key] = cleaned
    exp_flags = []
    raw_exp = raw.get("experimentalFlags")
    if isinstance(raw_exp, (list, tuple)):
        for item in raw_exp[:24]:
            if isinstance(item, str) and item.strip():
                cleaned_flag = redact_paths(item.strip())[:64]
                if cleaned_flag and cleaned_flag not in exp_flags:
                    exp_flags.append(cleaned_flag)
    for item in read_experimental_flags(user_dir):
        if len(exp_flags) >= 24:
            break
        if item not in exp_flags:
            exp_flags.append(item)
    nearest_map = dict(payload.get("nearest") or {})
    if exp_flags:
        payload["experimentalFlags"] = exp_flags
        nearest_map["experimentalFlags"] = list(exp_flags)
    if "drivingTelemetry" in payload and "drivingTelemetry" not in nearest_map:
        nearest_map["drivingTelemetry"] = payload["drivingTelemetry"]
    nearest_map["addonId"] = ADDON_ID
    cleaned_nearest = _sanitize_value(nearest_map)
    if isinstance(cleaned_nearest, dict) and cleaned_nearest:
        payload["nearest"] = cleaned_nearest
    if shot_path is not None:
        shot_url = _prepare_screenshot_data_url(Path(shot_path))
        if shot_url:
            payload["screenshot"] = shot_url
    return {k: v for k, v in payload.items() if k in ALLOWED_KEYS}


def _atomic_write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _safe_within_base(base, target):
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _match_child_exact_or_ci(parent_dir, wanted_name, prefer_names=()):
    try:
        by_name = {p.name: p for p in parent_dir.iterdir()}
    except OSError:
        return []
    matches = []
    seen = set()
    for pref in prefer_names:
        cand = by_name.get(pref)
        if cand is not None and cand.name not in seen:
            matches.append(cand)
            seen.add(cand.name)
    exact = by_name.get(wanted_name)
    if exact is not None and exact.name not in seen:
        matches.append(exact)
        seen.add(exact.name)
    low = wanted_name.lower()
    for name in sorted(by_name):
        if name.lower() == low and name not in seen:
            matches.append(by_name[name])
            seen.add(name)
    return matches


def _walk_existing_rel(base, parts):
    cur_list = [base]
    for idx, part in enumerate(parts):
        prefer = EARTH_SUBDIRS if idx == 0 and part.lower() == "beamngearth" else ()
        nxt_list = []
        for cur in cur_list:
            for child in _match_child_exact_or_ci(cur, part, prefer_names=prefer):
                if child.is_dir():
                    nxt_list.append(child)
        if not nxt_list:
            return []
        cur_list = nxt_list
    return [c for c in cur_list if _safe_within_base(base, c)]


def _resolve_report_dir(user_dir, rel):
    if not user_dir or not isinstance(rel, str):
        return None
    rel_clean = rel.replace("\\", "/").lstrip("/").rstrip("/")
    parts = [p for p in rel_clean.split("/") if p]
    if not parts or ".." in parts or parts[0].lower() != "beamngearth":
        return None
    base = Path(user_dir)
    if not base.exists():
        base = base.resolve()
    existing = _walk_existing_rel(base, parts)
    if existing:
        for cand in existing:
            if any((cand / fn).is_file() for fn in (*SHOT_FILENAMES, "report.json")):
                return cand
        return existing[0]
    earth_dirs = _match_child_exact_or_ci(base, parts[0], prefer_names=EARTH_SUBDIRS)
    root_sub = earth_dirs[0] if earth_dirs else (base / parts[0])
    target = root_sub.joinpath(*parts[1:])
    if not _safe_within_base(base, target):
        return None
    return target


def _find_reports_roots(user_dir):
    if not user_dir:
        return []
    base = Path(user_dir)
    if not base.is_dir():
        return []
    found = _walk_existing_rel(base, ["beamngEarth", REPORTS_SUBDIR])
    if found:
        return found
    earth_dirs = _match_child_exact_or_ci(base, "beamngEarth", prefer_names=EARTH_SUBDIRS)
    if earth_dirs:
        cand = earth_dirs[0] / REPORTS_SUBDIR
        if _safe_within_base(base, cand):
            return [cand]
    return []


def _select_report_context(user_dir, user_path, rel):
    dirs = []
    for raw_d in (user_path, user_dir):
        if raw_d:
            p = Path(str(raw_d).strip())
            if p not in dirs:
                dirs.append(p)
    best_u, best_r = (Path(user_dir) if user_dir else (dirs[0] if dirs else None)), None
    for cand_u in dirs:
        rdir = _resolve_report_dir(cand_u, rel)
        if rdir is not None:
            if best_r is None:
                best_u, best_r = cand_u, rdir
            if rdir.is_dir():
                return cand_u, rdir
    return best_u, best_r


def upload_report(user_dir, msg, endpoint=DEFAULT_ENDPOINT, timeout_s=20.0, wait_shot_s=3.0, user_path=None):
    """Build, upload, and record receipt for one captured report."""
    opts = _read_addon_options()
    eff_user_dir, report_dir = _select_report_context(user_dir, user_path, msg.get("rel"))
    if opts.get("enableOneHotkeyReport") is False:
        res = {"ok": False, "error": "Disabled in add-on options"}
        if report_dir is not None:
            _atomic_write_json(report_dir / "uploaded.json", res)
        return res

    shot_path = None
    if report_dir is not None:
        deadline = time.monotonic() + max(0.0, float(wait_shot_s))
        while time.monotonic() < deadline:
            # Re-resolve once in case BeamNG created lowercase beamngearth/ slightly after net.send
            if not report_dir.is_dir():
                _, refreshed = _select_report_context(user_dir, user_path, msg.get("rel"))
                if refreshed is not None:
                    report_dir = refreshed
            for fn in SHOT_FILENAMES:
                candidate = report_dir / fn
                if candidate.is_file() and candidate.stat().st_size > 128:
                    shot_path = candidate
                    break
            if shot_path is not None:
                break
            time.sleep(0.15)
        if shot_path is None:
            for fn in SHOT_FILENAMES:
                candidate = report_dir / fn
                if candidate.is_file() and candidate.stat().st_size > 0:
                    shot_path = candidate
                    break

    payload = build_payload(msg, shot_path=shot_path, options=opts, user_dir=eff_user_dir)

    def _post_json(body_dict):
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(body_dict, allow_nan=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"BeamNG-Earth-Autodrive-Fixes/{ADDON_VERSION}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))

    cur_payload = dict(payload)
    result = None
    for _attempt in range(3):
        try:
            result = _post_json(cur_payload)
            break
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", errors="replace")[:300]
            extra_keys = [k for k in cur_payload if k not in _CORE_FALLBACK_KEYS]
            unsupported_field = bool(extra_keys) and (
                "Unsupported top-level field" in body or "field" in body.lower() or "schema" in body.lower()
            )
            unsupported_kind = cur_payload.get("kind") in ("autodrive", "traffic", "overtake", "routing") and (
                "kind" in body.lower() or "category" in body.lower()
            )
            if err.code == 400 and _attempt < 2 and (unsupported_field or unsupported_kind):
                nxt = {
                    k: v
                    for k, v in cur_payload.items()
                    if not (unsupported_field and k not in _CORE_FALLBACK_KEYS)
                }
                nearest = dict(nxt.get("nearest") or {})
                tags = [f"[addon: {ADDON_ID}]"]
                if unsupported_field:
                    if "drivingTelemetry" in cur_payload:
                        nearest["drivingTelemetry"] = cur_payload["drivingTelemetry"]
                        d_tag = _compact_driving_summary(cur_payload["drivingTelemetry"])
                        if d_tag:
                            tags.append(d_tag)
                    if "loading_incomplete" in cur_payload or "loading_reason" in cur_payload:
                        loading_obj = dict(nearest.get("loading") or {}) if isinstance(nearest.get("loading"), dict) else {}
                        if "loading_incomplete" in cur_payload:
                            loading_obj["loading_incomplete"] = cur_payload["loading_incomplete"]
                        if "loading_reason" in cur_payload:
                            loading_obj["loading_reason"] = cur_payload["loading_reason"]
                        nearest["loading"] = loading_obj
                        tags.append(f"[loading_incomplete: {cur_payload.get('loading_reason') or 'true'}]")
                    if "experimentalFlags" in cur_payload:
                        nearest["experimentalFlags"] = list(cur_payload["experimentalFlags"])
                        tags.append(f"[experimentalFlags: {', '.join(cur_payload['experimentalFlags'])}]")
                if unsupported_kind:
                    orig_kind = cur_payload["kind"]
                    nxt["kind"] = "road"
                    nearest["originalKind"] = orig_kind
                    tags.append(f"[category: {orig_kind}]")
                if nearest:
                    nxt["nearest"] = _sanitize_value(nearest)
                note = str(nxt.get("note") or "")
                for tag in tags:
                    if tag not in note:
                        note = (f"{note} {tag}".strip() if note else tag)[:2000]
                if note:
                    nxt["note"] = note
                cur_payload = nxt
                continue
            result = {"ok": False, "status": err.code, "error": redact_paths(body or str(err))}
            break
        except Exception as err:
            result = {"ok": False, "error": redact_paths(str(err))}
            break

    if report_dir is not None:
        try:
            _atomic_write_json(report_dir / "uploaded.json", result)
        except OSError as err:
            log.warning("traffic_autodrive_report: failed writing uploaded.json: %s", err)
    if result.get("ok"):
        log.info("traffic_autodrive_report: sent #%s (%s)", result.get("issueNumber"), payload.get("kind"))
    else:
        log.warning("traffic_autodrive_report: upload failed (%s)", result.get("error"))
    return result


def _parse_positive_int(val):
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val if 1 <= val <= 999999 else None
    if isinstance(val, str) and val.strip().isdigit():
        num = int(val.strip())
        return num if 1 <= num <= 999999 else None
    return None


def _normalize_fixed_entry(num, raw_entry):
    entry = raw_entry if isinstance(raw_entry, dict) else {}
    raw_ver = str(entry.get("fixedIn") or "0.1.0").strip()
    m = _VERSION_RE.match(raw_ver)
    fixed_in = m.group(1) if m else "0.1.0"
    expected_url = f"{ALLOWED_ISSUE_URL_PREFIX}{num}"
    raw_url = entry.get("issueUrl")
    issue_url = raw_url if (isinstance(raw_url, str) and raw_url == expected_url) else expected_url
    kind = str(entry.get("kind") or "autodrive").lower()
    if kind not in ALLOWED_KINDS:
        kind = "autodrive"
    return {
        "fixedIn": fixed_in,
        "issueUrl": issue_url,
        "kind": kind,
    }


def _normalize_fixed_reports_map(raw_doc):
    merged = {k: dict(v) for k, v in BUNDLED_FIXED_REPORTS.items()}
    if isinstance(raw_doc, dict):
        inner = raw_doc.get("issues") if isinstance(raw_doc.get("issues"), dict) else raw_doc
        if isinstance(raw_doc.get("reports"), list):
            for item in raw_doc["reports"]:
                if isinstance(item, dict):
                    num = _parse_positive_int(item.get("issueNumber"))
                    if num is not None:
                        merged[str(num)] = _normalize_fixed_entry(num, item)
        else:
            for k, v in inner.items():
                num = _parse_positive_int(k)
                if num is not None and isinstance(v, dict):
                    merged[str(num)] = _normalize_fixed_entry(num, v)
    elif isinstance(raw_doc, list):
        for item in raw_doc:
            if isinstance(item, dict):
                num = _parse_positive_int(item.get("issueNumber"))
                if num is not None:
                    merged[str(num)] = _normalize_fixed_entry(num, item)
    return merged


def fetch_fixed_reports(fixed_url=FIXED_REPORTS_URL, timeout_s=4.0):
    """Fetch the static fixed_reports.json manifest once, falling back cleanly when offline."""
    if not fixed_url:
        return _normalize_fixed_reports_map(None)
    try:
        req = urllib.request.Request(
            fixed_url,
            headers={
                "Accept": "application/json",
                "User-Agent": f"BeamNG-Earth-Autodrive-Fixes/{ADDON_VERSION}",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read(MAX_FIXED_REPORTS_BYTES + 1)
        if len(raw) > MAX_FIXED_REPORTS_BYTES:
            raise ValueError("fixed_reports manifest exceeds size cap")
        doc = json.loads(raw.decode("utf-8"))
        return _normalize_fixed_reports_map(doc)
    except Exception as err:
        log.debug("traffic_autodrive_report: using bundled fixed_reports fallback (%s)", err)
        return _normalize_fixed_reports_map(None)


def check_fixed_reports(user_dir, fixed_url=FIXED_REPORTS_URL, timeout_s=4.0, options=None):
    """Match local uploaded reports against fixed_reports.json and stage pending_fix_notifications.json."""
    if not user_dir:
        return []
    opts = options if options is not None else _read_addon_options()
    roots = [r for r in _find_reports_roots(user_dir) if r.is_dir()]
    pending_paths = [r / "pending_fix_notifications.json" for r in roots]

    def _clear_pending():
        for pp in pending_paths:
            try:
                if pp.exists():
                    pp.unlink()
            except OSError:
                pass

    if opts.get("notifyFixedReports") is False:
        _clear_pending()
        return []
    if not roots:
        return []

    candidates = []
    seen_subs = set()
    for reports_root in roots:
        try:
            entries = sorted(reports_root.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for sub in entries:
            if not sub.is_dir() or ".." in sub.name or "/" in sub.name or "\\" in sub.name:
                continue
            if (sub / "notified_fixed.json").is_file():
                continue
            up_path = sub / "uploaded.json"
            if not up_path.is_file():
                continue
            try:
                up_doc = json.loads(up_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if not isinstance(up_doc, dict) or up_doc.get("ok") is not True:
                continue
            num = _parse_positive_int(up_doc.get("issueNumber"))
            if num is not None and (num, sub.name) not in seen_subs:
                seen_subs.add((num, sub.name))
                candidates.append((num, sub))

    if not candidates:
        _clear_pending()
        return []

    fixed_map = fetch_fixed_reports(fixed_url=fixed_url, timeout_s=timeout_s)
    grouped = {}
    for num, sub in sorted(candidates, key=lambda pair: (pair[0], pair[1].name)):
        fixed_entry = fixed_map.get(str(num))
        if not fixed_entry:
            continue
        rel_dir = f"beamngEarth/{REPORTS_SUBDIR}/{sub.name}/"
        bucket = grouped.get(num)
        if bucket is None:
            lat_val = None
            lon_val = None
            kind_val = fixed_entry["kind"]
            rep_path = sub / "report.json"
            if rep_path.is_file():
                try:
                    rep_doc = json.loads(rep_path.read_text(encoding="utf-8-sig"))
                    if isinstance(rep_doc, dict):
                        rlat = rep_doc.get("lat")
                        rlon = rep_doc.get("lon")
                        if (
                            isinstance(rlat, (int, float))
                            and not isinstance(rlat, bool)
                            and isinstance(rlon, (int, float))
                            and not isinstance(rlon, bool)
                            and math.isfinite(float(rlat))
                            and math.isfinite(float(rlon))
                            and -90.0 <= float(rlat) <= 90.0
                            and -180.0 <= float(rlon) <= 180.0
                        ):
                            lat_val = round(float(rlat), 6)
                            lon_val = round(float(rlon), 6)
                        rkind = str(rep_doc.get("kind") or "").lower()
                        if rkind in ALLOWED_KINDS:
                            kind_val = rkind
                except (OSError, ValueError):
                    pass
            grouped[num] = {
                "dir": rel_dir,
                "dirs": [rel_dir],
                "issueNumber": num,
                "fixedIn": fixed_entry["fixedIn"],
                "issueUrl": fixed_entry["issueUrl"],
                "lat": lat_val,
                "lon": lon_val,
                "kind": kind_val,
            }
        else:
            if rel_dir not in bucket["dirs"]:
                bucket["dirs"].append(rel_dir)

    items = [grouped[k] for k in sorted(grouped)]
    if not items:
        _clear_pending()
        return []

    for pp in pending_paths:
        try:
            _atomic_write_json(pp, {"version": 1, "items": items})
        except OSError as err:
            log.debug("traffic_autodrive_report: failed writing pending_fix_notifications.json (%s)", err)
    return items


def _parse_version_tuple(ver_str):
    if not isinstance(ver_str, str):
        return None
    m = _VERSION_RE.match(ver_str.strip())
    if not m:
        return None
    norm = m.group(1)
    core_part, _, pre_part = norm.partition("-")
    parts = tuple(int(p) for p in core_part.split("."))
    return parts, bool(pre_part), norm


def _sanitize_release_url(url):
    if not isinstance(url, str):
        return None
    cleaned = url.strip()
    if not cleaned.startswith(ALLOWED_RELEASE_URL_PREFIX):
        return None
    tail = cleaned[len(ALLOWED_RELEASE_URL_PREFIX):]
    if not tail or any(ch.isspace() or ord(ch) < 0x20 for ch in cleaned):
        return None
    return cleaned


def check_addon_update(
    user_dir,
    release_url=LATEST_RELEASE_API_URL,
    timeout_s=4.0,
    options=None,
    current_version=ADDON_VERSION,
):
    """Fetch latest GitHub release metadata once at launch and stage pending_update_notification.json when newer."""
    if not user_dir:
        return None
    opts = options if options is not None else _read_addon_options()
    roots = _find_reports_roots(user_dir)
    if not roots:
        roots = [Path(user_dir).resolve() / "beamngEarth" / REPORTS_SUBDIR]
    pending_paths = [r / "pending_update_notification.json" for r in roots]
    dismissed_paths = [r / "dismissed_update.json" for r in roots]

    def _clear_pending():
        for pp in pending_paths:
            try:
                if pp.exists():
                    pp.unlink()
            except OSError:
                pass

    if opts.get("notifyAddonUpdates") is False or os.environ.get("BNE_TELEMETRY", "").strip().lower() == "off":
        _clear_pending()
        return None
    if not release_url:
        _clear_pending()
        return None

    cur = _parse_version_tuple(current_version)
    if cur is None:
        _clear_pending()
        return None

    try:
        req = urllib.request.Request(
            release_url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"BeamNG-Earth-Autodrive-Fixes/{ADDON_VERSION}",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read(MAX_RELEASE_JSON_BYTES + 1)
        if len(raw) > MAX_RELEASE_JSON_BYTES:
            raise ValueError("release payload exceeds size cap")
        doc = json.loads(raw.decode("utf-8"))
    except Exception as err:
        log.debug("traffic_autodrive_report: addon update check skipped (%s)", err)
        _clear_pending()
        return None

    if not isinstance(doc, dict) or doc.get("draft") is True or doc.get("prerelease") is True:
        _clear_pending()
        return None

    tag_name = str(doc.get("tag_name") or "").strip()
    rel = _parse_version_tuple(tag_name)
    if rel is None or rel[1]:
        _clear_pending()
        return None
    if rel[0] < cur[0] or (rel[0] == cur[0] and not cur[1]):
        _clear_pending()
        return None

    safe_url = _sanitize_release_url(doc.get("html_url"))
    if not safe_url:
        _clear_pending()
        return None

    for dismissed_path in dismissed_paths:
        if dismissed_path.is_file():
            try:
                dis_doc = json.loads(dismissed_path.read_text(encoding="utf-8-sig"))
                if isinstance(dis_doc, dict):
                    skipped_ver = str(dis_doc.get("skippedVersion") or "").strip().lstrip("v")
                    if skipped_ver == rel[2]:
                        _clear_pending()
                        return None
            except (OSError, ValueError):
                pass

    item = {
        "version": 1,
        "latestVersion": rel[2],
        "currentVersion": cur[2],
        "tagName": tag_name,
        "releaseUrl": safe_url,
    }
    wrote_any = False
    for pp in pending_paths:
        try:
            _atomic_write_json(pp, item)
            wrote_any = True
        except OSError as err:
            log.debug("traffic_autodrive_report: failed writing pending_update_notification.json (%s)", err)
    if not wrote_any:
        return None
    return item


def check_fixed_reports_async(
    server,
    user_path=None,
    fixed_url=FIXED_REPORTS_URL,
    release_url=LATEST_RELEASE_API_URL,
):
    """Run check_fixed_reports and check_addon_update at most once per (server, user_dir) on one daemon thread."""
    target_dir = None
    if isinstance(user_path, str) and user_path.strip() and os.path.isdir(user_path.strip()):
        target_dir = os.path.normpath(user_path.strip())
    else:
        srv_dir = getattr(server, "user_dir", None)
        if srv_dir:
            target_dir = str(srv_dir)
    if not target_dir:
        return None

    dedup_key = (id(server), os.path.normcase(os.path.abspath(target_dir)))
    with _FIXED_CHECK_LOCK:
        if dedup_key in _FIXED_CHECKED_KEYS:
            return None
        _FIXED_CHECKED_KEYS.add(dedup_key)

    def _worker():
        try:
            check_fixed_reports(target_dir, fixed_url=fixed_url)
        except Exception as err:
            log.debug("traffic_autodrive_report: fixed check skipped (%s)", err)
        try:
            check_addon_update(target_dir, release_url=release_url)
        except Exception as err:
            log.debug("traffic_autodrive_report: update check skipped (%s)", err)

    t = threading.Thread(target=_worker, name="traffic-autodrive-fix-check", daemon=True)
    t.start()
    return t


def submit(server, msg, endpoint=DEFAULT_ENDPOINT, user_path=None):
    """Fire-and-forget daemon upload so earthd's event loop never blocks."""
    user_dir = getattr(server, "user_dir", None)
    t = threading.Thread(
        target=upload_report,
        args=(user_dir, msg),
        kwargs={"endpoint": endpoint, "user_path": user_path},
        name="traffic-autodrive-report",
        daemon=True,
    )
    t.start()
    return t
