"""SMART acquisition: smartctl (JSON) primary, PowerShell StorageReliabilityCounter fallback.

Normalized smart dict shape:
{
  available, source, passed, model, family, serial, firmware, capacity, rotation, protocol,
  temperature, temp_source, power_on_hours, power_cycles, unsafe_shutdowns,
  critical_warning, available_spare, percentage_used, media_errors,
  tbw: {written_bytes, read_bytes, attr, written_raw},
  attributes: [{id, id_hex, name, value, worst, thresh, raw_string, raw_value, status, flagged}],
  nvme_log: {...}, error_log_count, self_test,
  issues: [{sev, text}]
}
"""
import re

from . import util

# SATA attributes whose raw value > 0 is worth flagging as an early warning.
FLAG_RAW_GT0 = {5, 187, 196, 197, 198, 199}
# ids considered "endurance relevant"
ENDURANCE_IDS = {231, 233, 177, 241, 242, 246}
ATTR_NAMES_FALLBACK = {
    5: "Reallocated Sector Count", 9: "Power-On Hours", 12: "Power Cycle Count",
    187: "Reported Uncorrectable Errors", 190: "Airflow Temperature",
    194: "Temperature Celsius", 196: "Reallocated Event Count",
    197: "Current Pending Sectors", 198: "Offline Uncorrectable",
    199: "UDMA CRC Error Count", 231: "SSD Life Left", 232: "Endurance Remaining",
    233: "Media Wear Indicator / NAND Writes", 177: "Wear Leveling Count",
    241: "Total LBAs Written", 242: "Total LBAs Read", 246: "Total LBAs Written (alt)",
}

SMARTCTL = None
_checked = False

# ---- USB / external-enclosure bridge dialects ---------------------------------
# Auto-detection fails on many USB-SATA/USB-UASP bridge chips (Innostor,
# JMicron, Cypress, Sunplus, ...): they only forward SMART commands in one
# specific dialect. We probe them in order of observed hit rate — 'sat'
# (SCSI ATA Translation, 16-byte CDB first) covers most modern bridges, the
# vendor-specific entries catch the rest.
USB_BRIDGE_TYPES = ("sat", "sat,12", "sat,16", "usbjmicron", "usbcypress", "usbsunplus")

_BRIDGE_HINTS = {}      # stable key (drive serial) -> working dialect | False (= unreadable enclosure)
_BRIDGE_LOADED = False


def _ensure_bridge_loaded():
    """Hydrate persisted bridge dialect hints from the config (once)."""
    global _BRIDGE_LOADED
    if _BRIDGE_LOADED:
        return
    _BRIDGE_LOADED = True
    try:
        from . import store
        hints = store.load_config().get("smart_bridges") or {}
        if isinstance(hints, dict):
            _BRIDGE_HINTS.update(hints)
    except Exception:
        pass


def bridge_hint(key):
    """Known-working dialect for this drive (string), False when the enclosure
    was probed and found unreadable, None when never probed."""
    if not key:
        return None
    _ensure_bridge_loaded()
    return _BRIDGE_HINTS.get(key)


def set_bridge_hint(key, dialect):
    """Remember (and persist) a probe outcome: dialect string, 'auto', or False."""
    if not key:
        return
    _ensure_bridge_loaded()
    if _BRIDGE_HINTS.get(key) == dialect:
        return  # unchanged — spare the config disk write on refresh cycles
    _BRIDGE_HINTS[key] = dialect
    try:
        from . import store
        store.save_bridge_hints({key: dialect})
    except Exception:
        pass


def clear_negative_hints():
    """Forget 'unreadable enclosure' verdicts (RESCAN, elevation change) so the
    dialect probe runs again."""
    _ensure_bridge_loaded()
    stale = [k for k, v in _BRIDGE_HINTS.items() if v is False]
    for k in stale:
        _BRIDGE_HINTS.pop(k, None)
    if stale:
        try:
            from . import store
            store.drop_bridge_hints(stale)
        except Exception:
            pass


def reset_smartctl_cache():
    """Forget the cached smartctl location (RESCAN after installing smartmontools)."""
    global SMARTCTL, _checked
    SMARTCTL = None
    _checked = False


def smartctl_path():
    global SMARTCTL, _checked
    if not _checked:
        _checked = True
        SMARTCTL = util.find_smartctl()
    return SMARTCTL


def available():
    return smartctl_path() is not None


