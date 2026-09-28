"""1-second I/O sampler. LIVE mode reads psutil per-disk counters (Windows:
PhysicalDriveN / Linux: /sys diskstats). SIM mode ticks the simulator workloads.
Keeps 30-minute ring buffers + session totals."""
import threading
import time

import psutil

from . import util

SAMPLE_SEC = 1.0
MAX_SAMPLES = 1800  # 30 min


class IoMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._last_raw = None       # raw key -> cumulative counters
        self._last_ts = None
        self._series = {}           # disk_id -> list[(ts, rb, wb, ri, wi, busy)]
        self._session = {}          # disk_id -> dict(read, write, riops, wiops)
        self._last_rate = {}        # disk_id -> (rb, wb, ri, wi, busy)
        self.mode = "live"
        self.totals = {"read_bps": 0.0, "write_bps": 0.0, "read_iops": 0, "write_iops": 0}
        self.known_ids = set()

    # ---------------------------------------------------------------- lifecycle
    def start(self, mode="live"):
        self.mode = mode
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="io-monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def set_mode(self, mode):
        with self._lock:
            self.mode = mode
            self._last_raw = None
            self._series = {}
            self._session = {}
            self._last_rate = {}

    def set_known_ids(self, ids):
        self.known_ids = set(ids)

    # ---------------------------------------------------------------- sampling
    def _loop(self):
        while not self._stop.wait(SAMPLE_SEC):
            try:
                self._tick()
            except Exception:
                pass

    def _tick(self):
        now = time.time()
        if self.mode == "sim":
            from . import simulator
            with self._lock:
                for did, drive in simulator.SIM_DRIVES.items():
                    r, w = drive.tick(SAMPLE_SEC)
                    ri, wi = drive.iops()
                    self._append(did, now, r, w, ri, wi, drive.busy())
                    s = self._session.setdefault(did, {"read": 0, "write": 0, "riops": 0, "wiops": 0})
                    s["read"] += r * SAMPLE_SEC
                    s["write"] += w * SAMPLE_SEC
                    s["riops"] += ri
                    s["wiops"] += wi
            self._totals_from_last()
            return

        counters = psutil.disk_io_counters(perdisk=True, nowrap=True) or {}
        with self._lock:
            last = self._last_raw
            dt = (now - self._last_ts) if self._last_ts else 0
            self._last_ts = now
            mapped = {}
            for raw_key, c in counters.items():
                k = util.normalize_key(raw_key)
                if k.startswith(("loop", "ram", "zram", "dm-", "md", "sr")):
                    continue
                parent = util.partition_parent(raw_key) or k
                # partition counters fold into parent disk
                tgt = parent if parent in self.known_ids or parent in mapped else k
                if k in self.known_ids:
                    tgt = k
                if tgt in mapped:
                    m = mapped[tgt]
                    m["rb"] += c.read_bytes; m["wb"] += c.write_bytes
                    m["rc"] += c.read_count; m["wc"] += c.write_count
                    m["rt"] += c.read_time; m["wt"] += c.write_time
                else:
                    mapped[tgt] = {"rb": c.read_bytes, "wb": c.write_bytes,
                                   "rc": c.read_count, "wc": c.write_count,
                                   "rt": c.read_time, "wt": c.write_time}
            if last is None:
                self._last_raw = mapped
                return
            if dt <= 0:
                dt = SAMPLE_SEC
            for did, m in mapped.items():
                if did.startswith(("loop", "ram", "zram", "dm-", "md", "sr", "pmem", "nbd")):
                    continue
                prev = last.get(did)
                if prev is None:
                    continue
                rb = max(0, m["rb"] - prev["rb"]) / dt
                wb = max(0, m["wb"] - prev["wb"]) / dt
                rc = max(0, m["rc"] - prev["rc"]) / dt
                wc = max(0, m["wc"] - prev["wc"]) / dt
                busy = min(100.0, 100.0 * ((m["rt"] - prev["rt"]) + (m["wt"] - prev["wt"])) / (dt * 1000.0))
                self._append(did, now, rb, wb, rc, wc, busy)
                s = self._session.setdefault(did, {"read": 0, "write": 0, "riops": 0, "wiops": 0})
                s["read"] += rb * dt
                s["write"] += wb * dt
                s["riops"] += rc * dt
                s["wiops"] += wc * dt
            self._last_raw = mapped
            self._totals_from_last()

    def _append(self, did, ts, rb, wb, ri, wi, busy):
        buf = self._series.setdefault(did, [])
        buf.append((ts, rb, wb, ri, wi, busy))
        if len(buf) > MAX_SAMPLES:
            del buf[: len(buf) - MAX_SAMPLES]
        self._last_rate[did] = (rb, wb, ri, wi, busy)

    def _totals_from_last(self):
        rates = self._last_rate
        self.totals = {
            "read_bps": sum(v[0] for v in rates.values()),
            "write_bps": sum(v[1] for v in rates.values()),
            "read_iops": int(sum(v[2] for v in rates.values())),
            "write_iops": int(sum(v[3] for v in rates.values())),
        }

    # ---------------------------------------------------------------- queries
    def snapshot(self):
        with self._lock:
            rates = dict(self._last_rate)
            session = {k: dict(v) for k, v in self._session.items()}
        disks = {}
        for did, (rb, wb, ri, wi, busy) in rates.items():
            disks[did] = {"read_bps": rb, "write_bps": wb,
                          "read_iops": int(ri), "write_iops": int(wi),
                          "busy_pct": round(busy, 1)}
        return {
            "ts": time.time(),
            "mode": self.mode,
            "disks": disks,
            "totals": {k: (round(v, 1) if isinstance(v, float) else v) for k, v in self.totals.items()},
            "session": session,
        }

    def history(self, seconds=120, max_points=240):
        cutoff = time.time() - seconds
        with self._lock:
            series = {did: [row for row in buf if row[0] >= cutoff]
                      for did, buf in self._series.items()}
        out = {"ids": [], "t": None, "read": {}, "write": {}}
        if not series:
            return out
        # union timeline
        all_ts = sorted({row[0] for rows in series.values() for row in rows})
        step = max(1, int(len(all_ts) / max_points) if all_ts else 1)
        all_ts = all_ts[::step]
        tmap = {round(t): i for i, t in enumerate(all_ts)}
        out["t"] = [round(t, 1) for t in all_ts]
        for did, rows in series.items():
            r = [0.0] * len(all_ts)
            w = [0.0] * len(all_ts)
            for ts, rb, wb, *_ in rows:
                i = tmap.get(round(ts))
                if i is not None:
                    r[i] = rb
                    w[i] = wb
            out["ids"].append(did)
            out["read"][did] = r
            out["write"][did] = w
        return out

    def avg_write_bps(self, seconds=600):
        cutoff = time.time() - seconds
        agg = []
        with self._lock:
            for buf in self._series.values():
                agg.extend(row[2] for row in buf if row[0] >= cutoff)
        return (sum(agg) / len(agg)) if agg else 0.0

    def session_summary(self):
        with self._lock:
            return {k: dict(v) for k, v in self._session.items()}
