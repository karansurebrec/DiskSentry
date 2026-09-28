"""SIM mode: five realistic simulated drives with live workloads, thermal drift,
TBW accrual, SMART attribute tables and an event stream. Produces the same
normalized dicts as disks.py/smart.py so the whole pipeline is mode-agnostic."""
import random
import time

_SATA_SSD_ATTRS = [
    (1, "Raw Read Error Rate", 100, 100, 0, 0),
    (5, "Reallocated Sector Count", 100, 100, 10, 0),
    (9, "Power-On Hours", 99, 99, 0, None),
    (12, "Power Cycle Count", 100, 100, 0, None),
    (170, "Reserved Block Count", 100, 100, 10, 0),
    (171, "Program Fail Count", 100, 100, 0, 0),
    (172, "Erase Fail Count", 100, 100, 0, 0),
    (173, "Average Block Erase Count", 100, 100, 0, None),
    (174, "Unexpected Power Loss", 100, 100, 0, None),
    (177, "Wear Leveling Count", 100, 100, 0, None),
    (181, "Program Fail Count (total)", 100, 100, 0, 0),
    (183, "Runtime Bad Block", 100, 100, 0, 0),
    (187, "Reported Uncorrectable Errors", 100, 100, 0, 0),
    (194, "Temperature Celsius", 100, 90, 10, None),
    (196, "Reallocated Event Count", 100, 100, 0, 0),
    (197, "Current Pending Sectors", 100, 100, 0, 0),
    (198, "Offline Uncorrectable", 100, 100, 0, 0),
    (199, "UDMA CRC Error Count", 200, 200, 0, 0),
    (231, "SSD Life Left", 15, 15, 10, None),
    (241, "Total LBAs Written", 253, 253, 0, None),
    (242, "Total LBAs Read", 253, 253, 0, None),
]

_SATA_HDD_ATTRS = [
    (1, "Raw Read Error Rate", 80, 60, 44, 1_140_000),
    (3, "Spin Up Time", 91, 90, 24, 0),
    (4, "Start/Stop Count", 97, 97, 20, 87),
    (5, "Reallocated Sector Count", 100, 100, 36, 2),
    (7, "Seek Error Rate", 60, 52, 30, 0),
    (9, "Power-On Hours", 93, 93, 0, None),
    (10, "Spin Retry Count", 100, 100, 97, 0),
    (12, "Power Cycle Count", 99, 99, 20, 331),
    (187, "Reported Uncorrectable Errors", 100, 100, 0, 0),
    (189, "High Fly Writes", 99, 99, 0, 18),
    (190, "Airflow Temperature", 67, 45, 45, 32),
    (194, "Temperature Celsius", 118, 102, 45, None),
    (196, "Reallocated Event Count", 100, 100, 0, 2),
    (197, "Current Pending Sectors", 100, 100, 0, 0),
    (198, "Offline Uncorrectable", 100, 100, 0, 0),
    (199, "UDMA CRC Error Count", 200, 200, 0, 0),
]


def _mk_sata_table(rows, tbw_lbas, read_lbas, poh, cycles, temp, life_left, realloc_raw=0):
    table = []
    for aid, name, val, worst, thr, raw in rows:
        raw_value = raw
        if aid == 9:
            raw_value = poh
        elif aid == 12:
            raw_value = cycles
        elif aid == 194:
            raw_value = temp
        elif aid == 190:
            raw_value = max(0, 60 - temp + 32)
        elif aid == 5 or aid == 196:
            raw_value = realloc_raw
        elif aid == 241:
            raw_value = tbw_lbas
        elif aid == 242:
            raw_value = read_lbas
        elif aid == 231:
            raw_value = None  # normalized-only attr
        if raw_value is None and raw is None:
            raw_value = None
        status = "ok"
        if val is not None and thr is not None and val < thr:
            status = "fail"
        elif aid in (5, 187, 196, 197, 198, 199) and (raw_value or 0) > 0:
            status = "warn"
        table.append({
            "id": aid, "id_hex": "0x%02X" % aid, "name": name,
            "value": val, "worst": worst, "thresh": thr,
            "raw_value": raw_value,
            "raw_string": ("-" if raw_value is None else (format(raw_value, ","))),
            "status": status, "flagged": aid in (5, 187, 196, 197, 198, 199, 231, 233, 177, 241, 242, 246),
        })
    table.sort(key=lambda x: x["id"])
    return table, {"poh": poh, "cycles": cycles, "temp": temp, "life_left": life_left}


