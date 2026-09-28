"""Per-process disk I/O tracker — "which program is writing how much, WHERE".

LIVE mode: psutil process_iter + io_counters() deltas -> per-PID read/write
rates (B/s), session bytes (since DiskSentry started) and lifetime totals
(since the process itself started). On Windows these OS counters cover file,
disk and pipe traffic — the same source Task Manager's I/O columns use.
Processes that deny access (other users' / protected system processes when
not elevated) are counted as 'restricted' and skipped.

Drive attribution ("is it writing to my C: SSD or the external HDD?"):
a second sampler thread enumerates the open file handles of the busiest
processes (psutil open_files()), maps every file path to its drive letter /
mount point, and converts that into per-drive shares of each process's I/O.
Shares are refreshed every 4 s; recent sightings decay after 60 s so short
bursts (open -> write -> close) are still attributed. Exact per-drive rates
come from the kernel disk counters (io_monitor); the per-program split is an
estimate built from file handles — Windows offers no per-process-per-disk
counter without kernel ETW tracing, so we show both numbers side by side.

SIM mode: a plausible Windows process population with drifting workloads,
bursty writers, per-program drive footprints and realistic file paths.
"""
import os
import random
import threading
import time

import psutil

from . import util

SAMPLE_SEC = 2.0
MAX_ROWS = 250          # API payload cap
STALE_TICKS = 3         # drop rows unseen for this many consecutive ticks
FILE_SCAN_SEC = 4.0     # open-handle enumeration cadence
FILE_SCAN_TOP = 40      # busiest processes whose files get enumerated
FILE_SCAN_BUDGET = 2.0  # max seconds per scan round (handle-heavy procs)
SIGHT_TTL = 60.0        # seconds a drive sighting survives after last seen
FILE_TTL = 90.0         # seconds a file path stays in the detail list
MAX_FILES_ROW = 8       # recent file paths kept per pid
DRIVE_MAP_TTL = 15.0    # seconds between drive-letter map rebuilds


class ProcessIoTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._fscan = None
        self._stop = threading.Event()
        self.mode = "live"
        self._prev = {}          # pid -> (rb, wb, ts)
        self._rows = {}          # pid -> normalized row dict
        self._missed = {}        # pid -> consecutive missed scans
        self._ticks = 0
        self._sim = None
        self.totals = {"read_bps": 0.0, "write_bps": 0.0}
        self.tracked = 0
        self.restricted = 0
        # drive attribution state
        self._disk_provider = None
        self._dmap = None        # [(casefolded key, display key, disk dict)]
        self._dmap_ts = 0.0
        self._sight = {}         # pid -> {drive key: {"w": n, "ts": t, "cur": bool}}
        self._files = {}         # pid -> [(ts, path), ...]
        self._hidden = {}        # pid -> bool (open_files denied)

    # ---------------------------------------------------------------- lifecycle
    def start(self, mode="live"):
        self.mode = mode
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="proc-io", daemon=True)
        self._thread.start()
        self._fscan = threading.Thread(target=self._fscan_loop, name="proc-files", daemon=True)
        self._fscan.start()

    def stop(self):
        self._stop.set()

    def set_mode(self, mode):
        with self._lock:
            self.mode = mode
            self._reset()

    def set_disk_provider(self, fn):
        """fn() -> list of disk dicts (id/name/type/bus/mountpoints). Used to
        translate file paths into drive letters and physical disks."""
        self._disk_provider = fn

    def _reset(self):
        self._prev = {}
        self._rows = {}
        self._missed = {}
        self._ticks = 0
        self._sim = None
        self._dmap = None
        self._dmap_ts = 0.0
        self._sight = {}
        self._files = {}
        self._hidden = {}
        self.totals = {"read_bps": 0.0, "write_bps": 0.0}
        self.tracked = 0
        self.restricted = 0

    # ---------------------------------------------------------------- sampling
    def _loop(self):
        while not self._stop.wait(SAMPLE_SEC):
            try:
                if self.mode == "sim":
                    self._tick_sim()
                else:
                    self._tick_live()
            except Exception:
                pass

    def _tick_live(self):
        now = time.time()
        seen = {}
        restricted = 0
        for p in psutil.process_iter(attrs=["name"]):
            pid = p.pid
            try:
                io = p.io_counters()
            except psutil.AccessDenied:
                restricted += 1
                continue
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except Exception:
                continue
            name = p.info.get("name") or ""
            if not name:
                name = "pid:%d" % pid
            seen[pid] = (name, io.read_bytes, io.write_bytes)

        with self._lock:
            self._ticks += 1
            self.restricted = restricted
            rows = self._rows
            # age out vanished processes (keep them briefly in case of a
            # transient access failure, with rates zeroed)
            for pid in list(rows.keys()):
                if pid in seen:
                    self._missed.pop(pid, None)
                    continue
                missed = self._missed.get(pid, 0) + 1
                if missed >= STALE_TICKS:
                    rows.pop(pid, None)
                    self._missed.pop(pid, None)
                    self._prev.pop(pid, None)
                    self._sight.pop(pid, None)
                    self._files.pop(pid, None)
                    self._hidden.pop(pid, None)
                else:
                    self._missed[pid] = missed
                    rows[pid]["read_rate"] = 0.0
                    rows[pid]["write_rate"] = 0.0
            # fold current counters into rates / session / totals
            for pid, (name, rb, wb) in seen.items():
                prev = self._prev.get(pid)
                row = rows.get(pid)
                if row is None:
                    rows[pid] = {
                        "pid": pid, "name": name,
                        "read_rate": 0.0, "write_rate": 0.0,
                        "read_session": 0, "write_session": 0,
                        "read_total": rb, "write_total": wb,
                    }
                    self._prev[pid] = (rb, wb, now)
                    self._sight.pop(pid, None)
                    self._files.pop(pid, None)
                    self._hidden.pop(pid, None)
                    continue
                row["name"] = name
                if prev is None or rb < prev[0] or wb < prev[1]:
                    # first sight of an existing row, PID reuse or counter reset
                    row["read_rate"] = 0.0
                    row["write_rate"] = 0.0
                    row["read_total"] = rb
                    row["write_total"] = wb
                    self._prev[pid] = (rb, wb, now)
                    self._sight.pop(pid, None)
                    self._files.pop(pid, None)
                    self._hidden.pop(pid, None)
                    continue
                dt = now - prev[2]
                if dt <= 0:
                    dt = SAMPLE_SEC
                dt = min(dt, 15.0)
                drb = max(0, rb - prev[0])
                dwb = max(0, wb - prev[1])
                row["read_rate"] = drb / dt
                row["write_rate"] = dwb / dt
                row["read_session"] += drb
                row["write_session"] += dwb
                row["read_total"] = rb
                row["write_total"] = wb
                self._prev[pid] = (rb, wb, now)
            self.tracked = len(rows)
            self.totals = {
                "read_bps": sum(r["read_rate"] for r in rows.values()),
                "write_bps": sum(r["write_rate"] for r in rows.values()),
            }

    # ------------------------------------------------------- drive attribution
    def _drive_map(self):
        """[(casefolded key, display key, disk dict)] sorted longest-first."""
        now = time.time()
        if self._dmap is not None and now - self._dmap_ts < DRIVE_MAP_TTL:
            return self._dmap
        entries = []
        if self._disk_provider:
            try:
                dl = self._disk_provider() or []
                for d in dl:
                    for mp in (d.get("mountpoints") or []):
                        key = str(mp).strip()
                        if not key:
                            continue
                        key = key.rstrip("\\/").upper() or key
                        entries.append((key.casefold(), key, d))
                # container fallback: when NO disk reports any mountpoint
                # (overlay-root containers / CI sandboxes — lsblk shows the
                # block device but no mounts), attribute "/" to the first
                # real disk so per-process attribution still works. Never
                # fires on Windows (system drive always has a letter) or on
                # normal Linux installs (where / is always mounted somewhere).
                if not entries and dl:
                    entries.append(("/", "/", dl[0]))
            except Exception:
                entries = []
        entries.sort(key=lambda e: -len(e[0]))
        self._dmap = entries
        self._dmap_ts = now
        return entries

    def _match_drive(self, path):
        """file path -> (display drive key, disk dict) or None."""
        if not path:
            return None
        p = str(path).casefold()
        if p.startswith("\\\\") or p.startswith("//") or p.startswith("\\\\?\\"):
            return None  # UNC / device paths have no drive letter
        for cf, disp, d in self._drive_map():
            if p == cf:
                return disp, d
            if cf == "/":
                if p.startswith("/"):
                    return disp, d
            elif p.startswith(cf + "/") or p.startswith(cf + "\\"):
                return disp, d
        return None

    def _fscan_loop(self):
        while not self._stop.wait(FILE_SCAN_SEC):
            try:
                if self.mode == "live":
                    self._scan_files()
            except Exception:
                pass

    def _scan_files(self):
        """Enumerate open files of the busiest processes and map them to drives."""
        now = time.time()
        with self._lock:
            cand = [(pid, (r.get("write_rate", 0.0) or 0.0) + (r.get("read_rate", 0.0) or 0.0))
                    for pid, r in self._rows.items()]
        cand.sort(key=lambda x: x[1], reverse=True)
        targets = [pid for pid, _ in cand[:FILE_SCAN_TOP]]
        findings = {}
        deadline = now + FILE_SCAN_BUDGET
        for pid in targets:
            if time.time() > deadline:
                break
            try:
                fl = psutil.Process(pid).open_files()
            except psutil.AccessDenied:
                findings[pid] = {"drives": {}, "files": [], "hidden": True}
                continue
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except Exception:
                continue
            drives = {}
            paths = []
            for f in fl or []:
                path = getattr(f, "path", "") or ""
                if not path:
                    continue
                hit = self._match_drive(path)
                if hit:
                    disp = hit[0]
                    drives[disp] = drives.get(disp, 0) + 1
                if len(paths) < 32:
                    paths.append(path)
            findings[pid] = {"drives": drives, "files": paths, "hidden": False}
        # merge under lock
        with self._lock:
            for pid, f in findings.items():
                if pid not in self._rows:
                    continue
                sight = self._sight.setdefault(pid, {})
                for k in sight.values():
                    k["cur"] = False
                for disp, cnt in f["drives"].items():
                    sight[disp.casefold()] = {"w": cnt, "ts": now, "cur": True,
                                              "disp": disp}
                for k in list(sight.keys()):
                    if now - sight[k]["ts"] > SIGHT_TTL:
                        del sight[k]
                self._hidden[pid] = f["hidden"]
                seen_paths = {p for _, p in self._files.get(pid, [])}
                lst = self._files.setdefault(pid, [])
                for path in f["files"]:
                    if path not in seen_paths:
                        lst.append((now, path))
                        seen_paths.add(path)
                self._files[pid] = [(ts, p) for ts, p in lst
                                    if now - ts <= FILE_TTL][-MAX_FILES_ROW:]
            for pid in list(self._sight.keys()):
                if pid not in self._rows:
                    self._sight.pop(pid, None)
                    self._files.pop(pid, None)
                    self._hidden.pop(pid, None)

    # ---------------------------------------------------------------- sim mode
    def _tick_sim(self):
        with self._lock:
            if self._sim is None:
                self._sim = _SimPopulation()
            self._sim.tick(SAMPLE_SEC)
            self._rows = self._sim.rows
            self._ticks += 1
            self.tracked = len(self._rows)
            self.restricted = 0
            self.totals = {
                "read_bps": sum(r["read_rate"] for r in self._rows.values()),
                "write_bps": sum(r["write_rate"] for r in self._rows.values()),
            }

    # ---------------------------------------------------------------- queries
    def snapshot(self):
        with self._lock:
            rows = [dict(r) for r in self._rows.values()]
            totals = dict(self.totals)
            tracked = self.tracked
            restricted = self.restricted
            ticks = self._ticks
            mode = self.mode
            sight = {pid: dict(v) for pid, v in self._sight.items()}
            files = {pid: list(v) for pid, v in self._files.items()}
            hidden = dict(self._hidden)
        # drive key view for this snapshot
        dmap = self._drive_map()
        dk = {}
        for cf, disp, d in dmap:
            dk.setdefault(cf, (disp, d))
        now = time.time()
        att_w = 0.0
        att_r = 0.0
        for r in rows:
            if mode == "sim":
                shares = self._sim_shares(r)
                conf = shares and ("live" if len(shares) == 1 else "est") or "none"
                entries = shares
                r["files"] = list(r.get("_files") or [])
                r["files_hidden"] = False
            else:
                pid = r["pid"]
                s = sight.get(pid) or {}
                live = {k: v for k, v in s.items() if v.get("cur")}
                pool = live if live else s
                if live:
                    conf = "live" if len(live) == 1 else "est"
                elif pool:
                    conf = "stale"
                else:
                    conf = "none"
                entries = sorted(
                    ((v.get("disp") or k, v["w"]) for k, v in pool.items()),
                    key=lambda e: -e[1])
                r["files"] = [p for _, p in (files.get(pid) or [])]
                r["files_hidden"] = bool(hidden.get(pid))
            dlist = []
            if entries:
                total = sum(w for _, w in entries) or 1.0
                for disp, w in entries[:3]:
                    hit = dk.get(disp.casefold())
                    if not hit:
                        continue
                    share = round(min(1.0, w / total), 3)
                    dlist.append({
                        "drive": disp, "disk_id": hit[1].get("id"),
                        "name": hit[1].get("name") or hit[1].get("model"),
                        "type": hit[1].get("type"), "bus": hit[1].get("bus"),
                        "share": share,
                    })
                    att_w += (r.get("write_rate", 0.0) or 0.0) * share
                    att_r += (r.get("read_rate", 0.0) or 0.0) * share
            r["drives"] = dlist
            r["conf"] = conf
            r.pop("_files", None)
            r["read_rate"] = round(r.get("read_rate", 0.0) or 0.0, 1)
            r["write_rate"] = round(r.get("write_rate", 0.0) or 0.0, 1)
        rows.sort(key=lambda r: r.get("write_rate", 0.0) or 0.0, reverse=True)
        rows = rows[:MAX_ROWS]
        pct = 0.0
        if totals.get("write_bps"):
            pct = round(100.0 * att_w / totals["write_bps"], 1)
        return {
            "ts": time.time(),
            "mode": mode,
            "interval": SAMPLE_SEC,
            "warmup": ticks < 2,
            "tracked": tracked,
            "restricted": restricted,
            "admin": util.is_admin(),
            "totals": {k: round(v, 1) for k, v in totals.items()},
            "attributed": {"write_bps": round(att_w, 1), "read_bps": round(att_r, 1),
                           "pct": pct},
            "processes": rows,
        }

    def _sim_shares(self, row):
        """Jittered per-drive weights for a simulated program."""
        w = row.get("_drives") or {}
        if not w:
            return []
        out = []
        for k, weight in w.items():
            jitter = weight * random.uniform(0.72, 1.28) if len(w) > 1 else 1.0
            out.append((k.upper(), jitter))
        return out


