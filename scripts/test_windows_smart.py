#!/usr/bin/env python3
"""Offline e2e test of the DiskSentry Windows SMART acquisition path.

Emulates the reporter's machine (Seagate ZP1024CM30002 NVMe, PowerShell
serial '6479_A78A_6F00_09F6.') against a fake smartctl binary and a stubbed
PowerShell layer, then verifies every layer of the fixed pipeline:

  scenario 'ok'         : scan carries PhysicalDrive info  -> layer-1 key match
  scenario 'bare'       : scan has NO PhysicalDrive info   -> layer-2 serial match
  scenario 'deviceskey' : non-standard JSON scan key       -> still parsed
  scenario 'textscan'   : JSON scan broken                 -> text scan fallback
  scenario 'fail'       : smartctl can't read the device   -> PowerShell fallback
  plus read_smart gating on the invalid '\\\\.\\PhysicalDrive0' device name.

Run:  python3 scripts/test_windows_smart.py
"""
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
FAKE_DIR = os.path.join(ROOT, "scripts", "fake_smart")
os.environ.setdefault("XDG_DATA_HOME", os.path.join(ROOT, "scripts", ".test_data"))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[PASS]" if cond else "[FAIL]", name,
                         ("  -> " + str(detail)) if detail and not cond else ""))


def reset_modules():
    from monitor import smart, disks
    smart.SMARTCTL = None
    smart._checked = False
    smart._BRIDGE_HINTS.clear()
    smart._BRIDGE_LOADED = False
    disks._cache["ts"] = 0.0
    disks._smart_cache.clear()
    disks._smart_pending.clear()


def ps_stub(snippet, timeout=25):
    """Stub the PowerShell layer with the reporter's exact machine data."""
    if "Get-PhysicalDisk | Get-StorageReliabilityCounter" in snippet:
        return [{
            "DeviceId": 0, "Wear": 3, "Temperature": 317,
            "ReadErrorsTotal": 0, "ReadErrorsUncorrected": 0,
            "WriteErrorsTotal": 0, "WriteErrorsUncorrected": 0,
            "PowerOnHours": 4203, "StartStopCycleCount": 1511,
        }]
    if "Get-PhysicalDisk" in snippet:
        return [{
            "DeviceId": 0, "FriendlyName": "Seagate ZP1024CM30002",
            "SerialNumber": "6479_A78A_6F00_09F6.", "MediaType": "SSD",
            "BusType": "NVMe", "HealthStatus": "Healthy",
            "SpindleSpeed": None, "Size": 1024209543168,
        }]
    if "Get-Disk" in snippet:
        return [{"Number": 0, "Firmware": "SUNSY080"}]
    if "Get-Partition" in snippet:
        return [{"DiskNumber": 0, "DriveLetter": "C"}]
    return None


def setup(scenario):
    os.environ["FAKE_SCENARIO"] = scenario
    os.environ["PATH"] = FAKE_DIR + os.pathsep + os.environ.get("PATH", "")
    from monitor import util, smart, disks
    util.IS_WIN = True                 # force the Windows branch at runtime
    util.powershell_json = ps_stub     # stub PowerShell enumeration
    smart.SMARTCTL = None
    smart._checked = False
    return util, smart, disks


def expect_full_smart(s, label):
    """Common assertions for a fully-populated NVMe smart dict."""
    check("%s: available" % label, s.get("available") is True)
    check("%s: device name recorded" % label, s.get("device") == "/dev/nvme0", s.get("device"))
    check("%s: model" % label, s.get("model") == "ZP1024CM30002", s.get("model"))
    check("%s: serial" % label, s.get("serial") == "6479A78A6F0009F6", s.get("serial"))
    check("%s: protocol NVMe" % label, s.get("protocol") == "NVMe", s.get("protocol"))
    check("%s: passed" % label, s.get("passed") is True, s.get("passed"))
    check("%s: temperature 42" % label, s.get("temperature") == 42, s.get("temperature"))
    check("%s: power-on hours 4203" % label, s.get("power_on_hours") == 4203, s.get("power_on_hours"))
    check("%s: power cycles 1511" % label, s.get("power_cycles") == 1511, s.get("power_cycles"))
    check("%s: unsafe shutdowns 42" % label, s.get("unsafe_shutdowns") == 42, s.get("unsafe_shutdowns"))
    check("%s: media errors 0" % label, s.get("media_errors") == 0, s.get("media_errors"))
    tbw = (s.get("tbw") or {}).get("written_bytes")
    check("%s: TBW 5060217856000 (5.06 TB)" % label, tbw == 9883238 * 512000, tbw)
    check("%s: NVMe health log present" % label, bool(s.get("nvme_log")), "missing")
    check("%s: percentage used 3" % label, s.get("percentage_used") == 3, s.get("percentage_used"))