def scan_devices():
    """smartctl --scan --json=c -> [{name, type, protocol, key, pd, info}]

    Real smartctl JSON puts the array under 'device_list' (7.x); some builds
    emit 'devices'. If JSON yields nothing we also parse the plain-text scan
    table (works on any smartctl version, incl. pre-JSON 6.x).
    """
    exe = smartctl_path()
    if not exe:
        return []
    rc, out, _ = util.run_cmd([exe, "--scan", "--json=c"], timeout=15)
    devs = []
    if rc == 0 and out:
        try:
            import json
            data = json.loads(out)
        except Exception:
            data = {}
        for d in (data.get("device_list") or data.get("devices") or []):
            name = d.get("name")
            if not name:
                continue
            info = d.get("info_name") or ""
            devs.append({
                "name": name,
                "type": d.get("type") or "",
                "protocol": (d.get("protocol") or "").lower() or None,
                "key": util.normalize_key(name),
                "pd": _physicaldrive_no(info) if info else None,
                "info": info,
            })
    if not devs:
        # text fallback:  /dev/pd0 -d ata # "\\.\PhysicalDrive0" "MODEL" "FW"
        rc, out, _ = util.run_cmd([exe, "--scan"], timeout=15)
        if rc == 0 and out:
            for line in out.splitlines():
                m = re.match(r"^\s*(\S+)\s+-d\s+(\S+)\s*(?:#\s*(.*))?", line)
                if not m:
                    continue
                name, dtype, comment = m.group(1), m.group(2), m.group(3) or ""
                devs.append({
                    "name": name,
                    "type": dtype,
                    "protocol": "nvme" if dtype == "nvme" else None,
                    "key": util.normalize_key(name),
                    "pd": _physicaldrive_no(comment),
                    "info": comment,
                })
    return devs


def _physicaldrive_no(text):
    """Extract N from any 'PhysicalDriveN' occurrence (any backslash style)."""
    if not text:
        return None
    m = re.search(r"PhysicalDrive(\d+)", str(text))
    return int(m.group(1)) if m else None


def probe_identity(device_name, dtype=""):
    """Quick `smartctl -i` identity probe -> {model, serial, firmware, capacity,
    protocol} or None. Used to map scanned devices to platform-enumerated disks
    by serial number (the only reliable cross-source key on Windows)."""
    exe = smartctl_path()
    if not exe or not device_name:
        return None
    cmd = [exe, "-i", "--json=c"]
    if dtype and dtype != "auto":
        cmd += ["-d", dtype]
    cmd.append(device_name)
    rc, out, _ = util.run_cmd(cmd, timeout=15)
    if not out:
        return None
    try:
        import json
        d = json.loads(out)
    except Exception:
        return None
    ident = {
        "model": d.get("model_name") or d.get("model"),
        "serial": d.get("serial_number"),
        "firmware": d.get("firmware_version"),
        "capacity": ((d.get("user_capacity") or {}).get("bytes")),
    }
    proto = ((d.get("device") or {}).get("type") or "").lower()
    ident["protocol"] = ("NVMe" if "nvme" in proto else
                         "ATA" if proto in ("ata", "sata", "sat") else
                         proto.upper() if proto else None)
    if not ident["model"] and not ident["serial"]:
        return None
    return ident


def read_smart(device_name, dtype="", timeout=25):
    """Run smartctl -a --json=c on one device -> normalized dict.

    Crucially: smartctl with --json=c ALWAYS emits JSON, even when the device
    could not be opened / the type was not detected. We therefore gate
    'available' on the payload actually containing SMART or identity data;
    otherwise we return available:False carrying smartctl's own error
    message so the UI shows the real reason and the PowerShell fallback runs.
    """
    exe = smartctl_path()
    if not exe or not device_name:
        return {"available": False, "source": "none"}
    cmd = [exe, "-a", "--json=c"]
    if dtype and dtype != "auto":
        cmd += ["-d", dtype]
    cmd.append(device_name)
    rc, out, _ = util.run_cmd(cmd, timeout=timeout)
    if not out:
        return {"available": False, "source": "smartctl",
                "device": device_name,
                "issues": [{"sev": "warn", "text": "smartctl produced no output for %s" % device_name}]}
    try:
        import json
        raw = json.loads(out)
    except Exception:
        return {"available": False, "source": "smartctl",
                "device": device_name,
                "issues": [{"sev": "warn", "text": "smartctl output for %s was not valid JSON" % device_name}]}

    smart = _normalize_smartctl(raw)
    smart["device"] = device_name
    if _payload_empty(raw, smart):
        reason = _smartctl_reason(raw) or "smartctl could not read this device"
        return {
            "available": False, "source": "smartctl", "device": device_name,
            "passed": None, "issues": [{"sev": "warn", "text": "smartctl: %s" % reason}],
        }
    return smart