class SimDrive:
    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.accrued_write = 0
        self.accrued_read = 0
        self._phase = random.random() * 100
        self._burst = 0
        self._rate = (0, 0)

    # ---- workload model: returns (read_bps, write_bps)
    def workload(self, t):
        p = self.workload_kind
        ph = self._phase
        if p == "os":
            r = 2.5e6 + 2.0e6 * abs(_w(t / 23 + ph)) + random.uniform(0, 3e6)
            w = 1.2e6 + 1.4e6 * abs(_w(t / 17 + ph)) + random.uniform(0, 2.5e6)
            if random.random() < 0.006:
                self._burst = random.uniform(80, 260) * 1e6
            if self._burst > 0:
                w += self._burst
                self._burst *= 0.55
        elif p == "game":
            base = 3e4
            if (int(t / 40) % 6) in (1, 2):  # install window
                base = 150e6 + 60e6 * _w(t / 7 + ph)
            r = base * 0.35 + 1e5
            w = base + random.uniform(0, 4e6)
        elif p == "media":
            r = 9e6 + 2.5e6 * _w(t / 11 + ph) + random.uniform(0, 1e6)
            w = 0.4e6 + random.uniform(0, 0.6e6)
        elif p == "backup":
            cycle = (t + ph * 60) % 300
            if 0 < cycle < 95:
                r = 30e6 + random.uniform(0, 8e6)
                w = 55e6 + 14e6 * abs(_w(t / 5))
            else:
                r = 2e5
                w = 1e5
        else:  # usb idle
            r = 1.2e5 * (random.random() < 0.25 and 30 or 1)
            w = 8e4 * (random.random() < 0.1 and 40 or 1)
        # temperature drift
        self.temp = max(25, int(self.base_temp + 4 * _w(t / 90 + ph) + (6 if p != "usb" else 2) * min(1.0, (r + w) / 80e6)))
        self._rate = (r, w)
        return r, w

    def tick(self, dt):
        r, w = self.workload(time.time())
        self.accrued_read += r * dt
        self.accrued_write += w * dt
        return r, w

    def rates(self):
        return self._rate

    def iops(self):
        r, w = self._rate
        rsz = 4 * 1024 if self.type != "HDD" else 128 * 1024
        return max(1, int(r / rsz)), max(1, int(w / rsz))

    def busy(self):
        r, w = self._rate
        cap = 350e6 if self.type == "NVMe" else (180e6 if self.type == "SSD" else 160e6)
        return min(100.0, ((r + w) / cap) * 100.0)

    # ---- identity + smart
    def disk(self):
        return {
            "id": self.id, "name": self.model, "model": self.model, "family": self.family,
            "serial": self.serial, "firmware": self.firmware, "type": self.type, "bus": self.bus,
            "size": self.size, "spindle": self.spindle, "health": self.health,
            "mountpoints": self.mountpoints, "smart_source": "smartctl", "demo": True,
            "smart": self.smart(), "smart_pending": False, "smart_hint": None,
            "endurance": self.endurance,
        }

    def smart(self):
        if self.protocol == "NVMe":
            duw = self.base_duw + int(self.accrued_write / 512000)
            dur = self.base_dur + int(self.accrued_read / 512000)
            pct_used = self.base_pct + int(100.0 * (self.base_duw and duw / self.base_duw - 1 or 0))
            log = {
                "critical_warning": 0, "temperature": self.temp, "available_spare": 100,
                "available_spare_threshold": 10, "percentage_used": pct_used,
                "data_units_read": dur, "data_units_written": duw,
                "host_reads": dur * 8, "host_writes": duw * 8,
                "power_cycles": self.cycles, "power_on_hours": self.poh,
                "unsafe_shutdowns": self.unsafe_shut, "media_errors": 0,
            }
            issues = []
            return {
                "available": True, "source": "smartctl", "passed": True,
                "model": self.model, "family": self.family, "serial": self.serial,
                "firmware": self.firmware, "capacity": self.size, "rotation": 0,
                "protocol": "NVMe", "temperature": self.temp, "temp_source": "nvme log",
                "power_on_hours": self.poh, "power_cycles": self.cycles,
                "unsafe_shutdowns": self.unsafe_shut, "critical_warning": 0,
                "available_spare": 100, "percentage_used": pct_used, "media_errors": 0,
                "tbw": {"written_bytes": duw * 512000, "read_bytes": dur * 512000,
                        "attr": "nvme data_units_written", "written_raw": "%d units" % duw},
                "attributes": [], "nvme_log": log, "error_log_count": 0,
                "self_test": {"status": "Completed without error"},
                "issues": issues,
            }
        # SATA
        tbw_bytes = self.base_tbw + self.accrued_write
        read_bytes = self.base_read + self.accrued_read
        life = self.life_left
        rows = _SATA_SSD_ATTRS if self.type in ("SSD",) else _SATA_HDD_ATTRS
        realloc = getattr(self, "realloc", 0)
        if self.type == "SSD":
            table, meta = _mk_sata_table(rows, int(tbw_bytes / 512), int(read_bytes / 512),
                                         self.poh, self.cycles, self.temp, life, realloc)
        else:
            table, meta = _mk_sata_table(rows, 0, int(read_bytes / 512),
                                         self.poh, self.cycles, self.temp, None, realloc)
        issues = []
        if realloc:
            issues.append({"sev": "warn", "text": "Reallocated Sector Count raw = %d" % realloc})
        tbw_attr = None
        for a in table:
            if a["id"] == 241 and a["raw_value"]:
                tbw_attr = a
                break
        return {
            "available": True, "source": "smartctl", "passed": self.health != "CRIT",
            "model": self.model, "family": self.family, "serial": self.serial,
            "firmware": self.firmware, "capacity": self.size,
            "rotation": 0 if self.type == "SSD" else self.spindle,
            "protocol": "ATA", "temperature": self.temp, "temp_source": "attr 194",
            "power_on_hours": self.poh, "power_cycles": self.cycles,
            "unsafe_shutdowns": getattr(self, "unsafe_shut", 0),
            "critical_warning": None, "available_spare": None,
            "percentage_used": (100 - life) if life is not None else None,
            "media_errors": None,
            "tbw": {"written_bytes": int(tbw_bytes) if self.type == "SSD" else None,
                    "read_bytes": int(read_bytes),
                    "attr": ("attr 241 (%s)" % tbw_attr["name"]) if tbw_attr else None,
                    "written_raw": tbw_attr["raw_string"] if tbw_attr else None},
            "attributes": table, "nvme_log": None, "error_log_count": 0,
            "self_test": {"status": "Completed without error"},
            "issues": issues,
        }


