"""1-Click Install & Uninstall for BeamNG.Earth - Traffic and AutoDrive fixes."""
import argparse
import ctypes
import glob
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zlib

TOOLS_DIR = Path(__file__).resolve().parent
ROOT = TOOLS_DIR.parent
RESOURCES_DIR = ROOT / "resources"
PAYLOAD_DIR = ROOT / "payload"


def _load_patch_server():
    ps_path = RESOURCES_DIR / "patch_server.py"
    spec = importlib.util.spec_from_file_location("patch_server", ps_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


patch_server = _load_patch_server()

APP_TITLE = "BeamNG.Earth - Traffic and AutoDrive fixes"
GITHUB_REPO = "CodeWMLandfill/BeamNG-Earth-Traffic-and-AutoDrive-Fixes"
RECEIPT_FILENAME = "install_receipt.json"
LAST_TARGET_FILENAME = "last_target.txt"

MOD_REL_FILES = tuple(
    (rel.removeprefix("mod/beamng_earth/"), rel) for rel in patch_server.MOD_FILES
)
REPORTER_REL_FILES = tuple(
    (rel.removeprefix("mod/beamng_earth/"), rel)
    for rel in (
        patch_server.MOD_TRAFFICREPORT_LUA,
        patch_server.MOD_ACTIONS_JSON,
        patch_server.MOD_KEYMAP_JSON,
        patch_server.MOD_SCRIPT_LUA,
    )
)


class SetupCancelled(Exception):
    pass


def _norm_key(path):
    try:
        s = str(Path(path).resolve())
    except OSError:
        s = str(path)
    return s.lower() if os.name == "nt" else s


def _is_supported_platform():
    return (
        os.name == "nt"
        or sys.platform.startswith("linux")
        or os.environ.get("BNE_ADF_ALLOW_NON_WINDOWS_TEST") == "1"
    )


def default_state_dir(local_appdata=None):
    override = os.environ.get("BNE_ADF_STATE_DIR")
    if override and local_appdata is None:
        return Path(override).resolve()
    if local_appdata is not None or os.name == "nt":
        base = Path(
            local_appdata
            or os.environ.get("LOCALAPPDATA")
            or (Path.home() / "AppData" / "Local")
        )
    else:
        base = Path(
            os.environ.get("XDG_STATE_HOME")
            or (Path.home() / ".local" / "state")
        )
    return base / "BeamNG-Earth-Autodrive-Fixes"


def read_json_safe(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {} if default is None else default


def atomic_write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _is_ignored_exe_name(name):
    lower = name.lower()
    return (
        f".{patch_server.TAG.lower()}." in lower
        or ".earthbridgerailfixes." in lower
        or ".original" in lower
        or ".pending" in lower
        or ".swap." in lower
        or lower.endswith(".json")
        or lower.endswith(".sha256")
    )


def _is_candidate_binary(path):
    if not path.is_file() or _is_ignored_exe_name(path.name):
        return False
    lower = path.name.lower()
    if lower.endswith((".exe", ".appimage")):
        return True
    if lower.endswith(".zip") and patch_server._is_host_zip_file(path):
        return True
    try:
        with path.open("rb") as f:
            head = f.read(64)
        if head[:4] == b"\x7fELF" or patch_server.APPIMAGE_WRAPPER_MAGIC[:16] in head:
            return True
    except OSError:
        pass
    return False


def normalize_candidate_target(raw_path):
    """Resolve raw exe/AppImage/Host/folder path into a valid BeamNG.Earth target path, or None."""
    if not raw_path:
        return None
    s = str(raw_path).strip().strip('"').strip("'")
    if not s:
        return None
    p = Path(s)
    try:
        p = p.resolve()
    except OSError:
        return None

    if p.is_file():
        if _is_candidate_binary(p):
            return p
        if p.name == "earthd.py" and p.parent.name == "server":
            cand = p.parent.parent
            if (cand / "mod" / "beamng_earth").is_dir():
                return cand
        return None

    if p.is_dir():
        if (p / "server" / "earthd.py").is_file() and (p / "mod" / "beamng_earth").is_dir():
            return p
        if (p / "Resources" / "Client" / "beamng_earth.zip").is_file():
            return p
        bins = sorted(
            [
                e for pat in ("BeamNG.Earth*.exe", "BeamNG.Earth*.AppImage", "BeamNG.Earth*")
                for e in p.glob(pat)
                if _is_candidate_binary(e)
            ],
            key=lambda e: e.stat().st_mtime,
            reverse=True,
        )
        if bins:
            return bins[0]
    return None


def _self_exe_excluded_pids(self_exe=False, exclude_pid=None, exclude_pids=None):
    """Build the set of own/bootloader PIDs to ignore when scanning running BeamNG.Earth instances."""
    excluded = {int(exclude_pid) if exclude_pid else os.getpid()}
    if exclude_pids:
        for p in exclude_pids:
            if p and int(p) > 0:
                excluded.add(int(p))
    if self_exe:
        ppid = os.getppid()
        if ppid and int(ppid) > 0:
            excluded.add(int(ppid))
        try:
            env_parent = int(os.environ.get("BNGEARTH_PARENT") or 0)
        except ValueError:
            env_parent = 0
        if env_parent > 0:
            excluded.add(env_parent)
    return excluded


def find_running_beamng_earth_processes(exclude_pid=None, *, self_exe=False, exclude_pids=None):
    """Return list of (pid, exe_path) for running BeamNG.Earth processes on Windows or Linux."""
    excluded = _self_exe_excluded_pids(self_exe=self_exe, exclude_pid=exclude_pid, exclude_pids=exclude_pids)
    found = []
    if os.name == "nt":
        kw = {"capture_output": True, "text": True, "timeout": 8}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW

        ps_script = (
            "Get-CimInstance Win32_Process -Filter \"Name LIKE 'BeamNG.Earth%.exe'\" | "
            "ForEach-Object { \"$($_.ProcessId)|$($_.ExecutablePath)\" }"
        )
        try:
            res = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                **kw,
            )
            if res.returncode == 0 and res.stdout.strip():
                for line in res.stdout.splitlines():
                    if "|" not in line:
                        continue
                    pid_s, exe_path = line.split("|", 1)
                    try:
                        pid = int(pid_s.strip())
                    except ValueError:
                        continue
                    if pid <= 0 or pid in excluded:
                        continue
                    found.append((pid, exe_path.strip()))
                return found
        except (OSError, subprocess.SubprocessError):
            pass

        try:
            res = subprocess.run(["tasklist", "/FI", "IMAGENAME eq BeamNG.Earth*", "/FO", "CSV", "/NH"], **kw)
            if res.returncode == 0 and res.stdout.strip():
                for line in res.stdout.splitlines():
                    cols = [c.strip().strip('"') for c in line.split('","')]
                    if len(cols) >= 2 and cols[0].lower().startswith("beamng.earth") and cols[1].isdigit():
                        pid = int(cols[1])
                        if pid > 0 and pid not in excluded:
                            found.append((pid, ""))
        except (OSError, subprocess.SubprocessError):
            pass
        return found

    proc_root = Path("/proc")
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid <= 0 or pid in excluded:
                continue
            try:
                appimage_p = ""
                env_raw = (entry / "environ").read_bytes()
                for item in env_raw.split(b"\x00"):
                    if item.startswith(b"APPIMAGE="):
                        appimage_p = item[9:].decode("utf-8", "replace").strip()
                        break
                if appimage_p and Path(appimage_p).is_file():
                    found.append((pid, str(Path(appimage_p).resolve())))
                    continue
                exe_p = (entry / "exe").resolve()
                if exe_p.name.lower().startswith("beamng.earth") and "/tmp/.mount_" not in str(exe_p):
                    found.append((pid, str(exe_p)))
            except OSError:
                continue
    return found


def discover_targets(search_roots=None, state_root=None, include_running=True):
    """Discover candidate BeamNG.Earth installations (.exe, .AppImage, Host, or source) in priority order."""
    candidates = []
    seen = set()

    def _add(raw_path):
        norm = normalize_candidate_target(raw_path)
        if norm is None:
            return
        key = _norm_key(norm)
        if key not in seen:
            seen.add(key)
            candidates.append(norm)

    sdir = Path(state_root) if state_root else default_state_dir()
    receipt = read_json_safe(sdir / RECEIPT_FILENAME, {})
    if receipt.get("target"):
        _add(receipt["target"])
    last_target_file = sdir / LAST_TARGET_FILENAME
    if last_target_file.is_file():
        try:
            _add(last_target_file.read_text(encoding="utf-8").strip())
        except OSError:
            pass

    if search_roots is not None:
        roots = [Path(r) for r in search_roots if r]
    else:
        if include_running:
            for _pid, exe_path in find_running_beamng_earth_processes():
                if exe_path:
                    _add(exe_path)

        ps_discover = getattr(patch_server, "discover_targets", None)
        if callable(ps_discover):
            try:
                for item in ps_discover():
                    if isinstance(item, dict):
                        if item.get("target"):
                            _add(item["target"])
                    else:
                        _add(item)
            except Exception:
                pass

        home = Path.home()
        roots = [
            Path.cwd(),
            Path.cwd().parent,
            ROOT,
            ROOT.parent,
            home / "Downloads",
            home / "Desktop",
            home / "Applications",
            home / ".local" / "bin",
        ]
        if os.name == "nt":
            local_app = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
            prog_files = os.environ.get("ProgramFiles")
            prog_x86 = os.environ.get("ProgramFiles(x86)")
            onedrive = os.environ.get("OneDrive")
            if onedrive:
                roots.append(Path(onedrive) / "Desktop")
                roots.append(Path(onedrive) / "Downloads")
            roots.extend([
                local_app / "Programs",
                local_app / "Programs" / "BeamNG.Earth",
                local_app / "beamng-earth",
            ])
            if prog_files:
                roots.append(Path(prog_files))
                roots.append(Path(prog_files) / "BeamNG.Earth")
            if prog_x86:
                roots.append(Path(prog_x86) / "BeamNG.Earth")
        else:
            cache_base = Path(os.environ.get("XDG_CACHE_HOME") or (home / ".cache"))
            roots.append(cache_base / "beamng-earth")

    for folder in roots:
        try:
            folder = Path(folder)
            if folder.is_file():
                _add(folder)
                continue
            if not folder.is_dir():
                continue
        except OSError:
            continue

        _add(folder)
        for pat in ("BeamNG.Earth*.exe", "BeamNG.Earth*.AppImage"):
            try:
                for bin_p in sorted(folder.glob(pat), key=lambda p: p.stat().st_mtime, reverse=True):
                    if _is_candidate_binary(bin_p):
                        _add(bin_p)
            except OSError:
                pass

        try:
            for sub in sorted(folder.glob("BeamNG.Earth*")):
                if sub.is_dir():
                    _add(sub)
                    for pat in ("BeamNG.Earth*.exe", "BeamNG.Earth*.AppImage"):
                        for bin_p in sorted(sub.glob(pat), key=lambda p: p.stat().st_mtime, reverse=True):
                            if _is_candidate_binary(bin_p):
                                _add(bin_p)
        except OSError:
            pass

    return candidates


def _parse_ini_userfolder(ini_path):
    try:
        text = Path(ini_path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith((";", "#")):
            continue
        m = re.match(r"^(?:userFolder|userpath)\s*=\s*(.+)$", stripped, flags=re.IGNORECASE)
        if m:
            val = m.group(1).strip().strip('"').strip("'")
            if val:
                return os.path.expandvars(os.path.expanduser(val))
    return None


def discover_beamng_user_dirs(local_appdata=None, *, linux_home=None, xdg_data_home=None, xdg_cache_home=None):
    """Return existing native BeamNG.drive user folders in exact upstream priority order."""
    if local_appdata is None and (linux_home is not None or sys.platform.startswith("linux")):
        return patch_server.discover_user_dirs(
            include_system=True,
            linux_home=linux_home,
            xdg_data_home=xdg_data_home,
            xdg_cache_home=xdg_cache_home,
        )

    base = Path(local_appdata or os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    raw_candidates = []

    app_json = read_json_safe(base / "beamng-earth" / "app.json", {})
    game_user_dir = app_json.get("gameUserDir")
    if isinstance(game_user_dir, str) and game_user_dir.strip():
        raw_candidates.append(Path(game_user_dir.strip()))

    for ini_cand in (
        base / "BeamNG" / "BeamNG.drive.ini",
        base / "BeamNG.drive" / "startup.ini",
        base / "BeamNG.drive" / "userpath.ini",
    ):
        uf = _parse_ini_userfolder(ini_cand)
        if uf:
            uf_path = Path(uf)
            if (uf_path / "current").is_dir():
                raw_candidates.append(uf_path / "current")
            else:
                raw_candidates.append(uf_path)

    raw_candidates.append(base / "BeamNG" / "BeamNG.drive" / "current")

    legacy_glob = sorted(glob.glob(str(base / "BeamNG.drive" / "[0-9]*")), reverse=True)
    for d in legacy_glob:
        raw_candidates.append(Path(d))

    out = []
    seen = set()
    for cand in raw_candidates:
        try:
            if cand.is_dir():
                key = _norm_key(cand)
                if key not in seen:
                    seen.add(key)
                    out.append(cand.resolve())
        except OSError:
            continue
    return out


def verify_target_compatibility(target, payload_dir=PAYLOAD_DIR):
    """Verify target compatibility before modifying any files on disk."""
    del payload_dir
    return patch_server.verify_compatibility(Path(target).resolve())


def _extract_mod_files_from_target(target):
    """Extract {rel_inside_beamng_earth: bytes} from a BeamNG.Earth .exe or source tree."""
    target = Path(target).resolve()
    extracted = {}
    if target.is_dir():
        for short_rel, full_rel in MOD_REL_FILES:
            p = target / full_rel
            if p.is_file():
                extracted[short_rel] = p.read_bytes()
        return extracted

    if target.is_file():
        try:
            data = target.read_bytes()
            start, _toff, _cookie, _py, _lib, entries = patch_server.archive_entries(data)
            emap = {e[7].replace("\\", "/"): e for e in entries}
            for short_rel, full_rel in MOD_REL_FILES:
                e = emap.get(full_rel)
                if e is not None:
                    blob = data[start + e[1]:start + e[1] + e[2]]
                    extracted[short_rel] = zlib.decompress(blob) if e[4] else blob
        except Exception:
            pass
    return extracted


def _target_supports_reporter(target):
    """Check whether target has input/modScript files and matching Python ABI for Ctrl+Shift+T reporter."""
    try:
        resolved, mode = patch_server._resolve_target_path(target)
    except Exception:
        return False
    if mode == "source":
        return all((resolved / rel).is_file() for rel in patch_server.REPORTER_SHARED_MOD_FILES)
    if mode in ("host_dir", "host_zip"):
        return False
    if mode == "appimage":
        info = patch_server.inspect_target(resolved)
        return bool(info.get("compatible") and info.get("pythonMatches"))
    try:
        data = resolved.read_bytes()
        start, _toff, _cpos, py, _lib, entries = patch_server.archive_entries(data)
        cur_py = sys.version_info.major * 100 + sys.version_info.minor
        if py != cur_py:
            return False
        emap = {e[7]: e for e in entries}
        if not all(patch_server._carchive_key(emap, rel) in emap for rel in patch_server.REPORTER_SHARED_MOD_FILES):
            return False
        pyz_cands = [e for e in entries if e[5] == b"z"]
        if len(pyz_cands) != 1:
            return False
        pz = patch_server._read_entry_bytes(data, start, pyz_cands[0])
        _off, _magic, toc = patch_server._read_pyz_toc(pz)
        if "earthd" not in toc:
            return False
        ek, ep, el = toc["earthd"]
        earthd_co = __import__("marshal").loads(zlib.decompress(pz[ep:ep + el]))
        patch_server._verify_earthd_dispatch_hook(earthd_co, pz_data=pz, offset=_off, toc=toc)
        return True
    except Exception:
        return False


def sync_unpacked_mods_on_install(
    user_dirs,
    payload_dir,
    state_root,
    apply_fixes=True,
    enable_reporter=False,
    patch_tier=patch_server.TIER_FULL_SOLVER,
):
    """Update <user_dir>/mods/unpacked/beamng_earth if present, backing up originals first."""
    payload_dir = Path(payload_dir)
    state_root = Path(state_root)
    synced_records = []
    target_mod_rels = (
        {(patch_server.MOD_MARKER_FILE.removeprefix("mod/beamng_earth/"), patch_server.MOD_MARKER_FILE)}
        if (not apply_fixes or patch_tier == patch_server.TIER_CLIENT_AND_REPORTER)
        else set(MOD_REL_FILES)
    )

    for udir in user_dirs:
        udir = Path(udir)
        mod_root = udir / "mods" / "unpacked" / "beamng_earth"
        if not mod_root.is_dir():
            continue

        dir_id = hashlib.sha256(_norm_key(mod_root).encode("utf-8")).hexdigest()[:12]
        bdir = state_root / "mod_backups" / dir_id
        bdir.mkdir(parents=True, exist_ok=True)
        files_meta = []

        for short_rel, full_rel in MOD_REL_FILES:
            if (short_rel, full_rel) not in target_mod_rels:
                continue
            dest = mod_root / short_rel
            bfile = bdir / short_rel
            meta_file = bdir / f"{short_rel}.meta.json"
            if not meta_file.is_file():
                existed = dest.is_file()
                orig_sha = ""
                if existed:
                    bfile.parent.mkdir(parents=True, exist_ok=True)
                    raw = dest.read_bytes()
                    bfile.write_bytes(raw)
                    orig_sha = patch_server.sha256_bytes(raw)
                meta_file.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(
                    meta_file,
                    {"rel": short_rel, "existed": existed, "originalSha": orig_sha},
                )
            meta = read_json_safe(meta_file, {"rel": short_rel, "existed": False, "originalSha": ""})
            files_meta.append(meta)

            if apply_fixes or enable_reporter:
                src = payload_dir / full_rel
                if src.is_file():
                    upstream_b = bfile.read_bytes() if bfile.is_file() else (dest.read_bytes() if dest.is_file() else None)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(
                        patch_server._patched_mod_bytes(
                            full_rel, src.read_bytes(), upstream_bytes=upstream_b, patch_tier=patch_tier
                        )
                    )

        if enable_reporter and all((mod_root / s).is_file() for s, _ in REPORTER_REL_FILES[1:]):
            for short_rel, full_rel in REPORTER_REL_FILES:
                dest = mod_root / short_rel
                bfile = bdir / short_rel
                meta_file = bdir / f"{short_rel}.meta.json"
                if not meta_file.is_file():
                    existed = dest.is_file()
                    orig_sha = ""
                    if existed:
                        bfile.parent.mkdir(parents=True, exist_ok=True)
                        raw = dest.read_bytes()
                        bfile.write_bytes(raw)
                        orig_sha = patch_server.sha256_bytes(raw)
                    meta_file.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_json(
                        meta_file,
                        {"rel": short_rel, "existed": existed, "originalSha": orig_sha},
                    )
                meta = read_json_safe(meta_file, {"rel": short_rel, "existed": False, "originalSha": ""})
                files_meta.append(meta)

                if full_rel == patch_server.MOD_TRAFFICREPORT_LUA:
                    src = payload_dir / full_rel
                    if src.is_file():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(src.read_bytes())
                elif full_rel == patch_server.MOD_ACTIONS_JSON:
                    dest.write_bytes(patch_server.patch_actions_json_bytes(dest.read_bytes()))
                elif full_rel == patch_server.MOD_KEYMAP_JSON:
                    dest.write_bytes(patch_server.patch_keymap_json_bytes(dest.read_bytes()))
                elif full_rel == patch_server.MOD_SCRIPT_LUA:
                    dest.write_bytes(patch_server.patch_modscript_bytes(dest.read_bytes()))

        synced_records.append({
            "userDir": str(udir),
            "modRoot": str(mod_root),
            "backupDir": str(bdir),
            "files": files_meta,
        })

    return synced_records


def restore_unpacked_mods_on_uninstall(restored_target, receipt, discovered_user_dirs):
    """Restore pristine unpacked mod files from target archive or verified backups."""
    pristine_mod_files = {}
    if restored_target and Path(restored_target).exists():
        try:
            pristine_mod_files = _extract_mod_files_from_target(restored_target)
        except Exception:
            pristine_mod_files = {}

    candidate_dirs = []
    seen = set()
    by_norm_udir = {}

    for rec in receipt.get("syncedUserDirs") or ():
        udir_str = rec.get("userDir")
        if udir_str:
            k = _norm_key(udir_str)
            by_norm_udir[k] = rec
            if k not in seen:
                seen.add(k)
                candidate_dirs.append(Path(udir_str))

    for udir in discovered_user_dirs or ():
        k = _norm_key(udir)
        if k not in seen:
            seen.add(k)
            candidate_dirs.append(Path(udir))

    cleaned = []
    for udir in candidate_dirs:
        mod_root = Path(udir) / "mods" / "unpacked" / "beamng_earth"
        if not mod_root.is_dir():
            continue

        rec = by_norm_udir.get(_norm_key(udir), {})
        bdir = Path(rec["backupDir"]) if rec.get("backupDir") else None
        meta_by_rel = {m.get("rel"): m for m in rec.get("files") or () if isinstance(m, dict)}

        for short_rel, _full_rel in MOD_REL_FILES:
            dest = mod_root / short_rel
            meta = meta_by_rel.get(short_rel, {})
            if bdir and (bdir / short_rel).is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes((bdir / short_rel).read_bytes())
            elif short_rel in pristine_mod_files:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(pristine_mod_files[short_rel])
            elif meta and not meta.get("existed") and dest.is_file():
                dest.unlink()

        for short_rel, full_rel in REPORTER_REL_FILES:
            dest = mod_root / short_rel
            if not dest.is_file():
                continue
            if full_rel == patch_server.MOD_TRAFFICREPORT_LUA:
                dest.unlink()
            elif full_rel == patch_server.MOD_ACTIONS_JSON:
                cur_b = dest.read_bytes()
                if b"beamngEarth_quickBridgeRailReport" in cur_b or not (bdir and (bdir / short_rel).is_file()):
                    dest.write_bytes(patch_server.unpatch_actions_json_bytes(cur_b))
                else:
                    dest.write_bytes((bdir / short_rel).read_bytes())
            elif full_rel == patch_server.MOD_KEYMAP_JSON:
                cur_b = dest.read_bytes()
                if b"beamngEarth_quickBridgeRailReport" in cur_b or not (bdir and (bdir / short_rel).is_file()):
                    dest.write_bytes(patch_server.unpatch_keymap_json_bytes(cur_b))
                else:
                    dest.write_bytes((bdir / short_rel).read_bytes())
            elif full_rel == patch_server.MOD_SCRIPT_LUA:
                cur_b = dest.read_bytes()
                if b"beamngEarth_bugreport" in cur_b or not (bdir and (bdir / short_rel).is_file()):
                    dest.write_bytes(patch_server.unpatch_modscript_bytes(cur_b))
                else:
                    dest.write_bytes((bdir / short_rel).read_bytes())

        cleaned.append(str(mod_root))

    return cleaned


def stop_processes(pids, timeout_s=10.0):
    """Close running BeamNG.Earth PIDs cleanly, escalating only if still open."""
    if not pids:
        return
    if os.name != "nt":
        import signal
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass
        deadline = time.monotonic() + min(3.0, timeout_s * 0.4)
        while time.monotonic() < deadline:
            alive = [pid for pid, _p in find_running_beamng_earth_processes() if pid in pids]
            if not alive:
                return
            time.sleep(0.2)
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass
        return

    kw = {"capture_output": True, "timeout": 5}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW

    for pid in pids:
        try:
            subprocess.run(["taskkill", "/T", "/PID", str(pid)], **kw)
        except (OSError, subprocess.SubprocessError):
            pass

    deadline = time.monotonic() + min(3.0, timeout_s * 0.4)
    while time.monotonic() < deadline:
        alive = [pid for pid, _p in find_running_beamng_earth_processes() if pid in pids]
        if not alive:
            return
        time.sleep(0.25)

    for pid in pids:
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], **kw)
        except (OSError, subprocess.SubprocessError):
            pass

    end_deadline = time.monotonic() + timeout_s
    while time.monotonic() < end_deadline:
        alive = [pid for pid, _p in find_running_beamng_earth_processes() if pid in pids]
        if not alive:
            return
        time.sleep(0.25)


def relaunch_beamng_earth(target):
    target = Path(target).resolve()
    env = patch_server._clean_posix_env()
    if os.name == "nt":
        flags = 0x00000200 | 0x00000008
        if target.is_file() and target.suffix.lower() == ".exe":
            subprocess.Popen(
                [str(target)],
                cwd=str(target.parent),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            return True
        launcher_py = target / "launcher.py"
        if target.is_dir() and launcher_py.is_file():
            subprocess.Popen(
                [sys.executable, str(launcher_py)],
                cwd=str(target),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            return True
        return False

    if target.is_file() and os.access(target, os.X_OK):
        subprocess.Popen(
            [str(target)],
            cwd=str(target.parent),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    launcher_py = target / "launcher.py"
    if target.is_dir() and launcher_py.is_file():
        py_bin = sys.executable if not getattr(sys, "frozen", False) else (shutil.which("python3") or "python3")
        subprocess.Popen(
            [py_bin, str(launcher_py)],
            cwd=str(target),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    return False


def _start_deferred_swap(status_path, helper_ps1):
    env = patch_server._clean_posix_env()
    env.pop("BNGEARTH_PARENT", None)
    if os.name == "nt":
        command = "& '{}' -Receipt '{}'".format(
            str(helper_ps1).replace("'", "''"),
            str(status_path).replace("'", "''"),
        )
        encoded = __import__("base64").b64encode(command.encode("utf-16le")).decode("ascii")
        tramp = subprocess.Popen(
            [
                os.environ.get("COMSPEC") or "cmd.exe",
                "/d",
                "/c",
                "start",
                "",
                "/b",
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            cwd=tempfile.gettempdir(),
            env=env,
            creationflags=0x08000000 | 0x00000200,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        tramp.wait(timeout=5)
        return

    posix_helper = Path(helper_ps1).parent / "deferred_posix.py"
    if not getattr(sys, "frozen", False) and sys.executable and Path(sys.executable).is_file():
        py_cmd = [sys.executable, "-B"]
    else:
        py_bin = shutil.which("python3.12", path=env.get("PATH")) or shutil.which("python3", path=env.get("PATH")) or shutil.which("python", path=env.get("PATH"))
        if not py_bin:
            raise patch_server.PatchError("Python 3 is required on Linux to finalize the background swap.")
        py_cmd = [py_bin, "-B"]
    subprocess.Popen(
        [*py_cmd, str(posix_helper), "--receipt", str(status_path)],
        cwd=tempfile.gettempdir(),
        env=env,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _cleanup_after_restore(target, mode, state_root):
    """Delete backup, sidecar, stage dirs, and install receipt once original bytes are verified."""
    target = Path(target)
    state_root = Path(state_root)

    try:
        patch_server.cleanup_backup(target)
    except Exception:
        pass

    if mode in ("exe", "appimage", "host_zip"):
        backup = target.with_name(f"{target.name}.{patch_server.TAG}.original")
        sidecar = target.with_name(f"{target.name}.{patch_server.TAG}.json")
        for f in (backup, sidecar):
            if f.is_file():
                f.unlink()
        for pend in target.parent.glob(f"{target.name}.{patch_server.TAG}.*.pending"):
            try:
                pend.unlink()
            except OSError:
                pass
    elif mode in ("source", "host_dir"):
        tag_dir = target / f".{patch_server.TAG}"
        if tag_dir.is_dir():
            shutil.rmtree(tag_dir, ignore_errors=True)

    for rel in (RECEIPT_FILENAME, LAST_TARGET_FILENAME, "control.json", "patch-status.json"):
        p = state_root / rel
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass
    for sub_name in ("mod_backups", "zip_mod_backups", "unpacked_mod_backups"):
        sub_dir = state_root / sub_name
        if sub_dir.is_dir():
            shutil.rmtree(sub_dir, ignore_errors=True)
    for appdir_cand in state_root.glob("patched_appdir_*"):
        if appdir_cand.is_dir():
            shutil.rmtree(appdir_cand, ignore_errors=True)
    try:
        if state_root.is_dir() and not any(state_root.iterdir()):
            state_root.rmdir()
    except OSError:
        pass


def perform_install(
    target,
    *,
    state_root=None,
    payload_dir=PAYLOAD_DIR,
    user_dirs=None,
    local_appdata=None,
    apply_fixes=True,
    enable_reporter=None,
    self_exe=False,
    relaunch_after=False,
    notify_on_defer=False,
):
    """Install Traffic and AutoDrive fixes onto target and sync BeamNG.drive mod folders."""
    target = normalize_candidate_target(target)
    if target is None:
        raise patch_server.PatchError("Could not find a valid BeamNG.Earth installation at that path.")

    state_root = Path(state_root) if state_root else default_state_dir(local_appdata=local_appdata)
    state_root.mkdir(parents=True, exist_ok=True)
    payload_dir = Path(payload_dir).resolve()

    verify_target_compatibility(target, payload_dir=payload_dir)
    eff_reporter = _target_supports_reporter(target) if enable_reporter is None else bool(enable_reporter)

    control_path = state_root / "control.json"
    status_path = state_root / "patch-status.json"
    req_token = uuid.uuid4().hex
    atomic_write_json(
        control_path,
        {
            "enabled": bool(apply_fixes or eff_reporter),
            "modPath": str(Path(__file__).resolve()),
            "requestToken": req_token,
        },
    )

    record = patch_server.prepare(
        target,
        control_path,
        status_path,
        payload_dir,
        operation="install",
        token=req_token,
        apply_fixes=apply_fixes,
        enable_reporter=eff_reporter,
    )

    discovered_udirs = list(user_dirs) if user_dirs is not None else discover_beamng_user_dirs(local_appdata=local_appdata)
    synced_mods = sync_unpacked_mods_on_install(
        discovered_udirs,
        payload_dir,
        state_root,
        apply_fixes=apply_fixes,
        enable_reporter=eff_reporter,
        patch_tier=record.get("patchTier", patch_server.TIER_FULL_SOLVER),
    )
    patch_server.sync_discovered_user_mods(
        payload_dir,
        state_root,
        user_dirs=discovered_udirs,
        operation="install",
        apply_fixes=apply_fixes,
        enable_reporter=eff_reporter,
        patch_tier=record.get("patchTier", patch_server.TIER_FULL_SOLVER),
    )

    receipt_path = state_root / RECEIPT_FILENAME
    last_target_path = state_root / LAST_TARGET_FILENAME
    last_target_path.write_text(str(target) + "\n", encoding="utf-8")

    if record["state"] == "pending":
        if self_exe:
            record.update(
                standalone=True,
                companionPid=os.getpid(),
                notify=bool(notify_on_defer),
                skipReadyNotice=True,
                relaunchAfterSwap=bool(relaunch_after),
                installReceipt=str(receipt_path),
                lastTargetFile=str(last_target_path),
                completionNotice=(
                    "Traffic and AutoDrive fixes installed cleanly! Start BeamNG.Earth and use Traffic / Autodrive."
                    if notify_on_defer
                    else ""
                ),
            )
            atomic_write_json(record["sidecar"], record)
            atomic_write_json(status_path, record)
            atomic_write_json(
                receipt_path,
                {
                    "version": patch_server.VERSION,
                    "state": "pending",
                    "target": str(target),
                    "mode": record.get("mode", "exe"),
                    "originalSha": record.get("originalSha", ""),
                    "patchedSha": record.get("patchedSha", ""),
                    "backup": record.get("backup", ""),
                    "sidecar": record.get("sidecar", ""),
                    "controlPath": str(control_path),
                    "statusPath": str(status_path),
                    "syncedUserDirs": synced_mods,
                },
            )
            _start_deferred_swap(status_path, RESOURCES_DIR / "deferred.ps1")
            return record

        last_err = None
        for _ in range(10):
            try:
                record = patch_server.apply_now(record)
                last_err = None
                break
            except PermissionError as exc:
                last_err = exc
                time.sleep(0.5)
        if last_err is not None:
            raise patch_server.PatchError(
                "BeamNG.Earth is still open. Close BeamNG.Earth completely and run setup again."
            ) from last_err

    receipt_data = {
        "version": patch_server.VERSION,
        "state": "installed",
        "installedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "target": str(target),
        "mode": record.get("mode", "exe" if target.is_file() else "source"),
        "originalSha": record.get("originalSha", ""),
        "patchedSha": record.get("patchedSha", ""),
        "backup": record.get("backup", ""),
        "sidecar": record.get("sidecar", ""),
        "controlPath": str(control_path),
        "statusPath": str(status_path),
        "syncedUserDirs": synced_mods,
    }
    atomic_write_json(receipt_path, receipt_data)

    if relaunch_after:
        relaunch_beamng_earth(target)

    return dict(record, syncedUserDirs=synced_mods, receiptPath=str(receipt_path))


def perform_uninstall(
    target=None,
    *,
    state_root=None,
    payload_dir=PAYLOAD_DIR,
    user_dirs=None,
    local_appdata=None,
    self_exe=False,
    relaunch_after=False,
    notify_on_defer=False,
):
    """Restore original BeamNG.Earth byte-for-byte and remove mod overrides + backups + receipts."""
    state_root = Path(state_root) if state_root else default_state_dir(local_appdata=local_appdata)
    receipt_path = state_root / RECEIPT_FILENAME
    receipt = read_json_safe(receipt_path, {})

    if target is None and receipt.get("target"):
        target = Path(receipt["target"])
    target = normalize_candidate_target(target)
    if target is None:
        raise patch_server.PatchError("Could not locate a patched BeamNG.Earth installation to restore.")

    state_root.mkdir(parents=True, exist_ok=True)
    control_path = state_root / "control.json"
    status_path = state_root / "patch-status.json"
    req_token = uuid.uuid4().hex
    atomic_write_json(
        control_path,
        {
            "enabled": False,
            "modPath": str(Path(__file__).resolve()),
            "requestToken": req_token,
        },
    )

    record = patch_server.prepare(
        target,
        control_path,
        status_path,
        payload_dir,
        operation="restore",
        token=req_token,
    )

    discovered_udirs = list(user_dirs) if user_dirs is not None else discover_beamng_user_dirs(local_appdata=local_appdata)

    if record["state"] == "pending":
        if self_exe:
            cleaned_mods = restore_unpacked_mods_on_uninstall(None, receipt, discovered_udirs)
            patch_server.sync_discovered_user_mods(
                payload_dir,
                state_root,
                user_dirs=discovered_udirs,
                operation="restore",
            )
            record.update(
                standalone=True,
                companionPid=os.getpid(),
                notify=bool(notify_on_defer),
                skipReadyNotice=True,
                cleanupOnRestore=True,
                relaunchAfterSwap=bool(relaunch_after),
                installReceipt=str(receipt_path),
                lastTargetFile=str(state_root / LAST_TARGET_FILENAME),
                completionNotice="Original BeamNG.Earth has been restored cleanly." if notify_on_defer else "",
            )
            atomic_write_json(record["sidecar"], record)
            atomic_write_json(status_path, record)
            _start_deferred_swap(status_path, RESOURCES_DIR / "deferred.ps1")
            return dict(record, cleanedMods=cleaned_mods)

        last_err = None
        for _ in range(10):
            try:
                record = patch_server.apply_now(record)
                last_err = None
                break
            except PermissionError as exc:
                last_err = exc
                time.sleep(0.5)
        if last_err is not None:
            raise patch_server.PatchError(
                "BeamNG.Earth is still open. Close BeamNG.Earth completely and run uninstall again."
            ) from last_err

    mode = record.get("mode", "exe" if target.is_file() else "source")
    if mode in ("exe", "appimage", "host_zip") and record.get("originalSha"):
        actual_sha = patch_server.sha256_file(target)
        if actual_sha != record["originalSha"] and actual_sha != record.get("replacementSha"):
            raise patch_server.PatchError("Restored target checksum mismatch; backup kept for safety.")

    cleaned_mods = restore_unpacked_mods_on_uninstall(target, receipt, discovered_udirs)
    patch_server.sync_discovered_user_mods(
        payload_dir,
        state_root,
        user_dirs=discovered_udirs,
        operation="restore",
    )
    _cleanup_after_restore(target, mode, state_root)

    if relaunch_after:
        relaunch_beamng_earth(target)

    return dict(record, state="restored", cleanedMods=cleaned_mods)


class SetupUI:
    """Friendly GUI (tkinter / Win32 MessageBox) with automatic plain-text console fallback."""

    def __init__(self, use_gui=True, assume_yes=False):
        self.assume_yes = bool(assume_yes)
        self.use_gui = bool(use_gui and not assume_yes and os.environ.get("BNE_ADF_TEST_NO_PROMPT") != "1")
        self._tk_available = None

    def _has_tk(self):
        if self._tk_available is not None:
            return self._tk_available
        if not self.use_gui:
            self._tk_available = False
            return False
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            root.destroy()
            self._tk_available = True
        except Exception:
            self._tk_available = False
        return self._tk_available

    def say(self, text=""):
        try:
            print(f"  {text}" if text else "", flush=True)
        except OSError:
            pass

    def banner(self, subtitle):
        self.say("")
        self.say("=" * 72)
        self.say(f"{APP_TITLE} - {subtitle}")
        self.say("=" * 72)
        self.say("")

    def info(self, message, title=APP_TITLE):
        self.say(message)
        if not self.use_gui:
            return
        if self._has_tk():
            try:
                import tkinter as tk
                from tkinter import messagebox
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                messagebox.showinfo(title, message, parent=root)
                root.destroy()
                return
            except Exception:
                pass
        if os.name == "nt":
            try:
                ctypes.windll.user32.MessageBoxW(None, message, title, 0x40 | 0x10000 | 0x40000)
            except Exception:
                pass

    def error(self, message, title=f"{APP_TITLE} - Setup Problem"):
        self.say("")
        self.say("PROBLEM: " + message)
        if not self.use_gui:
            return
        if self._has_tk():
            try:
                import tkinter as tk
                from tkinter import messagebox
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                messagebox.showerror(title, message, parent=root)
                root.destroy()
                return
            except Exception:
                pass
        if os.name == "nt":
            try:
                ctypes.windll.user32.MessageBoxW(None, message, title, 0x10 | 0x10000 | 0x40000)
            except Exception:
                pass

    def ask_yes_no(self, question, default=True):
        if self.assume_yes:
            return True
        if self.use_gui:
            if self._has_tk():
                try:
                    import tkinter as tk
                    from tkinter import messagebox
                    root = tk.Tk()
                    root.withdraw()
                    root.attributes("-topmost", True)
                    ans = messagebox.askyesno(APP_TITLE, question, parent=root)
                    root.destroy()
                    return bool(ans)
                except Exception:
                    pass
            if os.name == "nt":
                try:
                    res = ctypes.windll.user32.MessageBoxW(None, question, APP_TITLE, 0x04 | 0x20 | 0x10000 | 0x40000)
                    return res == 6
                except Exception:
                    pass
        suffix = "[Y/n]" if default else "[y/N]"
        try:
            raw = input(f"  {question} {suffix}: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if not raw:
            return default
        return raw in ("y", "yes")

    def pick_target_when_missing(self):
        """Ask the user to locate BeamNG.Earth.exe (or a source folder) via file dialog or console."""
        if self.assume_yes:
            return None
        self.say("BeamNG.Earth was not found in your standard Downloads, Desktop, or Programs folders.")
        if self._has_tk():
            try:
                import tkinter as tk
                from tkinter import filedialog, messagebox
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                ok = messagebox.askokcancel(
                    APP_TITLE,
                    "Could not find BeamNG.Earth automatically.\n\n"
                    "Click OK to select your BeamNG.Earth.exe file (for example in the folder where you downloaded it).",
                    parent=root,
                )
                if not ok:
                    root.destroy()
                    raise SetupCancelled("Setup cancelled.")
                picked = filedialog.askopenfilename(
                    parent=root,
                    title="Select BeamNG.Earth.exe",
                    filetypes=[("BeamNG.Earth Executable", "BeamNG.Earth*.exe *.exe"), ("All Files", "*.*")],
                )
                if not picked:
                    picked = filedialog.askdirectory(
                        parent=root,
                        title="Or select a BeamNG.Earth source folder (Cancel to quit)",
                    )
                root.destroy()
                if not picked:
                    raise SetupCancelled("No BeamNG.Earth location was selected.")
                return normalize_candidate_target(picked)
            except SetupCancelled:
                raise
            except Exception:
                pass

        self.say("Paste the path to your BeamNG.Earth.exe (or drag-and-drop it into this window), then press Enter:")
        try:
            typed = input("  Path: ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise SetupCancelled("Setup cancelled.") from exc
        if not typed:
            raise SetupCancelled("No path entered.")
        return normalize_candidate_target(typed)

    def choose_from_multiple_targets(self, targets):
        """Let user pick when more than one BeamNG.Earth installation exists."""
        if self.assume_yes or len(targets) == 1:
            return targets[0]

        if self._has_tk():
            try:
                import tkinter as tk
                selected = {"index": None}
                win = tk.Tk()
                win.title(APP_TITLE)
                win.attributes("-topmost", True)
                win.resizable(False, False)

                tk.Label(
                    win,
                    text="More than one BeamNG.Earth copy was found.\nChoose which one to use:",
                    justify="left",
                    padx=16,
                    pady=12,
                ).pack(anchor="w")

                choice_var = tk.IntVar(master=win, value=0)
                for idx, path in enumerate(targets):
                    tk.Radiobutton(
                        win,
                        text=str(path),
                        variable=choice_var,
                        value=idx,
                        justify="left",
                        padx=16,
                    ).pack(anchor="w")

                btn_frame = tk.Frame(win, pady=12, padx=16)
                btn_frame.pack(fill="x")

                def _confirm():
                    selected["index"] = int(choice_var.get())
                    win.destroy()

                def _cancel():
                    win.destroy()

                tk.Button(btn_frame, text="Continue", width=14, command=_confirm).pack(side="right", padx=(8, 0))
                tk.Button(btn_frame, text="Cancel", width=12, command=_cancel).pack(side="right")
                win.mainloop()
                if selected["index"] is None:
                    raise SetupCancelled("Setup cancelled.")
                return targets[selected["index"]]
            except SetupCancelled:
                raise
            except Exception:
                pass

        self.say("Found more than one BeamNG.Earth installation:")
        for idx, t in enumerate(targets, 1):
            self.say(f"  {idx}. {t}")
        try:
            raw = input(f"  Choose [1-{len(targets)}, Enter = 1]: ").strip() or "1"
        except (EOFError, KeyboardInterrupt) as exc:
            raise SetupCancelled("Setup cancelled.") from exc
        if raw.isdigit() and 1 <= int(raw) <= len(targets):
            return targets[int(raw) - 1]
        return targets[0]


def _matching_running_pids(target, self_exe=False):
    target_norm = _norm_key(target)
    excluded = _self_exe_excluded_pids(self_exe=self_exe)
    try:
        running = find_running_beamng_earth_processes(self_exe=self_exe, exclude_pids=excluded)
    except TypeError:
        running = find_running_beamng_earth_processes()
    pids = []
    for pid, exe_path in running:
        if pid in excluded:
            continue
        if not exe_path:
            if Path(target).is_file():
                pids.append(pid)
        elif _norm_key(exe_path) == target_norm:
            pids.append(pid)
    return pids


def run_interactive(action, args, ui=None):
    ui = ui or SetupUI(use_gui=not args.no_gui, assume_yes=args.yes)
    state_root = Path(args.state_dir).resolve() if args.state_dir else default_state_dir()
    user_dirs = [Path(p).resolve() for p in args.user_dir] if args.user_dir else None

    ui.banner("1-Click Install" if action == "install" else "1-Click Uninstall")

    self_exe = False
    if args.self_exe:
        target = normalize_candidate_target(args.self_exe)
        self_exe = target is not None
    elif args.target:
        target = normalize_candidate_target(args.target)
    else:
        if action == "uninstall":
            receipt = read_json_safe(state_root / RECEIPT_FILENAME, {})
            if receipt.get("target") and normalize_candidate_target(receipt["target"]):
                target = normalize_candidate_target(receipt["target"])
            else:
                found = discover_targets(state_root=state_root)
                target = ui.choose_from_multiple_targets(found) if len(found) > 1 else (found[0] if found else None)
        else:
            found = discover_targets(state_root=state_root)
            if len(found) == 1:
                target = found[0]
            elif len(found) > 1:
                target = ui.choose_from_multiple_targets(found)
            else:
                target = None

        if target is None:
            target = ui.pick_target_when_missing()

    if target is None:
        raise patch_server.PatchError(
            "Could not find BeamNG.Earth.\n\n"
            f"Make sure BeamNG.Earth ({patch_server.MIN_SUPPORTED_EARTH_VERSION} or newer) is downloaded on this machine, then try again."
        )

    ui.say(f"Target BeamNG.Earth : {target}")

    if action == "install":
        verify_target_compatibility(target, payload_dir=PAYLOAD_DIR)

    running_pids = _matching_running_pids(target, self_exe=self_exe)
    relaunch_after = bool(args.relaunch)

    if running_pids:
        verb = "install Traffic and AutoDrive fixes" if action == "install" else "restore original BeamNG.Earth"
        ok = ui.ask_yes_no(
            f"BeamNG.Earth is currently open.\n\n"
            f"Close BeamNG.Earth automatically now, {verb}, and start it again when finished?",
            default=True,
        )
        if not ok:
            raise SetupCancelled("Setup cancelled so your running BeamNG.Earth session stays open.")
        ui.say("Closing running BeamNG.Earth window...")
        stop_processes(running_pids)
        if not args.no_relaunch:
            relaunch_after = True

    if action == "install":
        ui.say("Applying Traffic and AutoDrive fixes and verifying SHA-256 backups...")
        result = perform_install(
            target,
            state_root=state_root,
            payload_dir=PAYLOAD_DIR,
            user_dirs=user_dirs,
            self_exe=self_exe,
            relaunch_after=relaunch_after,
            notify_on_defer=ui.use_gui,
        )
        if result.get("state") == "pending" and self_exe:
            ui.say("Finalizing swap in the background (1-2 seconds)...")
            return 0
        synced_n = len(result.get("syncedUserDirs") or ())
        msg = (
            f"Success! Traffic and AutoDrive fixes is now installed.\n\n"
            f"Target: {target}\n"
            + (f"Synced BeamNG.drive mod folder(s): {synced_n}\n" if synced_n else "")
            + "\nIncludes highway traffic spawns, cloaked pre-roll, merge lanes, GPS gore/elevation snapping, Tune live sliders, Race mode, overtake & wreck escape.\n"
            + ("BeamNG.Earth is starting up now." if relaunch_after else "You can now start BeamNG.Earth normally.")
        )
        ui.info(msg)
        return 0

    ui.say("Restoring original BeamNG.Earth from verified backup...")
    result = perform_uninstall(
        target,
        state_root=state_root,
        payload_dir=PAYLOAD_DIR,
        user_dirs=user_dirs,
        self_exe=self_exe,
        relaunch_after=relaunch_after,
        notify_on_defer=ui.use_gui,
    )
    if result.get("state") == "pending" and self_exe:
        ui.say("Finalizing restore in the background (1-2 seconds)...")
        return 0

    if not args.no_relaunch and not relaunch_after and not args.yes and ui.use_gui:
        if ui.ask_yes_no(
            "Original BeamNG.Earth has been restored byte-for-byte and all add-on overrides were removed.\n\n"
            "Would you like to start BeamNG.Earth now?",
            default=False,
        ):
            relaunch_beamng_earth(target)
            return 0

    ui.info(
        f"Original BeamNG.Earth has been restored cleanly!\n\n"
        f"Restored: {target}\n"
        f"All Traffic and AutoDrive fixes mod overrides, backups, and receipts have been removed."
    )
    return 0


def build_arg_parser():
    parser = argparse.ArgumentParser(description="1-Click Install & Uninstall for BeamNG.Earth - Traffic and AutoDrive fixes.")
    parser.add_argument("action", nargs="?", choices=("install", "uninstall", "auto"), default="auto")
    parser.add_argument("--uninstall", action="store_true", help="Restore original BeamNG.Earth and remove overrides")
    parser.add_argument("--target", type=Path, help="Explicit path to BeamNG.Earth.exe or source folder")
    parser.add_argument("--self-exe", type=Path, help="Internal flag when invoked via BeamNG.Earth.exe --run-addon")
    parser.add_argument("--user-dir", action="append", default=[], help="Explicit BeamNG.drive user folder(s)")
    parser.add_argument("--state-dir", type=Path, help="Override receipt/backup metadata directory")
    parser.add_argument("--yes", "-y", action="store_true", help="Accept prompts non-interactively")
    parser.add_argument("--no-gui", action="store_true", help="Console text output only (no popup dialogs)")
    parser.add_argument("--relaunch", action="store_true", help="Start BeamNG.Earth after setup finishes")
    parser.add_argument("--no-relaunch", action="store_true", help="Never start BeamNG.Earth automatically")
    return parser


def main(argv=None):
    if not _is_supported_platform():
        print("This installer supports Windows 10/11 and Linux x86_64.", file=sys.stderr)
        return 1

    parser = build_arg_parser()
    args = parser.parse_args(argv)
    ui = SetupUI(use_gui=not args.no_gui, assume_yes=args.yes)

    action = "uninstall" if args.uninstall else args.action
    if action == "auto":
        state_root = Path(args.state_dir).resolve() if args.state_dir else default_state_dir()
        receipt = read_json_safe(state_root / RECEIPT_FILENAME, {})
        if receipt.get("state") == "installed" and not args.yes:
            uninstall_now = ui.ask_yes_no(
                "Traffic and AutoDrive fixes is already installed.\n\n"
                "Click Yes to UNINSTALL and restore original BeamNG.Earth, or No to RE-INSTALL / repair.",
                default=True,
            )
            action = "uninstall" if uninstall_now else "install"
        else:
            action = "install"

    try:
        return run_interactive(action, args, ui=ui)
    except SetupCancelled as cancel_exc:
        ui.say(str(cancel_exc))
        return 0
    except patch_server.PatchError as patch_err:
        ui.error(str(patch_err))
        return 1
    except Exception as exc:
        ui.error(f"Unexpected setup problem: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