# --------------------------------------------------------------- drive rollup
def by_drive(rows, disk_list, io_disks):
    """Merge per-process attribution with exact per-disk kernel counters.

    rows      -> process rows from ProcessIoTracker.snapshot()
    disk_list -> disk dicts (id/name/type/bus/mountpoints)
    io_disks  -> {disk_id: {"write_bps": .., "read_bps": ..}} (io_monitor)
    Returns a per-drive list with exact vs estimated rates + top writers.
    """
    agg = {}
    for r in rows:
        wr = r.get("write_rate", 0.0) or 0.0
        rr = r.get("read_rate", 0.0) or 0.0
        name = r.get("name") or "?"
        for dv in (r.get("drives") or []):
            did = dv.get("disk_id")
            if not did:
                continue
            a = agg.setdefault(did, {"w": 0.0, "r": 0.0, "top": {}})
            a["w"] += wr * (dv.get("share") or 0.0)
            a["r"] += rr * (dv.get("share") or 0.0)
            a["top"][name] = a["top"].get(name, 0.0) + wr * (dv.get("share") or 0.0)
    out = []
    for d in disk_list or []:
        io = (io_disks or {}).get(d.get("id")) or {}
        a = agg.get(d.get("id")) or {"w": 0.0, "r": 0.0, "top": {}}
        top = sorted(((n, v) for n, v in a["top"].items() if v > 0.5),
                     key=lambda kv: kv[1], reverse=True)[:3]
        mps = d.get("mountpoints") or []
        out.append({
            "disk_id": d.get("id"),
            "drive": " ".join(mps) if mps else "—",
            "name": d.get("name") or d.get("model") or d.get("id"),
            "type": d.get("type"), "bus": d.get("bus"),
            "write_bps": round(io.get("write_bps") or 0.0, 1),
            "read_bps": round(io.get("read_bps") or 0.0, 1),
            "est_write_bps": round(a["w"], 1), "est_read_bps": round(a["r"], 1),
            "top_writers": [{"name": n, "write_bps": round(v, 1)} for n, v in top],
        })
    tot = sum(x["write_bps"] for x in out) or 0.0
    for x in out:
        x["share_pct"] = round(100.0 * x["write_bps"] / tot, 1) if tot else 0.0
    out.sort(key=lambda x: x["write_bps"] + x["est_write_bps"], reverse=True)
    return out


