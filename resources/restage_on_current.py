"""Cooperative single-close restage helper invoked by deferred.ps1 / deferred_posix.py.

When two companion add-ons (e.g. Bridge, Rail & Tunnel Fixes and Traffic and AutoDrive
fixes) stage pending patches during the same BeamNG.Earth session, the first add-on to
acquire the swap lock updates BeamNG.Earth on close (S0 -> S1). The second add-on's
deferred helper sees target SHA S1 != expectedSha S0 and invokes this script to restage
cleanly onto S1 so both add-ons land atomically in a single close cycle.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import sys


def _load_patch_server(resources_dir):
    ps_path = Path(resources_dir) / "patch_server.py"
    spec = importlib.util.spec_from_file_location("_taf_restage_patch_server", ps_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def restage_receipt(receipt_path):
    receipt_path = Path(receipt_path).resolve()
    resources_dir = Path(__file__).resolve().parent
    ps = _load_patch_server(resources_dir)
    prev = ps.read_json(receipt_path, {})
    if not prev:
        raise RuntimeError("Missing receipt for restage")

    cfg_path = Path(prev["config"])
    cfg = ps.read_json(cfg_path, {})
    token = prev.get("token", "")
    if token and cfg.get("requestToken") != token:
        return 0

    operation = prev.get("operation", "install")
    if operation == "install" and not prev.get("standalone"):
        mod_path = cfg.get("modPath")
        if not cfg.get("enabled") or not mod_path or not Path(mod_path).is_file():
            return 0

    ps._cleanup_stale_pending(Path(prev["target"]), prev)
    payload_dir = resources_dir.parent / "payload"
    new_rec = ps.prepare(
        prev["target"],
        cfg_path,
        receipt_path,
        payload_dir=payload_dir if payload_dir.is_dir() else None,
        operation=operation,
        token=token,
        server_pid=0,
        apply_fixes=bool(prev.get("applyFixes", True)),
        enable_reporter=bool(prev.get("enableReporter", False)),
    )

    carry_keys = (
        "standalone",
        "notify",
        "skipReadyNotice",
        "cleanupOnRestore",
        "relaunchAfterSwap",
        "installReceipt",
        "lastTargetFile",
        "completionNotice",
    )
    for k in carry_keys:
        if k in prev:
            new_rec[k] = prev[k]
    new_rec["companionPid"] = 0
    if new_rec.get("sidecar"):
        ps.atomic_json(new_rec["sidecar"], new_rec)
    ps.atomic_json(receipt_path, new_rec)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, help="Path to patch-status.json receipt")
    args, _unknown = parser.parse_known_args(argv)
    return restage_receipt(args.receipt)


if __name__ == "__main__":
    raise SystemExit(main())
