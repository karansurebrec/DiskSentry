"""Physical disk enumeration + SMART attachment with TTL caches.

- Linux: /sys/block + lsblk -J (identity, mountpoints) + smartctl when present.
- Windows: PowerShell Get-PhysicalDisk / Get-Disk / Get-Partition (+ Storage
  Reliability Counters fallback when smartctl is missing).
- Produces normalized disk dicts consumed by the API layer.
"""
import json
import os
import re
import threading
import time

from . import endurance, simulator, smart as smart_mod, store, util

SCAN_TTL = 20          # seconds for the disk list cache
SMART_TTL = 60         # seconds per-drive SMART cache
_lock = threading.RLock()
_cache = {"ts": 0.0, "disks": []}
_smart_cache = {}      # disk_id -> {"ts": float, "smart": dict}
_smart_pending = set()


# ------------------------------------------------------------------ public API
def get_disks(force=False):
    """Return the cached disk list (refreshing if stale). Non-blocking: SMART
    entries may still be filling in the background (smart_pending flag)."""
    with _lock:
        fresh = (time.time() - _cache["ts"]) < SCAN_TTL
        if not force and fresh and _cache["disks"] is not None:
            return _cache["disks"], _smart_pending_copy()
    disks = _enumerate()
    with _lock:
        _cache["ts"] = time.time()
        _cache["disks"] = disks
    return disks, _smart_pending_copy()


def smart_for(disk_id, disk=None):
    """Blocking SMART fetch (used by the detail endpoint)."""
    with _lock:
        hit = _smart_cache.get(disk_id)
        if hit and (time.time() - hit["ts"]) < SMART_TTL:
            return hit["smart"]
    smart = _fetch_smart(disk_id, disk)
    with _lock:
        _smart_cache[disk_id] = {"ts": time.time(), "smart": smart}
        _smart_pending.discard(disk_id)
    return smart


def warm_smart_background(disks):
    """Kick off SMART fetches for disks whose cache is stale/missing."""
    from concurrent.futures import ThreadPoolExecutor
    with _lock:
        stale = [d for d in disks
                 if d["id"] not in _smart_cache
                 or (time.time() - _smart_cache[d["id"]]["ts"]) > SMART_TTL]
    for d in stale:
        with _lock:
            _smart_pending.add(d["id"])
    if not stale:
        return

    def job():
        with ThreadPoolExecutor(max_workers=4) as ex:
            for d in stale:
                ex.submit(smart_for, d["id"], d)

    threading.Thread(target=job, daemon=True, name="smart-warm").start()


def refresh_smart_caches():
    """Drop stale SMART caches (background refresher calls this)."""
    with _lock:
        for did in [k for k, v in _smart_cache.items() if time.time() - v["ts"] > SMART_TTL]:
            _smart_cache.pop(did, None)


def _smart_pending_copy():
    return set(_smart_pending)


def clear_caches():
    with _lock:
        _cache["ts"] = 0.0
        _smart_cache.clear()
        _smart_pending.clear()
    # RESCAN also gets a fresh shot at smartctl (e.g. installed mid-session)
    # and at enclosures previously marked unreadable.
    smart_mod.reset_smartctl_cache()
    smart_mod.clear_negative_hints()


# ------------------------------------------------------------------ enumeration
def _enumerate():
    if util.IS_WIN:
        disks = _windows_disks()
    else:
        disks = _linux_disks()

    # attach whatever SMART we already have cached (non-blocking)
    with _lock:
        for d in disks:
            hit = _smart_cache.get(d["id"])
            if hit:
                d["smart"] = hit["smart"]
                d["smart_pending"] = False
            else:
                d["smart"] = smart_mod.degraded_smart(_hint())
                d["smart_pending"] = True
    for d in disks:
        _apply_health(d)
        endurance.assess(d)
    return disks