def read_smart_probed(device_name, key=None, remember_negative=True):
    """Probe one device across USB bridge dialects; first real payload wins.

    Used for external USB/1394 enclosures where smartctl's auto-detection
    fails: the bridge chip only forwards SMART in one dialect (SAT 16/12-byte
    CDB or a vendor protocol). A persisted hint (by drive serial) makes later
    refreshes a single fast call; exhausted probes are remembered as False so
    we do not hammer the bus with ~7 subprocesses every refresh cycle.

    Returns (smart_dict, dialect_that_worked_or_None).
    """
    _ensure_bridge_loaded()
    hint = _BRIDGE_HINTS.get(key) if key else None
    order = []
    if hint and hint is not False:
        order.append(hint)
    order += [t for t in (("",) + USB_BRIDGE_TYPES) if t not in order]
    for t in order:
        s = read_smart(device_name, t, timeout=10)
        if s.get("available"):
            if key:
                set_bridge_hint(key, t or "auto")
            return s, (t or "auto")
    if key and remember_negative:
        set_bridge_hint(key, False)
    return {
        "available": False, "source": "smartctl", "device": device_name,
        "issues": [{"sev": "warn", "text":
                    "smartctl could not read this device through any USB bridge "
                    "dialect (SAT 16/12, JMicron, Cypress, Sunplus)"}],
    }, None


def _payload_empty(raw, smart):
    """True when the parsed JSON carries no identity / health content at all."""
    if smart["model"] or smart["serial"] or smart["attributes"]:
        return False
    if smart["nvme_log"] or smart["passed"] is not None:
        return False
    if smart["capacity"] or smart["temperature"] is not None:
        return False
    if smart["power_on_hours"] is not None:
        return False
    return True


def _smartctl_reason(raw):
    """Join smartctl's own error messages (JSON 'messages' array)."""
    msgs = ((raw.get("smartctl") or {}).get("messages") or [])
    texts = [str(m.get("string") or m.get("text") or "").strip()
             for m in msgs if isinstance(m, dict)]
    texts = [t for t in texts if t]
    return " · ".join(texts[:3]) if texts else None


