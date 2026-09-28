"""Persistent config (rated TBW overrides, mode) + TBW accrual history."""
import json
import os
import sys
import threading
import time

from . import util

_LOCK = threading.RLock()
_cache = {"config": None, "history": None}

RATED_TBW_HINTS = {
    # capacity GiB -> default rated TBW (0.3 DWPD / 5y consumer convention)
    128: 75, 256: 150, 512: 300, 1024: 600, 2048: 1200, 4096: 2400,
}


def data_dir():
    if util.IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    d = os.path.join(base, "DiskSentry")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(d, exist_ok=True)
    return d


def _config_path():
    return os.path.join(data_dir(), "config.json")


def _history_path():
    return os.path.join(data_dir(), "history.json")


def load_config():
    with _LOCK:
        if _cache["config"] is not None:
            return _cache["config"]
        cfg = {"rated_tbw": {}, "mode": "auto", "smart_bridges": {}}
        try:
            with open(_config_path()) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                cfg.update({k: v for k, v in loaded.items() if k in cfg})
        except Exception:
            pass
        _cache["config"] = cfg
        return cfg


def save_config(cfg):
    with _LOCK:
        _cache["config"] = cfg
        try:
            with open(_config_path(), "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass


def get_rated_tbw(serial):
    if not serial:
        return None
    v = load_config().get("rated_tbw", {}).get(serial.upper())
    return util.to_float(v)


def set_rated_tbw(serial, tbw_tb):
    serial = (serial or "").upper()
    if not serial:
        return
    with _LOCK:
        cfg = dict(load_config())
        rated = dict(cfg.get("rated_tbw", {}))
        val = util.to_float(tbw_tb)
        if val is None or val <= 0:
            rated.pop(serial, None)
        else:
            rated[serial] = round(val, 2)
        cfg["rated_tbw"] = rated
        _cache["config"] = cfg
        try:
            with open(_config_path(), "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass


def set_mode(mode):
    with _LOCK:
        cfg = dict(load_config())
        cfg["mode"] = mode if mode in ("auto", "live", "sim") else "auto"
        _cache["config"] = cfg
        try:
            with open(_config_path(), "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass


def save_bridge_hints(hints):
    """Merge {key: dialect | False} USB-bridge probe outcomes into the config.
    False marks an enclosure that ignores every pass-through dialect."""
    if not hints:
        return
    with _LOCK:
        cfg = dict(load_config())
        merged = dict(cfg.get("smart_bridges") or {})
        merged.update({str(k): v for k, v in hints.items()})
        cfg["smart_bridges"] = merged
        save_config(cfg)


def drop_bridge_hints(keys):
    """Forget probe verdicts for the given keys (RESCAN)."""
    with _LOCK:
        cfg = dict(load_config())
        merged = dict(cfg.get("smart_bridges") or {})
        changed = False
        for k in keys:
            if str(k) in merged:
                merged.pop(str(k), None)
                changed = True
        if changed:
            cfg["smart_bridges"] = merged
            save_config(cfg)


def load_history():
    with _LOCK:
        if _cache["history"] is not None:
            return _cache["history"]
        h = {"points": []}
        try:
            with open(_history_path()) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict) and isinstance(loaded.get("points"), list):
                h["points"] = [p for p in loaded["points"] if isinstance(p, dict)]
        except Exception:
            pass
        _cache["history"] = h
        return h


def append_history(serial_tbw_map, ts=None):
    """serial -> tbw bytes written (lifetime). Appends one point; prunes to 4000."""
    if not serial_tbw_map:
        return
    with _LOCK:
        h = _cache["history"] or {"points": []}
        point = {"ts": int(ts or time.time()), "d": {k.upper(): int(v) for k, v in serial_tbw_map.items() if v is not None}}
        h["points"].append(point)
        h["points"] = h["points"][-4000:]
        _cache["history"] = h
        try:
            with open(_history_path(), "w") as f:
                json.dump(h, f)
        except Exception:
            pass


def history_series():
    """Returns {serial: [(ts, tbw_bytes), ...]} sorted by ts."""
    pts = sorted(load_history()["points"], key=lambda p: p.get("ts", 0))
    series = {}
    for p in pts:
        for serial, tbw in (p.get("d") or {}).items():
            series.setdefault(serial, []).append((p["ts"], tbw))
    return series
