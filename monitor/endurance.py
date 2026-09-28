"""Endurance assessment: TBW, rated TBW, life %, DWPD, burn-rate projection."""
from . import store, util

RATED_DEFAULT = 0.3  # DWPD over 5 years convention when vendor spec unknown


def default_rated_tbw(capacity_bytes):
    if not capacity_bytes:
        return None
    gb = capacity_bytes / 1e9
    for cap, tbw in sorted(store.RATED_TBW_HINTS.items()):
        if gb <= cap * 1.05:
            return float(tbw)
    return round(gb / 1000.0 * RATED_DEFAULT * 5 * 365, 0)  # 0.3 DWPD * 5y


def assess(disk, avg_write_bps=None):
    """Attach endurance metrics to a disk dict (mutates + returns the endurance block)."""
    smart = disk.get("smart") or {}
    tbw = smart.get("tbw") or {}
    written = tbw.get("written_bytes")
    serial = disk.get("serial")
    cap = disk.get("size") or smart.get("capacity")
    rated_store = store.get_rated_tbw(serial)
    rated = rated_store
    rated_source = "user override" if rated_store else None

    if rated is None:
        sim_rated = (disk.get("endurance") or {}).get("rated_tbw")
        if sim_rated:
            rated = sim_rated
            rated_source = (disk.get("endurance") or {}).get("rated_source") or "vendor spec"
    if rated is None:
        # NVMe: back-calculate from percentage_used
        pct = smart.get("percentage_used")
        if written and pct:
            rated = round(written / 1e12 / (pct / 100.0), 1)
            rated_source = "derived from NAND wear %"
    if rated is None:
        # SATA: derive from life-left attribute (231 value etc.)
        life = _sata_life_left(smart)
        if written and life is not None and life < 100:
            rated = round(written / 1e12 / ((100.0 - life) / 100.0), 1)
            rated_source = "derived from life-left attr"
    if rated is None and disk.get("type") in ("SSD", "NVMe"):
        rated = default_rated_tbw(cap)
        rated_source = "estimated (0.3 DWPD/5y)"

    e = {
        "written_bytes": written,
        "read_bytes": tbw.get("read_bytes"),
        "rated_tbw": rated,
        "rated_source": rated_source,
        "written_tb": round(written / 1e12, 2) if written is not None else None,
        "read_tb": round(tbw.get("read_bytes", 0) / 1e12, 2) if tbw.get("read_bytes") is not None else None,
    }

    if written is not None and rated:
        e["used_pct_of_rated"] = round(100.0 * written / (rated * 1e12), 1)
    else:
        e["used_pct_of_rated"] = None

    # life %
    life = None
    if smart.get("percentage_used") is not None:
        life = max(0, 100 - smart["percentage_used"])
        e["life_source"] = "NAND wear indicator"
    else:
        sata_life = _sata_life_left(smart)
        if sata_life is not None:
            life = sata_life
            e["life_source"] = "SSD life-left attribute"
        elif e.get("used_pct_of_rated") is not None:
            life = max(0, round(100 - e["used_pct_of_rated"], 1))
            e["life_source"] = "vs rated TBW"
    e["life_pct"] = life

    # DWPD (5-year convention): TB written / (capacity TB * 1825)
    if written is not None and cap:
        cap_tb = cap / 1e12
        if cap_tb > 0:
            e["dwpd"] = round((written / 1e12) / (cap_tb * 1825.0), 3)
    # GB/day average over power-on time
    poh = smart.get("power_on_hours")
    if written is not None and poh and poh > 0:
        e["gb_per_day_avg"] = round(written / 1e9 / (poh / 24.0), 1)
        e["poh_days"] = round(poh / 24.0, 1)

    # burn rate projection
    if avg_write_bps is not None and avg_write_bps > 1024 and written is not None and rated:
        remaining = rated * 1e12 - written
        if remaining > 0:
            days = remaining / avg_write_bps / 86400.0
            e["burn_write_bps"] = round(avg_write_bps, 1)
            e["days_to_rated"] = round(days, 1)
            e["years_to_rated"] = round(days / 365.25, 1)

    # status
    status = "unknown"
    upct = e.get("used_pct_of_rated")
    lifep = e.get("life_pct")
    if disk.get("type") in ("HDD",) and written is None:
        status = "n/a"
    elif lifep is not None and lifep <= 10 or (upct is not None and upct >= 90):
        status = "crit"
    elif lifep is not None and lifep <= 25 or (upct is not None and upct >= 75):
        status = "warn"
    elif lifep is not None or upct is not None:
        status = "ok"
    e["status"] = status
    disk["endurance"] = e
    return e


def _sata_life_left(smart):
    for a in smart.get("attributes") or []:
        if a.get("id") in (231, 232) and a.get("value") is not None:
            return a["value"]
    return None