def _hint():
    if smart_mod.available():
        return "reading SMART…"
    if util.IS_WIN:
        return ("smartmontools not found — install it for full SMART + TBW "
                "(fallback: PowerShell reliability counters in use)")
    return "smartctl not installed on this host — identity/I/O still live"


def _apply_health(d):
    s = d.get("smart") or {}
    if s.get("passed") is False:
        d["health"] = "CRIT"
    elif d.get("health") in (None, "UNKNOWN") and s.get("passed") is True:
        d["health"] = "OK"
    if d.get("health") == "OK" and any(i.get("sev") in ("warn", "crit") for i in s.get("issues") or []):
        d["health"] = "WARN"


# ------------------------------------------------------------------ Linux
def _linux_disks():
    disks = []
    lsblk = _lsblk_map()
    try:
        names = sorted(os.listdir("/sys/block"))
    except Exception:
        names = []
    for name in names:
        if name.startswith(("loop", "ram", "zram", "dm-", "md", "sr", "pmem", "nbd", "ubiblock", "mtdblock")):
            continue
        base = "/sys/block/" + name
        real = os.path.realpath(base)
        if "/usb" in real and not name.startswith("sd"):
            pass
        sectors = util.to_int(util.read_file(base + "/size"))
        size = (sectors * 512) if sectors else (lsblk.get(name, {}).get("size"))
        rota = util.to_int(util.read_file(base + "/queue/rotational"))
        model = (util.read_file(base + "/device/model")
                 or util.read_file(base + "/device/name")
                 or lsblk.get(name, {}).get("model") or name)
        serial = (util.read_file(base + "/device/serial")
                  or _nvme_serial(name)
                  or lsblk.get(name, {}).get("serial"))
        tran = lsblk.get(name, {}).get("tran")
        if name.startswith("nvme"):
            dtype, bus = "NVMe", "NVMe"
        elif rota == 0:
            dtype, bus = "SSD", (tran or "SATA").upper()
        else:
            dtype, bus = "HDD", (tran or "SATA").upper()
        if not serial:
            serial = None
        disks.append({
            "id": name, "name": (model or name).strip(), "model": (model or "").strip(),
            "family": None, "serial": (serial or "").strip() if serial else None,
            "firmware": (util.read_file(base + "/device/firmware") or None),
            "type": dtype, "bus": bus, "size": size,
            "spindle": None if dtype in ("SSD", "NVMe") else (5400 if rota else None),
            "health": "UNKNOWN", "mountpoints": lsblk.get(name, {}).get("mounts", []),
            "smart": None, "smart_source": "smartctl" if smart_mod.available() else "none",
            "smart_pending": True, "smart_hint": _hint() if not smart_mod.available() else None,
            "demo": False,
        })

    # merge smartctl devices that /sys missed
    if smart_mod.available():
        known = {d["id"] for d in disks}
        for dev in smart_mod.scan_devices():
            k = util.normalize_key(dev["name"])
            if k in known:
                continue
            s = smart_mod.read_smart(dev["name"], dev["type"])
            if not s.get("available"):
                continue
            cap = s.get("capacity")
            rot = s.get("rotation")
            dtype = "NVMe" if s.get("protocol") == "NVMe" else ("SSD" if rot == 0 else ("HDD" if rot else "UNKNOWN"))
            disks.append({
                "id": k, "name": s.get("model") or dev["name"], "model": s.get("model"),
                "family": s.get("family"), "serial": s.get("serial"),
                "firmware": s.get("firmware"), "type": dtype,
                "bus": (dev.get("protocol") or "ata").upper(),
                "size": cap, "spindle": rot or None, "health": "OK" if s.get("passed") else "CRIT",
                "mountpoints": [], "smart": s, "smart_pending": False,
                "smart_source": "smartctl", "smart_hint": None, "demo": False,
            })
    return disks


def _nvme_serial(name):
    for pat in ("/sys/class/nvme/%s/serial" % name,
                "/sys/class/nvme/%s/model" % name):
        v = util.read_file(pat)
        if pat.endswith("serial") and v:
            return v
    return None


