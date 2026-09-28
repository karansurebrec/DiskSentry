#!/usr/bin/env python3
"""Offline e2e test of the DiskSentry USB-enclosure SMART path (v1.0.2).

Emulates the user's exact machine state behind the warning they reported:
    'Innostor Ext. HDD' 320 GB USB drive, S/N 20240906, PowerShell says
    HEALTHY, smartctl auto-detect fails -> old build fell back to PowerShell
    reliability counters with a blanket 'install smartmontools' message.

The fake smartctl answers ONLY to '-d sat,12' (classic cheap-bridge
behaviour), so these tests prove the new dialect probe actually recovers
full ATA SMART + TBW through the bridge.

  scenario 'usb'     : sat,12 works -> full SMART via probe + persisted hint
  scenario 'usbdead' : no dialect works -> negative hint + honest fallback

Run:  python3 scripts/test_usb_bridge.py
"""
import json
import os
import shutil
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
FAKE_DIR = os.path.join(ROOT, "scripts", "fake_smart")

# isolate persistent config BEFORE any monitor import touches the store
TEST_DATA = os.path.join(ROOT, "scripts", ".test_data_usb")
os.environ["XDG_DATA_HOME"] = TEST_DATA
os.environ["PATH"] = FAKE_DIR + os.pathsep + os.environ.get("PATH", "")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[PASS]" if cond else "[FAIL]", name,
                         ("  -> " + str(detail)) if detail and not cond else ""))


def ps_stub(snippet, timeout=25):
    """PowerShell layer emulating the reporter's machine + Innostor USB HDD."""
    if "Get-StorageReliabilityCounter" in snippet:
        return [{
            "DeviceId": 0, "Wear": 5, "Temperature": 40,
            "ReadErrorsTotal": 0, "ReadErrorsUncorrected": 0,
            "WriteErrorsTotal": 0, "WriteErrorsUncorrected": 0,
            "PowerOnHours": 2981, "StartStopCycleCount": 1517,
        }]
    if "Get-PhysicalDisk" in snippet:
        return [{
            "DeviceId": 0, "FriendlyName": "Innostor Ext. HDD",
            "SerialNumber": "20240906", "MediaType": "Unspecified",
            "BusType": "USB", "HealthStatus": "Healthy", "Health": "Healthy",
            "SpindleSpeed": None, "Size": 320072933376,
        }]
    if "Get-Disk" in snippet:
        return [{"Number": 0, "Firmware": "IS888"}]
    if "Get-Partition" in snippet:
        return [{"DiskNumber": 0, "DriveLetter": "E"}]
    return None


def setup(scenario):
    os.environ["FAKE_SCENARIO"] = scenario
    from monitor import util, smart, disks, store
    util.IS_WIN = True
    util.powershell_json = ps_stub
    util.is_admin = lambda: True          # the exe runs elevated (uac_admin)
    smart.SMARTCTL = None
    smart._checked = False
    smart._BRIDGE_HINTS.clear()
    smart._BRIDGE_LOADED = False
    store._cache["config"] = None
    disks._cache["ts"] = 0.0
    disks._smart_cache.clear()
    disks._smart_pending.clear()
    return util, smart, disks, store


