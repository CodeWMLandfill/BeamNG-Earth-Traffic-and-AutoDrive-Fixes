"""Detached POSIX helper for BeamNG.Earth - Traffic and AutoDrive fixes.

Replaces PowerShell deferred.ps1 on Linux (and POSIX systems) using pure Python
stdlib: waits for companion/server PIDs to exit, verifies SHA-256 checksums and
request tokens, applies staged Host-Linux ELF CArchive, source-tree, or AppImage
user-mod updates atomically while preserving executable file permissions, and
optionally re-verifies AppImage user_dir mods during BeamNG.Earth autostart.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
import uuid

TAG = "earthAutodriveFixes"


def write_json_atomic(destination, value):
    if not destination:
        return
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.replace(tmp, dest)


def read_json_file(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {} if default is None else default


def save_record(record, receipt_path):
    for dst in (record.get("sidecar"), receipt_path):
        if dst:
            write_json_atomic(dst, record)


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def pid_alive(pid):
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid():
        return False
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = k32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
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
            if rparen > 0:
                state_ch = txt[rparen + 2:rparen + 3]
                if state_ch == "Z":
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


def target_binary_busy_posix(target_path, exclude_pids=None):
    if os.name == "nt":
        return False
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return False
    excluded = {os.getpid()}
    if exclude_pids:
        excluded.update(int(p) for p in exclude_pids if p)
    try:
        target_real = Path(target_path).resolve()
    except OSError:
        return False
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in excluded:
            continue
        try:
            exe_link = (entry / "exe").resolve()
            if exe_link == target_real and pid_alive(pid):
                return True
        except OSError:
            pass
        try:
            env_raw = (entry / "environ").read_bytes()
            for item in env_raw.split(b"\x00"):
                if item.startswith(b"APPIMAGE="):
                    ap = Path(item[9:].decode("utf-8", "replace")).resolve()
                    if ap == target_real and pid_alive(pid):
                        return True
        except OSError:
            continue
    return False


def cleanup_staged(rec):
    mode = rec.get("mode")
    if mode == "source":
        stage_dir = rec.get("stageDir")
        if stage_dir and Path(stage_dir).is_dir():
            shutil.rmtree(stage_dir, ignore_errors=True)
    elif mode == "exe":
        pending = rec.get("pending")
        if pending and Path(pending).is_file():
            try:
                if sha256_file(pending) == rec.get("replacementSha"):
                    Path(pending).unlink()
            except OSError:
                pass


def mod_active(configuration):
    mod_path = configuration.get("modPath")
    if not configuration.get("enabled") or not mod_path or not Path(mod_path).is_file():
        return False
    return True


def _load_sibling_patch_server():
    ps_path = Path(__file__).resolve().parent / "patch_server.py"
    if not ps_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_taf_deferred_patch_server", ps_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def reverify_appimage_loop(receipt_path, watch_seconds=15.0, poll_interval=0.5):
    """Watch AppImage user_dir mods briefly during autostart in case earthd re-stages beamng_earth.zip."""
    rec = read_json_file(receipt_path, {})
    if not rec or rec.get("mode") != "appimage" or rec.get("operation") != "install":
        return 0
    ps = _load_sibling_patch_server()
    if ps is None:
        return 0
    companion_pid = int(rec.get("companionPid") or 0)
    comp_deadline = time.monotonic() + 10.0
    while companion_pid > 0 and pid_alive(companion_pid) and time.monotonic() < comp_deadline:
        time.sleep(0.1)

    server_pid = int(rec.get("serverPid") or 0)
    payload_dir = rec.get("payloadDir")
    if not payload_dir or not Path(payload_dir).is_dir():
        cand = Path(__file__).resolve().parent.parent / "payload"
        payload_dir = str(cand) if cand.is_dir() else ""
    if not payload_dir:
        return 0

    deadline = time.monotonic() + max(0.0, float(watch_seconds))
    while True:
        latest = read_json_file(receipt_path, {})
        cfg = read_json_file(rec.get("config", ""), {})
        if latest.get("token") != rec.get("token") or (cfg and cfg.get("requestToken") != rec.get("token")):
            return 0
        if not cfg.get("enabled", True):
            return 0
        try:
            ps.ensure_appimage_user_mods_patched(
                rec.get("target", ""),
                payload_dir=Path(payload_dir),
                state_dir=Path(receipt_path).resolve().parent,
                user_dirs=rec.get("userDirs") or None,
                apply_fixes=bool(rec.get("applyFixes", True)),
                enable_reporter=bool(rec.get("enableReporter", False)),
                record_out=rec,
            )
            save_record(rec, receipt_path)
        except Exception:
            pass
        if time.monotonic() >= deadline:
            break
        if server_pid > 0 and not pid_alive(server_pid):
            break
        time.sleep(max(0.05, float(poll_interval)))
    return 0


def run_deferred_swap(receipt_path, timeout_seconds=43200.0, poll_seconds=0.5):
    receipt_path = Path(receipt_path).resolve()
    record = read_json_file(receipt_path, {})
    if not record:
        return 1

    try:
        target = Path(record["target"]).resolve()
        mode = record.get("mode", "exe")
        pending = None
        if mode == "exe":
            pending = Path(record["pending"]).resolve()
            expected_prefix = f"{target.name}.{TAG}."
            if (
                pending.parent != target.parent
                or not pending.name.startswith(expected_prefix)
                or not pending.name.endswith(".pending")
            ):
                raise RuntimeError("Invalid staged-file path.")

        companion_pid = int(record.get("companionPid") or 0)
        if companion_pid > 0:
            comp_end = time.monotonic() + 30.0
            while pid_alive(companion_pid):
                if time.monotonic() > comp_end:
                    raise RuntimeError("Setup did not finish closing. Please try again.")
                time.sleep(0.1)

        server_pid = int(record.get("serverPid") or 0)
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        ready = False
        while time.monotonic() < deadline:
            latest = read_json_file(receipt_path, {})
            cfg = read_json_file(record.get("config", ""), {})
            if latest.get("token") != record.get("token") or cfg.get("requestToken") != record.get("token"):
                cleanup_staged(record)
                return 0
            busy = (server_pid > 0 and pid_alive(server_pid))
            if not busy and mode in ("exe", "appimage"):
                busy = target_binary_busy_posix(target, exclude_pids={companion_pid})
            if not busy:
                ready = True
                break
            time.sleep(max(0.05, float(poll_seconds)))

        if not ready:
            raise RuntimeError("Timed out waiting for BeamNG.Earth to close. Please try again.")

        cfg = read_json_file(record.get("config", ""), {})
        if record.get("operation") == "install" and not record.get("standalone") and not mod_active(cfg):
            cleanup_staged(record)
            record["state"] = "cancelled"
            record["message"] = "The add-on was disabled or removed; patch cancelled."
            save_record(record, receipt_path)
            return 0

        if mode == "source":
            for item in record.get("items", ()):
                dest = target / item["rel"]
                if item.get("expectedSha"):
                    if not dest.is_file() or sha256_file(dest) != item["expectedSha"]:
                        raise RuntimeError(f"File changed before apply: {item['rel']}")
                elif dest.exists():
                    raise RuntimeError(f"File unexpectedly appeared before apply: {item['rel']}")
            for item in record.get("items", ()):
                dest = target / item["rel"]
                if item.get("action") == "delete":
                    if dest.exists():
                        dest.unlink()
                else:
                    staged = Path(item["staged"])
                    if sha256_file(staged) != item["replacementSha"]:
                        raise RuntimeError(f"Staged file checksum mismatch: {item['rel']}")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(staged, dest)
                    if sha256_file(dest) != item["replacementSha"]:
                        raise RuntimeError(f"Post-copy checksum mismatch: {item['rel']}")
            cleanup_staged(record)
        elif mode == "appimage":
            ps = _load_sibling_patch_server()
            if ps is None:
                raise RuntimeError("patch_server.py missing next to deferred_posix.py")
            applied = ps.apply_now(record)
            record.update(applied)
        else:
            if sha256_file(target) != record["expectedSha"]:
                raise RuntimeError("App changed after preparation; an update was preserved.")
            if sha256_file(pending) != record["replacementSha"]:
                raise RuntimeError("Staged file changed; original app was preserved.")
            orig_mode = stat.S_IMODE(target.stat().st_mode)
            try:
                os.chmod(pending, orig_mode)
            except OSError:
                pass
            replaced = False
            for _ in range(15):
                latest = read_json_file(receipt_path, {})
                cfg = read_json_file(record.get("config", ""), {})
                if latest.get("token") != record.get("token") or cfg.get("requestToken") != record.get("token"):
                    cleanup_staged(record)
                    return 0
                if sha256_file(target) != record["expectedSha"]:
                    raise RuntimeError("App changed before replacement; original file preserved.")
                try:
                    os.replace(pending, target)
                    try:
                        os.chmod(target, orig_mode)
                    except OSError:
                        pass
                    replaced = True
                    break
                except OSError:
                    time.sleep(0.5)
            if not replaced:
                raise RuntimeError("The executable remained busy. Close BeamNG.Earth and try again.")
            if sha256_file(target) != record["replacementSha"]:
                raise RuntimeError("Replacement verification failed; original backup available.")

        if record.get("operation") == "restore":
            record["state"] = "restored"
            record["message"] = "Original BeamNG.Earth restored. Start BeamNG.Earth normally."
        else:
            record["state"] = "installed"
            record["message"] = "Traffic and AutoDrive fixes installed. Start BeamNG.Earth normally."
        save_record(record, receipt_path)

        if record.get("operation") == "restore" and record.get("cleanupOnRestore"):
            for key in ("backup", "sidecar", "installReceipt", "lastTargetFile"):
                val = record.get(key)
                if val and Path(val).is_file():
                    try:
                        Path(val).unlink()
                    except OSError:
                        pass

        if record.get("relaunchAfterSwap") and target.is_file() and os.access(target, os.X_OK):
            subprocess.Popen(
                [str(target)],
                cwd=str(target.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=(os.name != "nt"),
            )
        return 0
    except Exception as exc:
        record["state"] = "error"
        record["message"] = str(exc)
        save_record(record, receipt_path)
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, help="Path to patch-status.json receipt")
    parser.add_argument("--reverify-appimage", action="store_true", help="Re-verify AppImage user_dir mods")
    parser.add_argument("--watch-seconds", type=float, default=15.0)
    parser.add_argument("--timeout-seconds", type=float, default=43200.0)
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    args = parser.parse_args(argv)
    if args.reverify_appimage:
        return reverify_appimage_loop(
            args.receipt,
            watch_seconds=args.watch_seconds,
            poll_interval=args.poll_seconds,
        )
    return run_deferred_swap(
        args.receipt,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