def _lsblk_map():
    """lsblk -J -> {devname: {model, serial, size, tran, mounts: []}}"""
    rc, out, _ = util.run_cmd(
        ["lsblk", "-J", "-o", "NAME,TYPE,SIZE,MODEL,SERIAL,TRAN,MOUNTPOINTS"], timeout=10)
    if rc != 0 or not out:
        return {}
    try:
        data = json.loads(out)
    except Exception:
        return {}
    m = {}

    def walk(node):
        if node.get("type") == "disk":
            entry = {
                "model": (node.get("model") or "").strip() or None,
                "serial": (node.get("serial") or "").strip() or None,
                "size": util.parse_size(node.get("size")),
                "tran": (node.get("tran") or "").strip().lower() or None,
                "mounts": [],
            }
            m[node.get("name")] = entry
            for ch in node.get("children") or []:
                for mp in ch.get("mountpoints") or []:
                    if mp:
                        entry["mounts"].append(mp)
        else:
            for ch in node.get("children") or []:
                walk(ch)

    for top in data.get("blockdevices") or []:
        walk(top)
    return m


# ------------------------------------------------------------------ Windows
def _windows_disks():
    pd = util.powershell_json(
        "Get-PhysicalDisk | Select-Object DeviceId, FriendlyName, SerialNumber, "
        "@{n='MediaType';e={[string]$_.MediaType}}, @{n='BusType';e={[string]$_.BusType}}, "
        "@{n='Health';e={[string]$_.HealthStatus}}, SpindleSpeed, Size")
    fw = {}
    gd = util.powershell_json(
        "Get-Disk | Select-Object Number, @{n='Firmware';e={[string]$_.FirmwareVersion}}")
    if gd:
        if isinstance(gd, dict):
            gd = [gd]
        for r in gd:
            n = util.to_int(r.get("Number"))
            if n is not None and r.get("Firmware"):
                fw[n] = r.get("Firmware")
    parts = util.powershell_json(
        "Get-Partition | Select-Object DiskNumber, DriveLetter")
    mounts = {}
    if parts:
        if isinstance(parts, dict):
            parts = [parts]
        for r in parts:
            dn = util.to_int(r.get("DiskNumber"))
            letter = r.get("DriveLetter")
            if dn is None or not letter:
                continue
            mounts.setdefault(dn, [])
            drv = str(letter).strip()
            if drv and drv not in mounts[dn]:
                mounts[dn].append(drv[0].upper() + ":")

    if not pd:
        return []
    if isinstance(pd, dict):
        pd = [pd]
    disks = []
    for r in pd:
        num = util.to_int(r.get("DeviceId"))
        if num is None:
            continue
        media = (r.get("MediaType") or "").strip()
        bus = (r.get("BusType") or "").strip()
        size = util.to_int(r.get("Size"))
        spin = util.to_int(r.get("SpindleSpeed"))
        dtype = "SSD" if "SSD" in media else ("HDD" if "HDD" in media or "Hard" in media else "UNKNOWN")
        if bus.upper() == "NVME":
            dtype = "NVMe"
        elif dtype == "UNKNOWN":
            # USB enclosures routinely report MediaType 'Unspecified'. External
            # spinning disks dominate, and a successful smartctl read refines
            # this from ATA rotation data later (_enrich_from_smart).
            if bus.upper() in ("USB", "1394"):
                dtype = "HDD"
            elif spin and spin > 0:
                dtype = "HDD"
        health = (r.get("Health") or "").strip().lower()
        hmap = {"healthy": "OK", "warning": "WARN", "unhealthy": "CRIT"}
        did = "pd:%d" % num
        serial = (r.get("SerialNumber") or "").strip() or None
        d = {
            "id": did, "name": (r.get("FriendlyName") or "PhysicalDrive%d" % num),
            "model": (r.get("FriendlyName") or "").strip(), "family": None,
            "serial": serial, "firmware": fw.get(num), "type": dtype,
            "bus": bus.upper() or None, "size": size,
            "spindle": spin if spin and spin > 0 else None,
            "health": hmap.get(health, "UNKNOWN"),
            "mountpoints": sorted(mounts.get(num, [])),
            "smart": None, "smart_pending": True,
            "smart_source": "smartctl" if smart_mod.available() else "powershell",
            "smart_hint": _hint() if not smart_mod.available() else None,
            "demo": False,
        }
        disks.append(d)

    if smart_mod.available():
        _match_smartctl_windows(disks)
    return disks