def _normalize_smartctl(d):
    import json
    s = {
        "available": True, "source": "smartctl",
        "passed": None, "model": None, "family": None, "serial": None, "firmware": None,
        "capacity": None, "rotation": None, "protocol": None,
        "temperature": None, "temp_source": None, "power_on_hours": None,
        "power_cycles": None, "unsafe_shutdowns": None,
        "critical_warning": None, "available_spare": None, "percentage_used": None,
        "media_errors": None,
        "tbw": {"written_bytes": None, "read_bytes": None, "attr": None, "written_raw": None},
        "attributes": [], "nvme_log": None, "error_log_count": None, "self_test": None,
        "issues": [],
    }

    s["model"] = d.get("model_name") or d.get("model")
    s["family"] = d.get("model_family")
    s["serial"] = d.get("serial_number")
    s["firmware"] = d.get("firmware_version")
    s["capacity"] = ((d.get("user_capacity") or {}).get("bytes"))
    s["rotation"] = util.to_int(d.get("rotation_speed"), None)
    proto = ((d.get("device") or {}).get("type") or "").lower()
    if d.get("nvme_smart_health_information_log") or "nvme" in proto:
        s["protocol"] = "NVMe"
    elif proto in ("ata", "sata"):
        s["protocol"] = "ATA"
    elif proto:
        s["protocol"] = proto.upper()

    st = d.get("smart_status") or {}
    if "passed" in st:
        s["passed"] = bool(st["passed"])

    # ---- temperature
    temp = d.get("temperature") or {}
    if util.to_int(temp.get("current")) is not None:
        s["temperature"] = util.to_int(temp.get("current"))
        s["temp_source"] = "smartctl"
    else:
        for key in ("ata_sct_temperature_current", "ata_sct_temperature_min", "current"):
            v = util.to_int(d.get(key))
            if v is not None and 0 < v < 120:
                s["temperature"] = v
                s["temp_source"] = "sct"
                break

    # ---- NVMe health log
    nvme = d.get("nvme_smart_health_information_log")
    if isinstance(nvme, dict):
        log = dict(nvme)
        s["nvme_log"] = {
            "critical_warning": util.to_int(nvme.get("critical_warning")),
            "temperature": util.to_int(nvme.get("temperature")),
            "available_spare": util.to_int(nvme.get("available_spare")),
            "available_spare_threshold": util.to_int(nvme.get("available_spare_threshold")),
            "percentage_used": util.to_int(nvme.get("percentage_used")),
            "data_units_read": util.to_int(nvme.get("data_units_read")),
            "data_units_written": util.to_int(nvme.get("data_units_written")),
            "host_reads": util.to_int(nvme.get("host_read_commands")),
            "host_writes": util.to_int(nvme.get("host_write_commands")),
            "power_cycles": util.to_int(nvme.get("power_cycles")),
            "power_on_hours": util.to_int(nvme.get("power_on_hours")),
            "unsafe_shutdowns": util.to_int(nvme.get("unsafe_shutdowns")),
            "media_errors": util.to_int(nvme.get("media_errors")),
        }
        L = s["nvme_log"]
        s["critical_warning"] = L["critical_warning"]
        s["available_spare"] = L["available_spare"]
        s["percentage_used"] = L["percentage_used"]
        s["media_errors"] = L["media_errors"]
        s["power_cycles"] = L["power_cycles"]
        s["power_on_hours"] = L["power_on_hours"]
        s["unsafe_shutdowns"] = L["unsafe_shutdowns"]
        duw = L["data_units_written"]
        dur = L["data_units_read"]
        if duw is not None:
            s["tbw"]["written_bytes"] = duw * 512000
            s["tbw"]["attr"] = "nvme data_units_written"
            s["tbw"]["written_raw"] = "%d units" % duw
        if dur is not None:
            s["tbw"]["read_bytes"] = dur * 512000
        if s["temperature"] is None and L["temperature"]:
            s["temperature"] = L["temperature"]
            s["temp_source"] = "nvme log"
        if L["critical_warning"]:
            s["issues"].append({"sev": "crit", "text": "NVMe critical warning flag set (%d)" % L["critical_warning"]})
        if (L["available_spare"] or 100) < (L["available_spare_threshold"] or 10):
            s["issues"].append({"sev": "crit", "text": "Available spare below threshold"})
        if (L["media_errors"] or 0) > 0:
            s["issues"].append({"sev": "warn", "text": "%d media/data integrity errors" % L["media_errors"]})

    # ---- ATA attribute table
    table = (((d.get("ata_smart_attributes") or {}).get("table")) or [])
    if table:
        for a in table:
            aid = util.to_int(a.get("id"))
            if aid is None:
                continue
            rawv = a.get("raw") or {}
            raw_val = util.to_int(rawv.get("value"))
            raw_str = rawv.get("string") or (str(raw_val) if raw_val is not None else "")
            if raw_val is None:
                digits = re.findall(r"\d+", str(raw_str))
                raw_val = util.to_int(digits[0]) if digits else None
            value = util.to_int(a.get("value"))
            worst = util.to_int(a.get("worst"))
            thresh = util.to_int(a.get("thresh"))
            name = a.get("name") or ATTR_NAMES_FALLBACK.get(aid, "Attribute %d" % aid)
            status = "ok"
            if value is not None and thresh is not None and value < thresh:
                status = "fail"
            elif aid in FLAG_RAW_GT0 and (raw_val or 0) > 0:
                status = "warn"
            s["attributes"].append({
                "id": aid, "id_hex": "0x%02X" % aid, "name": name,
                "value": value, "worst": worst, "thresh": thresh,
                "raw_value": raw_val, "raw_string": raw_str, "status": status,
                "flagged": aid in FLAG_RAW_GT0 or aid in ENDURANCE_IDS,
            })
        s["attributes"].sort(key=lambda x: x["id"])
        byid = {a["id"]: a for a in s["attributes"]}

        for aid, sev in ((5, "warn"), (197, "warn"), (198, "warn"), (187, "warn"), (199, "warn")):
            a = byid.get(aid)
            if a and (a["raw_value"] or 0) > 0:
                s["issues"].append({"sev": sev, "text": "%s raw = %s" % (a["name"], a["raw_string"])})

        if s["power_on_hours"] is None and byid.get(9):
            s["power_on_hours"] = byid[9]["raw_value"]
        if s["power_cycles"] is None and byid.get(12):
            s["power_cycles"] = byid[12]["raw_value"]
        if s["temperature"] is None and byid.get(194):
            v = byid[194]["raw_value"]
            if v is not None and 0 < v < 120:
                s["temperature"] = v
                s["temp_source"] = "attr 194"
        elif s["temperature"] is None and byid.get(190):
            v = byid[190]["raw_value"]
            if v is not None and 0 < v < 120:
                s["temperature"] = v
                s["temp_source"] = "attr 190"

        # TBW: prefer 246 then 241 (x512B sectors)
        for aid in (246, 241):
            a = byid.get(aid)
            if a and a["raw_value"]:
                s["tbw"]["written_bytes"] = a["raw_value"] * 512
                s["tbw"]["attr"] = "attr %d (%s)" % (aid, a["name"])
                s["tbw"]["written_raw"] = a["raw_string"]
                break
        a242 = byid.get(242)
        if a242 and a242["raw_value"] and s["tbw"]["read_bytes"] is None:
            s["tbw"]["read_bytes"] = a242["raw_value"] * 512
        # life left: 231 (value normalized) or 233
        for aid in (231, 232, 233):
            a = byid.get(aid)
            if a and a["value"] is not None and s["percentage_used"] is None:
                if aid in (231, 232):
                    s["percentage_used"] = max(0, min(100, 100 - a["value"]))
                break

    err = (d.get("ata_smart_error_log") or {}).get("summary") or {}
    s["error_log_count"] = util.to_int(err.get("count"))
    stst = (d.get("ata_smart_self_test_log") or {}).get("standard") or {}
    if stst:
        s["self_test"] = {
            "status": ((stst.get("table") or [{}])[0].get("status") or {}).get("string")
                      if stst.get("table") else None,
        }
    if (s["error_log_count"] or 0) > 0:
        s["issues"].append({"sev": "warn", "text": "SMART error log: %d entries" % s["error_log_count"]})
    return s