def _w(x):
    import math
    return math.sin(x)


def build_sim():
    drives = [
        SimDrive(id="sim0", model="SAMSUNG SSD 990 PRO 2TB", family="Samsung SSD 990 PRO",
                 serial="S6Z1NJ0R623145", firmware="3B2QJXA7", type="NVMe", bus="NVMe",
                 protocol="NVMe", size=2000398934016, spindle=0, health="OK",
                 mountpoints=["C:"], workload_kind="os", base_temp=42, temp=42,
                 base_duw=164_000_000, base_dur=237_000_000, base_pct=7,
                 poh=6100, cycles=1204, unsafe_shut=58,
                 endurance={"rated_tbw": 1200.0, "rated_source": "vendor spec"}),
        SimDrive(id="sim1", model="WD_BLACK SN850X 1TB", family="Western Digital WD_BLACK",
                 serial="WT61B2FY#3A9z", firmware="611200WD", type="NVMe", bus="NVMe",
                 protocol="NVMe", size=1000204886016, spindle=0, health="OK",
                 mountpoints=[], workload_kind="game", base_temp=39, temp=39,
                 base_duw=60_500_000, base_dur=180_000_000, base_pct=5,
                 poh=2950, cycles=850, unsafe_shut=21,
                 endurance={"rated_tbw": 600.0, "rated_source": "vendor spec"}),
        SimDrive(id="sim2", model="Crucial MX500 1TB", family="Crucial MX500 SSD",
                 serial="2031E3A71F2K", firmware="M3CR046", type="SSD", bus="SATA",
                 protocol="ATA", size=1000204886016, spindle=0, health="WARN",
                 mountpoints=["D:"], workload_kind="media", base_temp=36, temp=36,
                 base_tbw=291_000_000_000_000, base_read=520_000_000_000_000,
                 life_left=15, poh=8760, cycles=3315, unsafe_shut=112, realloc=0,
                 endurance={"rated_tbw": 360.0, "rated_source": "vendor spec"}),
        SimDrive(id="sim3", model="Seagate IronWolf 8TB", family="Seagate IronWolf NAS",
                 serial="ZHA1YN4L", firmware="SC60", type="HDD", bus="SATA",
                 protocol="ATA", size=8001563222016, spindle=7200, health="WARN",
                 mountpoints=["F:"], workload_kind="backup", base_temp=38, temp=38,
                 base_tbw=0, base_read=310_000_000_000_000, life_left=None,
                 poh=24500, cycles=87, unsafe_shut=9, realloc=2,
                 endurance={"rated_tbw": None, "rated_source": None}),
        SimDrive(id="sim4", model="WD Elements 25A3 4TB", family="Western Digital USB",
                 serial="575836:5A1C", firmware="1021", type="HDD", bus="USB",
                 protocol="ATA", size=4000787030016, spindle=5400, health="OK",
                 mountpoints=["E:"], workload_kind="usb", base_temp=33, temp=33,
                 base_tbw=0, base_read=95_000_000_000_000, life_left=None,
                 poh=9800, cycles=640, unsafe_shut=30, realloc=0,
                 endurance={"rated_tbw": None, "rated_source": None}),
    ]
    return {d.id: d for d in drives}


