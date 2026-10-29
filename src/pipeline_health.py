#!/usr/bin/env python3
"""
pipeline_health.py — morning heartbeat for the Supernote → Obsidian pipeline.

Checks the things that silently broke before (services, Syncthing, stuck notes)
and sends ONE Discord summary so the owner can see at a glance it is alive.
Run by a systemd timer each morning. Fully defensive — never raises.

  --now   send the summary regardless (also the default)
  --quiet only ping if something is WRONG (good for a more frequent timer)
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sn_config as mp

VAULT      = mp.vault_root()
RAW        = mp.supernote_raw()
_H         = mp.health()
SERVICES   = list(_H.get("services", []))
ST_CONFIG  = Path(_H.get("syncthing_config", "")).expanduser()
BACKUP     = Path(_H["backup_repo"]).expanduser() if _H.get("backup_repo") else None


def _svc_active(name: str) -> bool:
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", name],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() == "active"
    except Exception:
        return False


def _syncthing():
    """Return (devices_connected, devices_total, folders_ok, folders_total) or None."""
    try:
        root = ET.parse(ST_CONFIG).getroot()
        gui  = root.find("gui")
        addr = gui.find("address").text
        api  = gui.find("apikey").text
        base = f"http://{addr}"

        def get(p):
            req = urllib.request.Request(base + p, headers={"X-API-Key": api})
            return json.load(urllib.request.urlopen(req, timeout=8))

        conns = get("/rest/system/connections")["connections"]
        total = len(conns)
        up    = sum(1 for c in conns.values() if c.get("connected"))
        f_ok = f_tot = 0
        for f in root.findall("folder"):
            f_tot += 1
            try:
                st = get(f"/rest/db/status?folder={f.get('id')}")
                if st.get("needFiles", 0) == 0 and st.get("state") == "idle":
                    f_ok += 1
            except Exception:
                pass
        return up, total, f_ok, f_tot
    except Exception:
        return None


def _note_stats():
    """(.note files, processed, awaiting-format, unprocessed, processed-24h)."""
    notes = list(RAW.glob("*.note")) if RAW.exists() else []
    procs = list(RAW.glob("*.processed")) if RAW.exists() else []
    awaiting = unprocessed = recent = 0
    cutoff = time.time() - 86400
    proc_stems = set()
    for p in procs:
        try:
            d = json.loads(p.read_text())
        except Exception:
            d = {}
        proc_stems.add(p.stem)
        if d.get("awaiting_format"):
            awaiting += 1
        if p.stat().st_mtime >= cutoff:
            recent += 1
    for n in notes:
        if n.stem not in proc_stems:
            unprocessed += 1
    return len(notes), len(procs), awaiting, unprocessed, recent


def _system_stats():
    """(disk_free_pct on /, system-backup age in days) — either may be None."""
    free_pct = age_days = None
    try:
        import shutil
        total, _used, free = shutil.disk_usage("/")
        free_pct = round(100 * free / total)
    except Exception:
        pass
    if BACKUP is None:
        return free_pct, age_days
    try:
        r = subprocess.run(["git", "-C", str(BACKUP), "log", "-1", "--format=%ct"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            age_days = int((time.time() - int(r.stdout.strip())) / 86400)
    except Exception:
        pass
    return free_pct, age_days


def build_report() -> tuple[str, bool]:
    """Return (message, all_ok)."""
    ok = True
    lines = ["**☀️ Pipeline heartbeat**"]

    svc = {s: _svc_active(s) for s in SERVICES}
    if all(svc.values()):
        lines.append("✅ services: all up")
    else:
        ok = False
        down = ", ".join(s for s, a in svc.items() if not a)
        lines.append(f"❌ services DOWN: {down}")

    st = _syncthing()
    if st is None:
        ok = False
        lines.append("⚠️ syncthing: couldn't query")
    else:
        up, tot, f_ok, f_tot = st
        mark = "✅" if (f_ok == f_tot) else "⚠️"
        if f_ok != f_tot:
            ok = False
        lines.append(f"{mark} sync: {up}/{tot} devices · {f_ok}/{f_tot} folders in sync")

    n, p, awaiting, unprocessed, recent = _note_stats()
    lines.append(f"📝 notes: {recent} processed (24h) · {awaiting} awaiting format")
    if unprocessed:
        ok = False
        lines.append(f"⚠️ {unprocessed} note(s) synced but NOT processed — check the watcher")

    free_pct, bage = _system_stats()
    if free_pct is not None:
        mark = "✅" if free_pct > 10 else "⚠️"
        if free_pct <= 10:
            ok = False
        lines.append(f"{mark} disk: {free_pct}% free on /")
    if bage is not None:
        mark = "✅" if bage <= 2 else "⚠️"
        if bage > 2:
            ok = False
        lines.append(f"{mark} system backup: {bage}d old (timer is daily)")

    return "\n".join(lines), ok


def send_discord(msg: str):
    if mp.send_discord(msg) is None:
        print("[health] discord send failed")


def main(argv):
    quiet = "--quiet" in argv
    msg, ok = build_report()
    print(msg)
    if quiet and ok:
        return 0
    send_discord(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