# ===================================================================== sim pop
# name, write base B/s, read base B/s, write burst x, read burst x,
# burst prob per tick, lifetime write seed, lifetime read seed,
# drive weights {drive: weight}, file path templates
_SIM_PROCS = [
    ("chrome.exe", 1.6e6, 4.2e6, 6, 8, 0.05, 38.4e9, 121.7e9,
     {"C:": 0.92, "D:": 0.08},
     ["C:\\Users\\you\\AppData\\Local\\Google\\Chrome\\User Data\\Default\\Cache\\Cache_Data\\f_{n:06d}",
      "C:\\Users\\you\\AppData\\Local\\Google\\Chrome\\User Data\\Default\\History"]),
    ("msedge.exe", 0.8e6, 2.0e6, 5, 7, 0.04, 12.9e9, 44.2e9,
     {"C:": 1.0},
     ["C:\\Users\\you\\AppData\\Local\\Microsoft\\Edge\\User Data\\Default\\Cache\\Cache_Data\\data_{n:03d}"]),
    ("Code.exe", 0.6e6, 1.1e6, 4, 3, 0.03, 22.1e9, 9.8e9,
     {"C:": 0.85, "D:": 0.15},
     ["C:\\Users\\you\\AppData\\Roaming\\Code\\logs\\window{n}\\renderer.log",
      "D:\\projects\\site\\.git\\objects\\pack\\pack-{n:07d}.idx"]),
    ("Discord.exe", 0.3e6, 0.7e6, 3, 6, 0.03, 5.4e9, 18.3e9,
     {"C:": 1.0},
     ["C:\\Users\\you\\AppData\\Roaming\\discord\\Cache\\Cache_Data\\f_{n:06d}"]),
    ("steam.exe", 2.4e6, 2.8e6, 12, 5, 0.02, 96.7e9, 14.5e9,
     {"D:": 0.78, "C:": 0.22},
     ["D:\\SteamLibrary\\steamapps\\downloading\\730\\depot_731\\file_{n}.csd",
      "C:\\Program Files (x86)\\Steam\\logs\\content_log.txt"]),
    ("sqlservr.exe", 3.0e6, 2.2e6, 2, 2, 0.00, 187.3e9, 66.9e9,
     {"D:": 0.8, "C:": 0.2},
     ["D:\\MSSQL\\DATA\\sales_log.ldf",
      "C:\\Program Files\\Microsoft SQL Server\\MSSQL16.MSSQLSERVER\\MSSQL\\Data\\tempdb.mdf"]),
    ("MsMpEng.exe", 1.0e6, 5.5e6, 3, 12, 0.06, 28.8e9, 210.4e9,
     {"C:": 1.0},
     ["C:\\ProgramData\\Microsoft\\Windows Defender\\Scans\\History\\service\\DetectionHistory-{n:06d}"]),
    ("SearchIndexer.exe", 0.7e6, 2.4e6, 4, 9, 0.05, 15.2e9, 88.1e9,
     {"C:": 0.8, "D:": 0.2},
     ["C:\\ProgramData\\Microsoft\\Search\\Data\\Applications\\Windows\\Windows.edb",
      "D:\\media\\photos\\IMG_{n:04d}.jpg"]),
    ("svchost.exe", 0.25e6, 0.4e6, 2, 3, 0.02, 9.6e9, 12.0e9,
     {"C:": 1.0},
     ["C:\\Windows\\SoftwareDistribution\\DataStore\\Logs\\edb.log"]),
    ("explorer.exe", 0.12e6, 0.3e6, 2, 2, 0.01, 3.1e9, 7.7e9,
     {"C:": 1.0},
     ["C:\\Users\\you\\AppData\\Local\\Microsoft\\Windows\\Explorer\\thumbcache_{n}.db"]),
    ("dwm.exe", 0.35e6, 0.2e6, 2, 2, 0.01, 6.8e9, 2.9e9,
     {"C:": 1.0},
     ["C:\\Windows\\Temp\\dwm_swapchain_{n}.tmp"]),
    ("System", 0.5e6, 0.9e6, 3, 4, 0.02, 31.5e9, 58.2e9,
     {"C:": 1.0},
     ["C:\\pagefile.sys"]),
    ("DiskSentry.exe", 0.02e6, 0.05e6, 1, 1, 0.00, 0.12e9, 0.20e9,
     {"C:": 1.0},
     ["C:\\Users\\you\\AppData\\Local\\DiskSentry\\tbw_history.json"]),
]