def _match_smartctl_windows(disks):
    """Attach _smartctl_name/_smartctl_type to PowerShell-enumerated disks.

    Three layers, most reliable first:
      1. direct key match — scan name normalizes to our pd:N id
         (e.g. '/dev/pd0' -> pd:0), or scan info_name mentions PhysicalDriveN
      2. serial match — probe each scanned device's identity (smartctl -i)
         and match against the PowerShell serial. This is the only fully
         reliable key across sources: PowerShell formats NVMe serials like
         '6479_A78A_6F00_09F6.' while smartctl reports '6479A78A6F0009F6',
         so both are normalized before comparing.
      3. model containment — normalized model of one side contained in the
         other (>= 6 chars), capacity equal within 1%.
    """
    devs = smart_mod.scan_devices()
    if not devs:
        return

    # ---- layer 1: key / PhysicalDrive-number match
    by_key = {}
    for dev in devs:
        if dev.get("key"):
            by_key.setdefault(dev["key"], dev)
        if dev.get("pd") is not None:
            by_key.setdefault("pd:%d" % dev["pd"], dev)
    unmatched, used = [], set()
    for d in disks:
        dev = by_key.get(d["id"])
        if dev:
            d["_smartctl_name"] = dev["name"]
            d["_smartctl_type"] = dev.get("type") or ""
            used.add(dev["name"])
        else:
            unmatched.append(d)

    if not unmatched:
        return

    # ---- layer 2 + 3: identity probes on the remaining devices
    pending = [dev for dev in devs if dev["name"] not in used]
    idents = {}
    for dev in pending:
        ident = smart_mod.probe_identity(dev["name"], dev.get("type") or "")
        if ident:
            idents[dev["name"]] = ident
    for d in unmatched:
        want_serial = util.norm_serial(d.get("serial"))
        want_model = util.norm_serial(d.get("model") or d.get("name"))
        got = None
        # serial first
        if want_serial:
            for name, ident in idents.items():
                if util.norm_serial(ident.get("serial")) == want_serial:
                    got = name
                    break
        # model containment + capacity as fallback
        if got is None and want_model and len(want_model) >= 6:
            for name, ident in idents.items():
                have_model = util.norm_serial(ident.get("model"))
                if not have_model:
                    continue
                model_ok = (have_model in want_model or want_model in have_model)
                cap_ok = (ident.get("capacity") and d.get("size")
                          and abs(ident["capacity"] - d["size"]) <= d["size"] * 0.01)
                if model_ok and (cap_ok or not ident.get("capacity")):
                    got = name
                    break
        if got is not None:
            dev = next(x for x in devs if x["name"] == got)
            d["_smartctl_name"] = dev["name"]
            d["_smartctl_type"] = dev.get("type") or ""
            idents.pop(got, None)  # one disk per device