# ---------------------------------------------------------------- scenarios
def scenario_ok():
    print("\n=== scenario 'ok': scan with PhysicalDrive info (layer-1 match) ===")
    util, smart, disks = setup("ok")
    devs = smart.scan_devices()
    check("scan parses device_list", len(devs) == 2, devs)
    check("scan extracts pd:0 from info_name", devs[0].get("pd") == 0, devs[0])
    wd = disks._windows_disks()
    check("one disk enumerated", len(wd) == 1, len(wd))
    d = wd[0]
    check("disk id pd:0", d["id"] == "pd:0", d["id"])
    check("layer-1 mapped /dev/nvme0", d.get("_smartctl_name") == "/dev/nvme0", d.get("_smartctl_name"))
    s = disks._fetch_smart("pd:0", d)
    expect_full_smart(s, "ok")
    check("disk enriched serial", d.get("serial") == "6479_A78A_6F00_09F6.", d.get("serial"))
    check("disk type NVMe", d.get("type") == "NVMe", d.get("type"))


def scenario_bare():
    print("\n=== scenario 'bare': NO PhysicalDrive info (layer-2 serial match) ===")
    util, smart, disks = setup("bare")
    devs = smart.scan_devices()
    check("scan parses device_list", len(devs) == 1, devs)
    check("no pd hint present", devs[0].get("pd") is None, devs[0])
    wd = disks._windows_disks()
    d = wd[0]
    check("serial match mapped /dev/nvme0", d.get("_smartctl_name") == "/dev/nvme0", d.get("_smartctl_name"))
    s = disks._fetch_smart("pd:0", d)
    expect_full_smart(s, "bare")


def scenario_deviceskey():
    print("\n=== scenario 'deviceskey': non-standard scan JSON key ===")
    util, smart, disks = setup("deviceskey")
    devs = smart.scan_devices()
    check("scan parses 'devices' key", len(devs) == 2, devs)
    wd = disks._windows_disks()
    d = wd[0]
    check("mapped /dev/nvme0", d.get("_smartctl_name") == "/dev/nvme0", d.get("_smartctl_name"))
    s = disks._fetch_smart("pd:0", d)
    expect_full_smart(s, "deviceskey")


def scenario_textscan():
    print("\n=== scenario 'textscan': JSON scan broken -> text scan fallback ===")
    util, smart, disks = setup("textscan")
    devs = smart.scan_devices()
    check("text scan parsed", len(devs) == 2, devs)
    check("pd:0 from text comment", devs[0].get("pd") == 0, devs[0])
    check("type nvme from text", devs[0].get("type") == "nvme", devs[0])
    wd = disks._windows_disks()
    d = wd[0]
    check("mapped /dev/nvme0", d.get("_smartctl_name") == "/dev/nvme0", d.get("_smartctl_name"))
    s = disks._fetch_smart("pd:0", d)
    expect_full_smart(s, "textscan")


def scenario_fail():
    print("\n=== scenario 'fail': smartctl unreadable -> PowerShell fallback ===")
    util, smart, disks = setup("fail")
    # direct: the old broken device name must be gated as unavailable
    s = smart.read_smart("\\\\.\\PhysicalDrive0", "")
    check("bad device name gated unavailable", s.get("available") is False, s)
    check("real smartctl reason surfaced",
          "Unable to detect device type" in (s.get("issues") or [{}])[0].get("text", ""),
          s.get("issues"))
    # full path: PowerShell reliability counters must take over
    s = disks._fetch_smart("pd:0", {"id": "pd:0", "bus": "NVME", "serial": "6479_A78A_6F00_09F6."})
    check("fallback source powershell", s.get("source") == "powershell", s.get("source"))
    check("fallback unavailable-but-populated", s.get("available") is False)
    check("temperature from counter (317K -> 44C)", s.get("temperature") == 44, s.get("temperature"))
    check("power-on hours from counter", s.get("power_on_hours") == 4203, s.get("power_on_hours"))
    check("wear attribute present", bool(s.get("attributes")), "missing")
    if s.get("attributes"):
        check("wear row value 3", s["attributes"][0].get("raw_value") == 3, s["attributes"][0])


def scenario_mapping_absent():
    print("\n=== scenario 'fail' + no _smartctl_name: brute-force candidates ===")
    util, smart, disks = setup("fail")
    # disk with NVMe bus, no mapping attached: candidates must be tried and
    # rejected (serial mismatch / unavailable), then PS fallback engaged
    s = disks._fetch_smart("pd:0", {"id": "pd:0", "bus": "NVME", "serial": "6479_A78A_6F00_09F6."})
    check("fell through to powershell", s.get("source") == "powershell", s.get("source"))


def scenario_probe_identity():
    print("\n=== probe_identity ===")
    util, smart, disks = setup("ok")
    ident = smart.probe_identity("/dev/nvme0", "nvme")
    check("identity model", ident and ident.get("model") == "ZP1024CM30002", ident)
    check("identity serial", ident and ident.get("serial") == "6479A78A6F0009F6", ident)
    check("identity capacity", ident and ident.get("capacity") == 1024209543168, ident)
    from monitor import util as u
    check("norm_serial strips PS decoration",
          u.norm_serial("6479_A78A_6F00_09F6.") == u.norm_serial("6479A78A6F0009F6"))


if __name__ == "__main__":
    for fn in (scenario_ok, scenario_bare, scenario_deviceskey, scenario_textscan,
               scenario_fail, scenario_mapping_absent, scenario_probe_identity):
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
    print("ALL WINDOWS SMART PATH TESTS GREEN")
