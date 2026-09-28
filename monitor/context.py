"""Monitor singleton: mode management (LIVE/SIM), background refresher threads,
event log, TBW history snapshots."""
import os
import platform
import threading
import time

import psutil

from . import disks, endurance, io_monitor, process_io, simulator, smart, store, util


class MonitorState:
    def __init__(self):
        self.io = io_monitor.IoMonitor()
        self.proc_io = process_io.ProcessIoTracker()
        self.mode = None            # "live" | "sim" (effective)
        self._threads_started = False
        self._stop = threading.Event()
        self.events = []
        self.boot_ts = time.time()
        self._tbw_seen = {}

    # ------------------------------------------------------------------ start
    def start(self):
        if self._threads_started:
            return
        self._threads_started = True
        self._apply_mode(store.load_config().get("mode", "auto"), announce=False)
        threading.Thread(target=self._maint_loop, name="maint", daemon=True).start()

    def _apply_mode(self, want, announce=True):
        if want not in ("live", "sim"):
            want = "auto"
        live_disks = []
        if want in ("auto", "live"):
            try:
                live_disks, _ = disks.get_disks(force=True)
            except Exception:
                live_disks = []
        if want == "auto":
            want = "live" if live_disks else "sim"
        if want == "live" and not live_disks:
            want = "sim"
        self.mode = want
        self.io.set_mode(want)
        if want == "sim":
            self.io.set_known_ids(list(simulator.SIM_DRIVES.keys()))
        else:
            self.io.set_known_ids([d["id"] for d in live_disks])
        self.io.start(want)
        self.proc_io.set_mode(want)
        self.proc_io.set_disk_provider(self._disk_provider)
        self.proc_io.start(want)
        if announce:
            self.log("mode → %s" % want.upper(), "info")
        return want

    def set_mode(self, mode):
        store.set_mode(mode)
        disks.clear_caches()
        eff = self._apply_mode(mode)
        return eff

    def _disk_provider(self):
        """Disk list for the process-I/O drive mapper (cached enumeration)."""
        if self.mode == "sim":
            return disks.sim_disks()
        try:
            return disks.get_disks()[0]
        except Exception:
            return []

    # ------------------------------------------------------------------ maint
    def _maint_loop(self):
        """Background: SMART refresh (45s), TBW history snapshot (10 min)."""
        next_smart = 0.0
        next_snap = time.time() + 120
        while not self._stop.wait(5.0):
            now = time.time()
            if now >= next_smart:
                next_smart = now + 45
                try:
                    if self.mode == "live":
                        ds, pending = disks.get_disks()
                        if not pending:
                            disks.warm_smart_background(ds)
                except Exception:
                    pass
            if now >= next_snap:
                next_snap = now + 600
                try:
                    self._snapshot_tbw()
                except Exception:
                    pass

    def _snapshot_tbw(self):
        if self.mode == "sim":
            return
        mapping = {}
        for d in disks.get_disks()[0]:
            wb = (d.get("smart") or {}).get("tbw", {}).get("written_bytes")
            if d.get("serial") and wb is not None:
                mapping[d["serial"]] = wb
        if mapping:
            store.append_history(mapping)

    # ------------------------------------------------------------------ data
    def current_disks(self):
        if self.mode == "sim":
            return disks.sim_disks(), set()
        ds, pending = disks.get_disks()
        for d in ds:
            if d.get("smart_pending"):
                hit = None
                # non-blocking peek into smart cache
                from .disks import _smart_cache
                hit = _smart_cache.get(d["id"])
                if hit:
                    d["smart"] = hit["smart"]
                    d["smart_pending"] = False
            if not d.get("smart_pending"):
                disks._apply_health(d)
            endurance.assess(d, self.io.avg_write_bps())
        return ds, pending

    def events_recent(self, limit=40):
        if self.mode == "sim":
            return simulator.sim_events()
        return list(reversed(self.events[-limit:]))

    def log(self, text, sev="info"):
        self.events.append({"ts": time.time(), "text": text, "sev": sev})
        del self.events[:-200]

    def system_info(self):
        cpu = psutil.cpu_percent(interval=0)
        mem = psutil.virtual_memory()
        uname = platform.uname()
        return {
            "os": ("%s %s (%s)" % (uname.system, uname.release, uname.machine)),
            "hostname": uname.node,
            "cpu_pct": cpu,
            "ram_total": mem.total,
            "ram_used_pct": mem.percent,
            "boot_ts": psutil.boot_time(),
            "mode": self.mode,
            "smartctl": smart.smartctl_path(),
            "admin": util.is_admin(),
            "data_dir": store.data_dir(),
            "started_ts": self.boot_ts,
            "sentry_version": "1.3.1",
        }


MONITOR = MonitorState()


def ensure_started():
    MONITOR.start()
    return MONITOR