# ------------------------------------------------------------------ SMART fetch
def _fetch_smart(disk_id, disk):
    if disk and disk.get("demo"):
        return simulator.SIM_DRIVES[disk_id].smart()
    # sim ids
    if disk_id.startswith("sim"):
        drive = simulator.SIM_DRIVES.get(disk_id)
        if drive:
            return drive.smart()
        return smart_mod.degraded_smart()
    if smart_mod.available():
        name = None
        dtype = ""
        # stable key: serial survives replug-order changes of pd:N
        key = hint = None
        if disk_id.startswith("pd:") and disk:
            key = util.norm_serial(disk.get("serial")) or disk_id
            hint = smart_mod.bridge_hint(key)
        # a known probe outcome (working dialect OR unreadable enclosure) makes
        # the generic scan-mapped attempt pointless — skip straight to it
        skip_generic = hint is not None
        if disk and not skip_generic:
            name = disk.get("_smartctl_name")
            dtype = disk.get("_smartctl_type") or ""
        if not name and not skip_generic:
            for dev in smart_mod.scan_devices():
                if util.normalize_key(dev["name"]) == disk_id:
                    name, dtype = dev["name"], dev["type"]
                    break
        if name:
            s = smart_mod.read_smart(name, dtype)
            if s.get("available"):
                _enrich_from_smart(disk, s)
                return s
        # brute-force candidates for PowerShell-style ids (pd:N).
        # smartctl's native Windows names are /dev/pdN and /dev/nvmeN —
        # '\\.\PhysicalDriveN' is NOT understood by smartctl, so it comes last.
        if disk_id.startswith("pd:"):
            num = disk_id.split(":", 1)[1]
            if not key:
                key = util.norm_serial((disk or {}).get("serial")) or disk_id
            want_serial = util.norm_serial((disk or {}).get("serial"))
            usb_bus = bool(disk) and (disk.get("bus") or "").upper() in ("USB", "1394")

            def _try(cand, ct, timeout=25):
                s = smart_mod.read_smart(cand, ct, timeout=timeout)
                if not s.get("available"):
                    return None
                got = util.norm_serial(s.get("serial"))
                if want_serial and got and got != want_serial:
                    return None  # hit a different physical drive
                return s

            # negative hint = enclosure already probed, no dialect works -> skip
            if smart_mod.bridge_hint(key) is not False:
                base = "/dev/pd%s" % num
                cands = []
                if hint and hint is not False:
                    cands.append((base, hint))       # persisted dialect first
                if disk and (disk.get("bus") or "").upper() == "NVME":
                    cands += [("/dev/nvme%d" % i, "nvme") for i in range(4)]
                cands += [(base, ""), ("pd%s" % num, ""),
                          ("\\\\.\\PhysicalDrive%s" % num, "")]
                for cand, ct in cands:
                    s = _try(cand, ct)
                    if s:
                        if usb_bus:
                            smart_mod.set_bridge_hint(key, ct or "auto")
                        _enrich_from_smart(disk, s)
                        return s
                # USB enclosures: probe SAT + vendor pass-through dialects
                # (Innostor/JMicron/Cypress/Sunplus bridges ignore auto-detect).
                if usb_bus:
                    s, _dialect = smart_mod.read_smart_probed(
                        base, key=key, remember_negative=util.is_admin())
                    if s.get("available"):
                        _enrich_from_smart(disk, s)
                        return s
        elif not disk_id.startswith("pd:"):
            s = smart_mod.read_smart(disk_id, "")
            if s.get("available"):
                _enrich_from_smart(disk, s)
                return s
    # PowerShell reliability fallback
    if util.IS_WIN:
        rel = smart_mod.powershell_reliability()
        r = rel.get(disk_id)
        reason, advice, usb = _fallback_note(disk)
        if r:
            s = smart_mod.degraded_smart(reason)
            s["source"] = "powershell"
            s["device"] = disk_id
            s["fallback_advice"] = advice
            s["usb_bridge"] = usb
            s["temperature"] = r.get("temperature")
            s["temp_source"] = "reliability counter"
            s["power_on_hours"] = r.get("power_on_hours")
            s["power_cycles"] = r.get("start_stop_cycles")
            s["attributes"] = [
                {"id": 1001, "id_hex": "—", "name": "Wear (%)", "value": r.get("wear"),
                 "worst": None, "thresh": None, "raw_value": r.get("wear"),
                 "raw_string": str(r.get("wear") if r.get("wear") is not None else "—"),
                 "status": "warn" if (r.get("wear") or 0) >= 90 else "ok", "flagged": True},
                {"id": 1002, "id_hex": "—", "name": "Read Errors Total", "value": None,
                 "worst": None, "thresh": None, "raw_value": r.get("read_errors_total"),
                 "raw_string": str(r.get("read_errors_total") if r.get("read_errors_total") is not None else "—"),
                 "status": "ok", "flagged": False},
                {"id": 1003, "id_hex": "—", "name": "Write Errors Total", "value": None,
                 "worst": None, "thresh": None, "raw_value": r.get("write_errors_total"),
                 "raw_string": str(r.get("write_errors_total") if r.get("write_errors_total") is not None else "—"),
                 "status": "ok", "flagged": False},
            ]
            if r.get("temperature") and r["temperature"] > 60:
                s["issues"].append({"sev": "warn", "text": "Temperature %d°C" % r["temperature"]})
            return s
        # no reliability counters either — still explain precisely why
        s = smart_mod.degraded_smart(reason)
        s["fallback_advice"] = advice
        s["usb_bridge"] = usb
        return s
    return smart_mod.degraded_smart(_hint())


