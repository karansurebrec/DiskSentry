"""Boot DiskSentry in SIM mode and verify the /api/processes pipeline
including per-process drive attribution and the by_drive rollup."""
import json
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8799"
LOG = "/home/z/my-project/disk-sentry/scripts/proc_test_server.log"


def jget(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read().decode())


def main():
    proc = subprocess.Popen(
        [sys.executable, "run.py", "--sim", "--no-browser", "--port", "8799"],
        cwd="/home/z/my-project/disk-sentry",
        stdout=open(LOG, "w"), stderr=subprocess.STDOUT,
    )
    try:
        # wait for server up
        for _ in range(40):
            try:
                jget("/api/health")
                break
            except Exception:
                time.sleep(0.5)
        else:
            print("FAIL: server never came up")
            return 1
        # wait for two tracker ticks (rates need 2 samples; sim needs 1 to spawn)
        time.sleep(5)
        d = jget("/api/processes")
        assert d["mode"] == "sim", d["mode"]
        assert d["demo"] is True
        assert d["tracked"] >= 10, d["tracked"]
        assert d["warmup"] is False
        procs = d["processes"]
        assert len(procs) >= 10
        for key in ("pid", "name", "read_rate", "write_rate",
                    "read_session", "write_session", "read_total", "write_total",
                    "drives", "files", "files_hidden", "conf"):
            assert key in procs[0], "missing field %s" % key
        assert procs[0]["write_rate"] >= procs[-1]["write_rate"], "not sorted by write_rate"
        assert d["totals"]["write_bps"] > 0, d["totals"]
        print("mode=%s tracked=%d restricted=%d admin=%s" % (d["mode"], d["tracked"], d["restricted"], d["admin"]))
        print("totals:", d["totals"])
        print("top 5 writers:")
        for p in procs[:5]:
            print("  %-18s pid=%-6d w=%9.1f B/s  r=%9.1f B/s  sessW=%10.0f  lifeW=%10.2e  drives=%s"
                  % (p["name"], p["pid"], p["write_rate"], p["read_rate"],
                     p["write_session"], p["write_total"],
                     [(x["drive"], x["share"]) for x in p["drives"]]))

        # ---- drive attribution (v1.2.0) ----
        assert "attributed" in d and d["attributed"]["pct"] > 0, d.get("attributed")

        chrome = next(p for p in procs if p["name"] == "chrome.exe")
        assert len(chrome["drives"]) == 2, chrome["drives"]
        drv_by_letter = {x["drive"]: x for x in chrome["drives"]}
        assert "C:" in drv_by_letter and "D:" in drv_by_letter, drv_by_letter
        total_share = sum(x["share"] for x in chrome["drives"])
        assert 0.97 <= total_share <= 1.01, "shares must sum to ~1: %s" % total_share
        assert drv_by_letter["C:"]["disk_id"] == "sim0", drv_by_letter["C:"]
        assert drv_by_letter["C:"]["type"] == "NVMe"
        assert drv_by_letter["D:"]["disk_id"] == "sim2", drv_by_letter["D:"]
        assert chrome["files"] and any("Chrome" in f for f in chrome["files"]), chrome["files"]
        assert chrome["conf"] == "est", chrome["conf"]
        assert chrome["files_hidden"] is False
        print("chrome attribution OK: C:%d%% D:%d%% + %d file paths"
              % (round(drv_by_letter["C:"]["share"] * 100),
                 round(drv_by_letter["D:"]["share"] * 100), len(chrome["files"])))

        system = next(p for p in procs if p["name"] == "System")
        assert len(system["drives"]) == 1 and system["drives"][0]["drive"] == "C:"
        assert any("pagefile" in f for f in system["files"]), system["files"]
        print("System attribution OK: pagefile.sys -> C:")

        # ---- by_drive rollup ----
        bd = d["by_drive"]
        assert len(bd) == 5, "expected 5 sim drives in rollup, got %d" % len(bd)
        for b in bd:
            for key in ("disk_id", "drive", "name", "type", "bus", "write_bps",
                        "read_bps", "est_write_bps", "est_read_bps", "top_writers", "share_pct"):
                assert key in b, "by_drive missing %s" % key
        cdrv = next(b for b in bd if b["drive"] == "C:")
        assert cdrv["est_write_bps"] > 0, cdrv
        top_names = [t["name"] for t in cdrv["top_writers"]]
        assert any(n in top_names for n in ("chrome.exe", "msedge.exe", "MsMpEng.exe")), top_names
        ddrv = next(b for b in bd if b["drive"] == "D:")
        assert ddrv["est_write_bps"] > 0 and "steam.exe" in [t["name"] for t in ddrv["top_writers"]], ddrv
        edrv = next(b for b in bd if b["drive"] == "E:")
        assert edrv["bus"] == "USB" and edrv["type"] == "HDD", edrv
        shares = sum(b["share_pct"] for b in bd)
        assert 99 <= shares <= 101, "share_pct must sum to ~100: %s" % shares
        print("by_drive rollup OK:")
        for b in bd:
            print("  %-4s %-26s %-5s exact=%9.1f est=%9.1f share=%5s%% top=%s"
                  % (b["drive"], b["name"][:26], b["type"], b["write_bps"],
                     b["est_write_bps"], b["share_pct"],
                     [t["name"] for t in b["top_writers"]]))

        # rates should evolve between polls
        time.sleep(3)
        d2 = jget("/api/processes")
        assert d2["ts"] > d["ts"]
        assert any(p["write_session"] > q["write_session"]
                   for p, q in zip(d2["processes"], d["processes"])), "session bytes not accruing"
        # HTML section present
        html = urllib.request.urlopen(BASE + "/", timeout=10).read().decode()
        for marker in ("PROCESS I/O", "proc-body", "proc-q", "proc-group", "ps-top",
                       "ps-drv", "WRITING TO", "pdrive", "WHERE THE WRITES GO", "v1.3.1"):
            assert marker in html, "HTML missing marker %s" % marker
        print("HTML markers OK (section + drive column + breakdown + footer v1.3.1)")
        # system version
        s = jget("/api/system")
        assert s["sentry_version"] == "1.3.1", s["sentry_version"]
        print("api/system version:", s["sentry_version"])
        print("ALL PROCESS-IO SIM CHECKS PASSED (incl. drive attribution)")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
