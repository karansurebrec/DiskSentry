"""DiskSentry Flask API + dashboard."""
import time

from flask import Flask, jsonify, render_template, request

from monitor import disks, endurance, process_io, simulator, store, util
from monitor.context import MONITOR, ensure_started

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False


def _started():
    ensure_started()
    return MONITOR


@app.get("/")
def index():
    _started()
    return render_template("index.html")


@app.get("/api/health")
def api_health():
    return jsonify({"ok": True, "mode": MONITOR.mode, "ts": time.time()})


@app.get("/api/system")
def api_system():
    m = _started()
    return jsonify(m.system_info())


@app.get("/api/disks")
def api_disks():
    m = _started()
    ds, pending = m.current_disks()
    out = []
    for d in ds:
        io = m.io.snapshot()["disks"].get(d["id"])
        out.append({
            "id": d["id"], "name": d["name"], "model": d["model"], "family": d.get("family"),
            "serial": d.get("serial"), "firmware": d.get("firmware"),
            "type": d.get("type"), "bus": d.get("bus"), "size": d.get("size"),
            "spindle": d.get("spindle"), "health": d.get("health"),
            "mountpoints": d.get("mountpoints"),
            "smart_source": d.get("smart_source"), "smart_pending": d.get("smart_pending"),
            "smart_limited": (d.get("smart") or {}).get("source") == "powershell",
            "smart_hint": d.get("smart_hint"),
            "temperature": (d.get("smart") or {}).get("temperature"),
            "power_on_hours": (d.get("smart") or {}).get("power_on_hours"),
            "power_cycles": (d.get("smart") or {}).get("power_cycles"),
            "tbw": (d.get("smart") or {}).get("tbw"),
            "endurance": d.get("endurance"),
            "issues": (d.get("smart") or {}).get("issues") or [],
            "io": io,
            "io_session": m.io.session_summary().get(d["id"]),
            "demo": bool(d.get("demo")),
        })
    return jsonify({
        "ts": time.time(), "mode": m.mode, "disks": out,
        "pending": sorted(pending), "demo": m.mode == "sim",
    })


@app.get("/api/io")
def api_io():
    m = _started()
    snap = m.io.snapshot()
    snap["demo"] = m.mode == "sim"
    return jsonify(snap)


@app.get("/api/processes")
def api_processes():
    m = _started()
    snap = m.proc_io.snapshot()
    snap["demo"] = m.mode == "sim"
    try:
        disk_list = disks.sim_disks() if m.mode == "sim" else disks.get_disks()[0]
    except Exception:
        disk_list = []
    try:
        io_disks = (m.io.snapshot() or {}).get("disks") or {}
    except Exception:
        io_disks = {}
    snap["by_drive"] = process_io.by_drive(snap.get("processes") or [], disk_list, io_disks)
    return jsonify(snap)


@app.get("/api/io/history")
def api_io_history():
    m = _started()
    sec = max(30, min(1800, int(request.args.get("sec", 120))))
    return jsonify(m.io.history(seconds=sec))


@app.get("/api/smart/<disk_id>")
def api_smart(disk_id):
    m = _started()
    disk = None
    ds, _ = m.current_disks()
    for d in ds:
        if d["id"] == disk_id:
            disk = d
            break
    if disk is None and not disk_id.startswith("sim"):
        return jsonify({"error": "unknown disk %r" % disk_id}), 404
    s = disks.smart_for(disk_id, disk)
    _disk = disk or {"id": disk_id}
    endurance.assess(_disk, m.io.avg_write_bps())
    return jsonify({
        "ts": time.time(),
        "disk": {k: _disk.get(k) for k in ("id", "name", "model", "family", "serial",
                                           "firmware", "type", "bus", "size", "spindle",
                                           "health", "mountpoints")},
        "smart": s,
        "endurance": _disk.get("endurance"),
    })


@app.get("/api/history")
def api_history():
    m = _started()
    if m.mode == "sim":
        pts = simulator.seed_history_points()
        h = {"points": pts}
    else:
        h = store.load_history()
    series = {}
    for p in sorted(h.get("points", []), key=lambda x: x.get("ts", 0)):
        for serial, tbw in (p.get("d") or {}).items():
            series.setdefault(serial, []).append([p["ts"], tbw])
    names = {}
    for d in (m.current_disks()[0] if m.mode == "sim" else disks.get_disks()[0]):
        if d.get("serial"):
            names[d["serial"].upper()] = d.get("name") or d.get("model")
    return jsonify({
        "ts": time.time(),
        "series": {s: {"name": names.get(s.upper(), s), "points": v} for s, v in series.items()},
        "sim": m.mode == "sim",
    })


@app.get("/api/events")
def api_events():
    m = _started()
    return jsonify({"ts": time.time(), "events": m.events_recent()})


@app.post("/api/config")
def api_config():
    m = _started()
    body = request.get_json(silent=True) or {}
    if "rated_tbw" in body and "serial" in body:
        store.set_rated_tbw(body["serial"], body["rated_tbw"])
        disks.clear_caches()
        m.log("rated TBW for %s → %s TB" % (body["serial"], body["rated_tbw"]), "ok")
        return jsonify({"ok": True})
    if "mode" in body:
        eff = m.set_mode(body["mode"])
        return jsonify({"ok": True, "mode": eff})
    return jsonify({"error": "bad request"}), 400


@app.post("/api/scan")
def api_scan():
    m = _started()
    if m.mode == "sim":
        return jsonify({"ok": True, "mode": "sim"})
    disks.clear_caches()
    ds, pending = disks.get_disks(force=True)
    disks.warm_smart_background(ds)
    m.log("rescan: %d drive(s) detected" % len(ds), "ok")
    return jsonify({"ok": True, "count": len(ds)})


@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "not found"}), 404
    return render_template("index.html"), 404