# ---------------------------------------------------------------- scenarios
def scenario_usb_recovered():
    print("\n=== scenario 'usb': Innostor bridge answers only -d sat,12 ===")
    util, smart, disks, store = setup("usb")
    check("smartctl found", smart.available())
    check("auto read fails (bridge ignores it)",
          smart.read_smart("/dev/pd0", "").get("available") is False)

    wd = disks._windows_disks()
    check("one disk enumerated", len(wd) == 1, len(wd))
    d = wd[0]
    check("usb type guessed HDD (was UNKNOWN)", d["type"] == "HDD", d["type"])
    check("bus USB", d["bus"] == "USB", d["bus"])
    check("health OK from PowerShell", d["health"] == "OK", d["health"])
    check("scan layer-1 mapped /dev/pd0", d.get("_smartctl_name") == "/dev/pd0",
          d.get("_smartctl_name"))

    # direct probe: dialect ladder must land on sat,12
    s, dialect = smart.read_smart_probed("/dev/pd0", key="20240906")
    check("probe recovered SMART", s.get("available") is True, s.get("issues"))
    check("probe found dialect sat,12", dialect == "sat,12", dialect)
    check("hint persisted in memory", smart.bridge_hint("20240906") == "sat,12",
          smart.bridge_hint("20240906"))
    cfg = store.load_config()
    check("hint persisted to config.json",
          (cfg.get("smart_bridges") or {}).get("20240906") == "sat,12",
          cfg.get("smart_bridges"))

    # full pipeline through _fetch_smart
    setup("usb")                          # reset caches, hint survives on disk
    smart._BRIDGE_HINTS.clear(); smart._BRIDGE_LOADED = False  # force disk reload
    d = disks._windows_disks()[0]
    s = disks._fetch_smart("pd:0", d)
    check("pipeline returns full SMART", s.get("available") is True, s.get("issues"))
    check("model behind bridge", s.get("model") == "WDC WD3200BEVT-22A23T0", s.get("model"))
    check("serial verified", s.get("serial") == "20240906", s.get("serial"))
    check("passed True", s.get("passed") is True, s.get("passed"))
    check("temperature 38", s.get("temperature") == 38, s.get("temperature"))
    check("power-on hours 2981", s.get("power_on_hours") == 2981, s.get("power_on_hours"))
    check("power cycles 1517", s.get("power_cycles") == 1517, s.get("power_cycles"))
    tbw = (s.get("tbw") or {}).get("written_bytes")
    check("TBW 5.00 TB (9765625000 x 512)", tbw == 9765625000 * 512, tbw)
    check("6 ATA attributes", len(s.get("attributes") or []) == 6, len(s.get("attributes") or []))
    check("disk type refined HDD (rotation 5400)", d["type"] == "HDD", d["type"])
    check("disk serial enriched", d.get("serial") == "20240906", d.get("serial"))

    # second fetch must use the persisted hint (fast path, single call)
    calls = []
    orig = smart.read_smart
    def counting(name, dtype="", timeout=25):
        calls.append((name, dtype))
        return orig(name, dtype, timeout=timeout)
    smart.read_smart = counting
    disks._smart_cache.pop("pd:0", None)
    s2 = disks._fetch_smart("pd:0", d)
    smart.read_smart = orig
    check("refetch still full SMART", s2.get("available") is True)
    check("refetch uses cached dialect first",
          calls and calls[0] == ("/dev/pd0", "sat,12"), calls[:2])
    check("refetch probe cheap (<=2 smartctl calls)", len(calls) <= 2, calls)


def scenario_usb_dead():
    print("\n=== scenario 'usbdead': bridge ignores every dialect ===")
    util, smart, disks, store = setup("usbdead")
    d = disks._windows_disks()[0]
    s = disks._fetch_smart("pd:0", d)
    check("fell back to powershell", s.get("source") == "powershell", s.get("source"))
    check("unavailable flag set", s.get("available") is False)
    check("usb_bridge flag set", s.get("usb_bridge") is True, s.get("usb_bridge"))
    check("context advice mentions USB enclosure",
          "USB" in (s.get("fallback_advice") or ""), s.get("fallback_advice"))
    check("reason mentions USB enclosure",
          "USB" in (s.get("issues") or [{}])[0].get("text", ""), s.get("issues"))
    check("temperature from counter 40", s.get("temperature") == 40, s.get("temperature"))
    check("power-on hours from counter 2981", s.get("power_on_hours") == 2981,
          s.get("power_on_hours"))
    check("wear counter table present", bool(s.get("attributes")))
    check("negative hint persisted", smart.bridge_hint("20240906") is False,
          smart.bridge_hint("20240906"))

    # negative hint must make the next cycle cheap (no dialect re-probe)
    calls = []
    orig = smart.read_smart
    def counting(name, dtype="", timeout=25):
        calls.append((name, dtype))
        return orig(name, dtype, timeout=timeout)
    smart.read_smart = counting
    disks._smart_cache.pop("pd:0", None)
    s2 = disks._fetch_smart("pd:0", d)
    smart.read_smart = orig
    check("negative cycle stays on fallback", s2.get("source") == "powershell")
    check("negative cycle skips all smartctl probing", len(calls) == 0, calls)

    # RESCAN must clear the negative verdict so probing retries
    disks.clear_caches()
    check("rescan cleared negative hint", smart.bridge_hint("20240906") is None,
          smart.bridge_hint("20240906"))
    cfg = store.load_config()
    check("rescan cleaned config.json",
          "20240906" not in (cfg.get("smart_bridges") or {}), cfg.get("smart_bridges"))


