#!/usr/bin/env python3
"""API-level test: Flask endpoints against the emulated Windows machine.

Same fake-smartctl + stubbed-PowerShell environment as
test_windows_smart.py, but driven through the real Flask app so the
exact JSON the browser consumes is verified end-to-end.
"""
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
FAKE_DIR = os.path.join(ROOT, "scripts", "fake_smart")
os.environ["FAKE_SCENARIO"] = "bare"        # hardest case: serial matching
os.environ["PATH"] = FAKE_DIR + os.pathsep + os.environ.get("PATH", "")

from monitor import util, smart, disks, store


def ps_stub(snippet, timeout=25):
    if "Get-StorageReliabilityCounter" in snippet:
        return None  # smartctl works in this scenario; reliability unused
    if "Get-PhysicalDisk" in snippet:
        return [{"DeviceId": 0, "FriendlyName": "Seagate ZP1024CM30002",
                 "SerialNumber": "6479_A78A_6F00_09F6.", "MediaType": "SSD",
                 "BusType": "NVMe", "HealthStatus": "Healthy",
                 "SpindleSpeed": None, "Size": 1024209543168}]
    if "Get-Disk" in snippet:
        return [{"Number": 0, "Firmware": "SUNSY080"}]
    if "Get-Partition" in snippet:
        return [{"DiskNumber": 0, "DriveLetter": "C"}]
    return None


util.IS_WIN = True
util.powershell_json = ps_stub
smart.SMARTCTL = None
smart._checked = False

import app as app_mod
from monitor.context import MONITOR

ok, bad = [], []
def check(name, cond, detail=""):
    (ok if cond else bad).append(name)
    print("  %s %s%s" % ("[PASS]" if cond else "[FAIL]", name, (" -> " + str(detail)) if not cond else ""))

store.set_mode("live")
client = app_mod.app.test_client()

print("=== /api/system ===")
r = client.get("/api/system")
sysinfo = r.get_json()
check("200", r.status_code == 200)
check("smartctl detected", bool(sysinfo.get("smartctl")), sysinfo.get("smartctl"))

print("=== /api/disks (after warm) ===")
disks.clear_caches()
r = client.get("/api/disks")
data = r.get_json()
check("200", r.status_code == 200)
check("live mode", data.get("mode") == "live", data.get("mode"))
d = (data.get("disks") or [{}])[0]
check("disk pd:0", d.get("id") == "pd:0", d.get("id"))
check("model Seagate", "ZP1024CM30002" in (d.get("model") or ""), d.get("model"))
check("bus NVMe", d.get("bus") == "NVME" or d.get("bus") == "NVMe", d.get("bus"))

print("=== /api/smart/pd:0 ===")
r = client.get("/api/smart/pd:0")
check("200", r.status_code == 200, r.status_code)
payload = r.get_json() or {}
s = payload.get("smart") or {}
check("smart available", s.get("available") is True, s.get("available"))
check("device /dev/nvme0 exposed", s.get("device") == "/dev/nvme0", s.get("device"))
check("temperature 42", s.get("temperature") == 42, s.get("temperature"))
check("power_on_hours 4203", s.get("power_on_hours") == 4203, s.get("power_on_hours"))
check("tbw written bytes", (s.get("tbw") or {}).get("written_bytes") == 9883238 * 512000, s.get("tbw"))
check("nvme_log present", bool(s.get("nvme_log")))
check("serial from smartctl", s.get("serial") == "6479A78A6F0009F6", s.get("serial"))
check("endurance assessed", "endurance" in payload and payload["endurance"] is not None)

print("=== /api/disks after SMART warm ===")
r = client.get("/api/disks")
d = (r.get_json().get("disks") or [{}])[0]
check("temperature on disk card", d.get("temperature") == 42, d.get("temperature"))
check("tbw on disk card", (d.get("tbw") or {}).get("written_bytes") == 9883238 * 512000, d.get("tbw"))
check("power_on_hours on disk card", d.get("power_on_hours") == 4203, d.get("power_on_hours"))
check("smart_pending cleared", d.get("smart_pending") is False, d.get("smart_pending"))

print("=== /api/io/history, /api/events, /api/history ===")
for ep in ("/api/io", "/api/io/history?sec=60", "/api/events", "/api/history", "/api/health"):
    r = client.get(ep)
    check("%s 200" % ep, r.status_code == 200, r.status_code)

print("\n%d passed, %d failed" % (len(ok), len(bad)))
sys.exit(1 if bad else 0)