# Windows-Update style visitor: shows up once in a while, hammers the disk,
# then exits — demonstrates live process appear/disappear in the table.
_VISITOR = ("TiWorker.exe", 24.0e6, 3.5e6)


def _instantiate(tmpl, n):
    try:
        return tmpl.replace("{n:06d}", "%06d" % n).replace("{n:07d}", "%07d" % n) \
                   .replace("{n:04d}", "%04d" % n).replace("{n:03d}", "%03d" % n) \
                   .replace("{n}", str(n))
    except Exception:
        return tmpl


class _SimPopulation:
    def __init__(self):
        self.rows = {}
        self._pid = 1044
        self._visitor_pid = None
        self._visitor_left = 0
        self._n = 0
        for (name, wb, rb, wmx, rmx, prob, wseed, rseed, drives, files) in _SIM_PROCS:
            self._spawn(name, wb, rb, wmx, rmx, prob, wseed, rseed, drives, files)

    def _spawn(self, name, wb, rb, wmx, rmx, prob, wseed, rseed, drives, files):
        pid = self._pid
        self._pid += random.randint(3, 17)
        self._n += random.randint(2, 97)
        self.rows[pid] = {
            "pid": pid, "name": name,
            "read_rate": rb * random.uniform(0.4, 1.6),
            "write_rate": wb * random.uniform(0.4, 1.6),
            "read_session": 0, "write_session": 0,
            "read_total": rseed, "write_total": wseed,
            "_wb": wb, "_rb": rb, "_wmx": wmx, "_rmx": rmx,
            "_prob": prob, "_burst_w": 0, "_burst_r": 0,
            "_drives": dict(drives),
            "_files": [_instantiate(t, self._n + i) for i, t in enumerate(files)],
        }

    @staticmethod
    def _walk(cur, base):
        cur += (base - cur) * 0.10 + random.gauss(0.0, 0.28 * base)
        return max(0.0, cur)

    def tick(self, dt):
        for row in self.rows.values():
            if row["_burst_w"] > 0:
                row["write_rate"] = row["_wb"] * row["_wmx"] * random.uniform(0.7, 1.3)
                row["_burst_w"] -= 1
            elif random.random() < row["_prob"]:
                row["_burst_w"] = random.randint(2, 5)
            else:
                row["write_rate"] = self._walk(row["write_rate"], row["_wb"])
            if row["_burst_r"] > 0:
                row["read_rate"] = row["_rb"] * row["_rmx"] * random.uniform(0.7, 1.3)
                row["_burst_r"] -= 1
            elif random.random() < row["_prob"]:
                row["_burst_r"] = random.randint(2, 5)
            else:
                row["read_rate"] = self._walk(row["read_rate"], row["_rb"])
            row["write_session"] += row["write_rate"] * dt
            row["read_session"] += row["read_rate"] * dt
            row["write_total"] += row["write_rate"] * dt
            row["read_total"] += row["read_rate"] * dt
            # occasional file churn: swap one visible path for a fresh one
            if row["_files"] and random.random() < 0.06:
                self._n += random.randint(1, 31)
                i = random.randrange(len(row["_files"]))
                d = row["_files"][i].rsplit("\\", 1)[0]
                row["_files"][i] = d + "\\chunk_%d.tmp" % self._n
        # visitor lifecycle
        if self._visitor_pid is None and random.random() < 0.02:
            name, wb, rb = _VISITOR
            self._spawn(name, wb, rb, 1.5, 1.5, 0.0, 1.1e9, 0.3e9,
                        {"C:": 1.0},
                        ["C:\\Windows\\SoftwareDistribution\\DeliveryOptimization\\do_{n}.tmp"])
            self._visitor_pid = max(self.rows)
            self._visitor_left = random.randint(8, 18)
        elif self._visitor_pid is not None:
            self._visitor_left -= 1
            if self._visitor_left <= 0:
                self.rows.pop(self._visitor_pid, None)
                self._visitor_pid = None
