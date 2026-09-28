"""LIVE-mode end-to-end: boot real telemetry, generate a 60MB write from THIS
process (keeping the file OPEN so the handle scanner sees it), and assert the
tracker attributes the bytes to our own pid AND to the drive that file lives
on — proving per-process -> per-disk attribution works on real I/O."""
import json
import os
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8798"
LOG = "/home/z/my-project/disk-sentry/scripts/proc_test_live.log"
TARGET = "/home/z/my-project/disk-sentry/scripts/_live_io_blob.bin"
PY = "/home/z/my-project/disk-sentry/.venv/bin/python"


def jget(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read().decode())


def jpost(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def expected_disk_for(path, disks):
    """/api/disks list -> the disk whose mountpoint is the longest prefix of path.
    Mirrors the backend's container fallback: if no disk has any mountpoint,
    '/' maps to the first disk."""
    best = None
    any_mount = any(d.get("mountpoints") for d in disks)
    for d in disks:
        mps = (d.get("mountpoints") or []) if any_mount else ["/"]
        for mp in mps:
            key = str(mp).rstrip("\\/") or "/"
            p = path.replace("\\", "/")
            hit = p == key or (key == "/" and p.startswith("/")) or \
                p.startswith(key + "/") or p.startswith(key + "\\")
            if hit:
                if best is None or len(key) > len(best[0]):
                    best = (key, d)
    return best[1] if best else None


def main():
    proc = subprocess.Popen(
        [PY, "run.py", "--no-browser", "--port", "8798"],
        cwd="/home/z/my-project/disk-sentry",
        stdout=open(LOG, "w"), stderr=subprocess.STDOUT,
    )
    fh = None
    try:
        for _ in range(40):
            try:
                jget("/api/health")
                break
            except Exception:
                time.sleep(0.5)
        else:
            print("FAIL: server never came up")
            return 1
        # force LIVE mode (a previous test persisted mode=sim)
        r = jpost("/api/config", {"mode": "live"})
        assert r.get("mode") == "live", r
        time.sleep(6)  # 3 tracker ticks

        d = jget("/api/processes")
        assert d["mode"] == "live", d["mode"]
        assert d["demo"] is False
        assert d["tracked"] >= 5, "too few live processes: %d" % d["tracked"]
        assert d["warmup"] is False
        for key in ("pid", "name", "read_rate", "write_rate",
                    "read_session", "write_session", "read_total", "write_total",
                    "drives", "files", "conf"):
            assert key in d["processes"][0], "missing %s" % key
        print("mode=%s tracked=%d restricted=%d admin=%s" %
              (d["mode"], d["tracked"], d["restricted"], d["admin"]))

        # ---- sustained real writes from THIS process (handle stays OPEN) ----
        mypid = os.getpid()
        import threading
        stop_evt = threading.Event()

        def _writer(fh):
            chunk = b"A" * (4 * 1024 * 1024)
            while not stop_evt.is_set():
                fh.write(chunk)
                fh.flush()
                time.sleep(0.7)

        fh = open(TARGET, "wb")
        th = threading.Thread(target=_writer, args=(fh,), daemon=True)
        th.start()
        print("sustained ~5.7MB/s writes from pid %d (handle open), waiting for rate ticks + handle scans..." % mypid)
        time.sleep(10)  # ~5 rate ticks + 2 file-scan rounds while writing

        d2 = jget("/api/processes")
        row = next((p for p in d2["processes"] if p["pid"] == mypid), None)
        assert row is not None, "our pid %d not in top-250 writers list" % mypid
        print("our row: name=%s write_rate=%.1f B/s write_session=%.0f write_total=%.0f" %
              (row["name"], row["write_rate"], row["write_session"], row["write_total"]))
        assert row["write_rate"] > 100 * 1024, \
            "sustained writes not visible in rate (rate=%.1f)" % row["write_rate"]

        # ---- drive attribution on REAL I/O (asserted mid-burst) ----
        disks = jget("/api/disks")["disks"]
        want = expected_disk_for(TARGET, disks)
        assert want is not None, "no disk mountpoint covers %s (mounts=%s)" % (
            TARGET, [x.get("mountpoints") for x in disks])
        print("expected disk for %s: id=%s name=%s mounts=%s" %
              (TARGET, want["id"], want["name"], want.get("mountpoints")))

        drives = row.get("drives") or []
        print("our drives:", json.dumps(drives))
        print("our files:", [f for f in (row.get("files") or []) if "blob" in f][:2])
        assert drives, "no drive attribution for our own pid (conf=%s files_hidden=%s)" % (
            row.get("conf"), row.get("files_hidden"))
        assert any(x["disk_id"] == want["id"] for x in drives), \
            "expected disk %s in drives, got %s" % (want["id"], drives)
        assert row.get("conf") in ("live", "est"), row.get("conf")
        assert any(TARGET in f for f in (row.get("files") or [])), \
            "our open file path not visible in detail list"
        share = sum(x["share"] for x in drives)
        assert 0.97 <= share <= 1.01, "shares must sum to ~1: %s" % share

        # by_drive rollup must attribute our writes to that disk RIGHT NOW
        bd = d2["by_drive"]
        brow = next((b for b in bd if b["disk_id"] == want["id"]), None)
        assert brow is not None, "disk %s missing from by_drive" % want["id"]
        print("by_drive[%s]: exact W=%.1f est W=%.1f top=%s" %
              (want["id"], brow["write_bps"], brow["est_write_bps"],
               [(t["name"], round(t["write_bps"])) for t in brow["top_writers"]]))
        assert brow["est_write_bps"] > 0, "no estimated process writes on %s" % want["id"]
        assert any(t["name"] == row["name"] for t in brow["top_writers"]) or \
            brow["est_write_bps"] > row["write_rate"], \
            "our program not reflected in top writers for %s" % want["id"]
        assert d2["attributed"]["pct"] > 0, d2["attributed"]
        print("attributed: %s%% of all process writes mapped to drives" % d2["attributed"]["pct"])

        # ---- stop writing, verify session totals accrued ----
        stop_evt.set()
        th.join(timeout=10)
        try:
            fh.flush()
            os.fsync(fh.fileno())
        except Exception:
            pass
        fh.close()
        fh = None
        time.sleep(3)
        d3 = jget("/api/processes")
        row3 = next((p for p in d3["processes"] if p["pid"] == mypid), None)
        assert row3 is not None
        assert row3["write_session"] > 40 * 1024 * 1024, \
            "sustained writes not in session total (session=%.0f)" % row3["write_session"]
        assert row3["write_total"] >= row3["write_session"]
        print("session writes after burst: %.1f MB" % (row3["write_session"] / 1048576.0))

        # regression: core endpoints still healthy
        for path in ("/api/disks", "/api/io", "/api/system", "/api/events"):
            jget(path)
        print("core endpoints OK (disks/io/system/events)")
        print("ALL PROCESS-IO LIVE CHECKS PASSED — 60MB attributed to own pid + drive %s" % want["id"])
        return 0
    finally:
        if fh:
            try:
                fh.close()
            except Exception:
                pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        try:
            os.remove(TARGET)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