def powershell_reliability():
    """Windows fallback: Get-StorageReliabilityCounter per physical disk (needs admin)."""
    if not util.IS_WIN:
        return {}
    rows = util.powershell_json(
        "Get-PhysicalDisk | Get-StorageReliabilityCounter | Select-Object "
        "DeviceId, Wear, Temperature, ReadErrorsTotal, ReadErrorsUncorrected, "
        "WriteErrorsTotal, WriteErrorsUncorrected, PowerOnHours, StartStopCycleCount"
    )
    out = {}
    if not rows:
        return out
    if isinstance(rows, dict):
        rows = [rows]
    for r in rows:
        num = util.to_int(r.get("DeviceId"))
        if num is None:
            continue
        temp = util.to_int(r.get("Temperature"))
        if temp is not None and temp > 100:  # some drivers report Kelvin-ish
            temp = temp - 273
        out["pd:%d" % num] = {
            "wear": util.to_int(r.get("Wear")),
            "temperature": temp,
            "power_on_hours": util.to_int(r.get("PowerOnHours")),
            "read_errors_total": util.to_int(r.get("ReadErrorsTotal")),
            "read_errors_uncorrected": util.to_int(r.get("ReadErrorsUncorrected")),
            "write_errors_total": util.to_int(r.get("WriteErrorsTotal")),
            "write_errors_uncorrected": util.to_int(r.get("WriteErrorsUncorrected")),
            "start_stop_cycles": util.to_int(r.get("StartStopCycleCount")),
        }
    return out


def degraded_smart(reason="smartmontools not found"):
    return {
        "available": False, "source": "none", "passed": None,
        "model": None, "family": None, "serial": None, "firmware": None,
        "capacity": None, "rotation": None, "protocol": None,
        "temperature": None, "temp_source": None, "power_on_hours": None,
        "power_cycles": None, "unsafe_shutdowns": None,
        "critical_warning": None, "available_spare": None, "percentage_used": None,
        "media_errors": None, "tbw": {"written_bytes": None, "read_bytes": None, "attr": None, "written_raw": None},
        "attributes": [], "nvme_log": None, "error_log_count": None, "self_test": None,
        "issues": [{"sev": "info", "text": reason}],
    }