def _fallback_note(disk):
    """Context-aware (reason, advice, usb_flag) for degraded SMART payloads.

    The old blanket message told everyone to 'install smartmontools + run as
    admin' even when both were already true — the usual cause for external
    USB drives is the enclosure's bridge chip, which deserves its own story.
    """
    if not smart_mod.available():
        return (
            "smartmontools (smartctl) not installed — PowerShell reliability counters in use",
            "Install smartmontools (winget install smartmontools), run DiskSentry as "
            "Administrator, then press RESCAN for the full attribute table and TBW.",
            False,
        )
    if not util.is_admin():
        return (
            "smartctl found but DiskSentry is not elevated — PowerShell reliability counters in use",
            "Relaunch DiskSentry as Administrator (right-click the exe > Run as administrator) "
            "so smartctl can open the raw device handles.",
            False,
        )
    if disk is not None and (disk.get("bus") or "").upper() in ("USB", "1394"):
        return (
            "USB enclosure does not expose SMART to smartctl — PowerShell reliability counters in use",
            "This drive sits behind a USB bridge chip that does not forward SMART commands "
            "(SAT and vendor pass-through dialects were all probed automatically). That is a "
            "hardware limitation of some external enclosures: live read/write monitoring, "
            "identity and capacity still work, but the raw SMART attribute table and exact "
            "TBW are unavailable. Plugging the bare drive into a SATA/USB dock with SAT "
            "support usually unlocks full SMART.",
            True,
        )
    return (
        "smartctl could not read this drive — PowerShell reliability counters in use",
        "Run DiskSentry as Administrator, make sure no other tool holds exclusive access to "
        "the drive, then press RESCAN.",
        False,
    )


def _enrich_from_smart(disk, s):
    """Copy identity facts from a successful smartctl read onto the disk dict."""
    if not disk:
        return
    if not disk.get("serial") and s.get("serial"):
        disk["serial"] = s["serial"]
    if s.get("capacity") and not disk.get("size"):
        disk["size"] = s["capacity"]
    if s.get("protocol") == "NVMe":
        disk["type"] = "NVMe"
        disk["bus"] = disk.get("bus") or "NVMe"
    elif s.get("rotation") is not None:
        # ATA rotation data is authoritative — also corrects the USB 'HDD' guess
        # when the drive behind the bridge is actually an SSD.
        disk["type"] = "SSD" if s["rotation"] == 0 else "HDD"


# ------------------------------------------------------------------ SIM mode
def sim_disks():
    disks = [d.disk() for d in simulator.SIM_DRIVES.values()]
    for d in disks:
        endurance.assess(d)
    return disks


def rescan_events():
    return []