def scenario_not_admin():
    print("\n=== scenario 'usbdead' WITHOUT elevation: advice must say so ===")
    util, smart, disks, store = setup("usbdead")
    util.is_admin = lambda: False
    d = disks._windows_disks()[0]
    s = disks._fetch_smart("pd:0", d)
    check("advice points at elevation",
          "Administrator" in (s.get("fallback_advice") or ""), s.get("fallback_advice"))
    check("negative hint NOT persisted without admin",
          smart.bridge_hint("20240906") is None, smart.bridge_hint("20240906"))


def scenario_type_guess_only():
    print("\n=== USB type guess + enrich refinement (SSD behind bridge) ===")
    util, smart, disks, store = setup("usb")
    d = disks._windows_disks()[0]
    # simulate a successful smart payload with rotation 0 (SSD in USB enclosure)
    fake = {"available": True, "source": "smartctl", "serial": "20240906",
            "capacity": 320072933376, "protocol": "ATA", "rotation": 0}
    disks._enrich_from_smart(d, fake)
    check("type refined to SSD from rotation", d["type"] == "SSD", d["type"])


def scenario_api_level():
    print("\n=== API level: /api/disks + /api/smart for the USB enclosure ===")
    util, smart, disks, store = setup("usbdead")
    import app as app_mod
    from monitor.context import MONITOR
    store.set_mode("live")
    client = app_mod.app.test_client()

    r = client.get("/api/smart/pd:0")
    check("/api/smart 200", r.status_code == 200, r.status_code)
    s = (r.get_json() or {}).get("smart") or {}
    check("api smart fell back to powershell", s.get("source") == "powershell", s.get("source"))
    check("api smart carries usb_bridge flag", s.get("usb_bridge") is True)
    check("api smart carries fallback_advice",
          "USB" in (s.get("fallback_advice") or ""), s.get("fallback_advice"))

    r = client.get("/api/disks")
    data = r.get_json() or {}
    d = (data.get("disks") or [{}])[0]
    check("api disk type HDD (guessed)", d.get("type") == "HDD", d.get("type"))
    check("api disk smart_limited True", d.get("smart_limited") is True, d.get("smart_limited"))
    check("api disk health OK", d.get("health") == "OK", d.get("health"))
    check("api disk temperature 40 from counter", d.get("temperature") == 40, d.get("temperature"))

    # and the healthy recovery path through the API
    setup("usb")
    smart._BRIDGE_HINTS.clear(); smart._BRIDGE_LOADED = False
    disks.clear_caches()
    disks._smart_cache.clear()
    r = client.get("/api/smart/pd:0")
    s = (r.get_json() or {}).get("smart") or {}
    check("api smart recovered full ATA SMART", s.get("available") is True, s.get("issues"))
    check("api smart 6 attributes", len(s.get("attributes") or []) == 6)
    check("api smart TBW 5 TB", (s.get("tbw") or {}).get("written_bytes") == 9765625000 * 512,
          s.get("tbw"))


if __name__ == "__main__":
    for fn in (scenario_usb_recovered, scenario_usb_dead,
               scenario_not_admin, scenario_type_guess_only, scenario_api_level):
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            FAIL.append("%s raised %r" % (fn.__name__, e))
    print("\n" + "=" * 60)
    print("RESULT: %d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print("  - " + f)
        sys.exit(1)
    print("ALL USB BRIDGE PATH TESTS GREEN")