SIM_DRIVES = build_sim()

_EVENTS = []


def sim_event(text, sev="info"):
    _EVENTS.append({"ts": time.time(), "text": text, "sev": sev})
    del _EVENTS[:-80]
    return _EVENTS[-1]


def sim_events():
    if not _EVENTS:
        _seed_events()
    return list(reversed(_EVENTS[:40]))


def _seed_events():
    t = time.time()
    for dt, text, sev in [
        (47, "TRIM completed on SAMSUNG 990 PRO 2TB (sim0)", "ok"),
        (12, "Crucial MX500 1TB: endurance at 81% of rated TBW", "warn"),
        (8, "Backup job 'nas-mirror' started → IronWolf 8TB", "info"),
        (5, "WD Elements 4TB exited low-power idle", "info"),
        (2, "Snapshot: TBW history persisted (2 SSDs tracked)", "ok"),
    ]:
        _EVENTS.append({"ts": t - dt, "text": text, "sev": sev})


def seed_history_points():
    """14 days of hourly TBW accrual for the SSDs (in-memory only)."""
    import time as _t
    now = int(_t.time())
    pts = []
    for i in range(14 * 24, 0, -1):
        ts = now - i * 3600
        day = i / 24.0
        d = {}
        d["S6Z1NJ0R623145"] = int((164_000_000 - day * 0.9) * 512000)
        d["WT61B2FY#3A9Z"] = int((60_500_000 - day * 0.22) * 512000)
        d["2031E3A71F2K"] = int(291_000_000_000_000 - day * 3600 * 3600 * 4.5)
        d["WT61B2FY#3A9Z".upper()] = d.get("WT61B2FY#3A9Z".upper(), 0)
        pts.append({"ts": ts, "d": {k: v for k, v in d.items() if v}})
    return pts


SIM_SERIALIZED_NOTE = "Simulated telemetry — 5 synthetic drives"
