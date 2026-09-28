/* DISK://SENTRY — dashboard application
   vanilla JS · 1s polling · client ring buffers · slide-over detail panel */
"use strict";

/* ============================ helpers ============================ */
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtB(b) {
  if (b == null || !isFinite(b)) return "—";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0, v = Number(b);
  while (Math.abs(v) >= 1000 && i < u.length - 1) { v /= 1000; i++; }
  return (Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 10 ? v.toFixed(1) : v.toFixed(2)) + " " + u[i];
}
const fmtBps = (b) => (b == null ? "—" : fmtB(b) + "/s");
function fmtNum(n) {
  if (n == null || !isFinite(n)) return "—";
  return Number(n).toLocaleString("en-US");
}
function fmtPct(p) { return p == null ? "—" : p.toFixed(p < 10 ? 1 : 0) + "%"; }
function fmtPoh(h) {
  if (h == null) return "—";
  if (h < 48) return h.toFixed(0) + " h";
  return (h / 24 / 365).toFixed(h / 24 / 365 < 1 ? 2 : 1) + " y";
}
function fmtDays(d) {
  if (d == null) return "—";
  if (d > 3650) return "10+ y";
  if (d > 400) return (d / 365).toFixed(1) + " y";
  if (d > 48) return d.toFixed(0) + " d";
  return d.toFixed(1) + " d";
}
function fmtRate(r) {
  if (r == null) return "—";
  if (r >= 1e9) return (r / 1e9).toFixed(2) + " GB/s";
  if (r >= 1e6) return (r / 1e6).toFixed(1) + " MB/s";
  if (r >= 1e3) return (r / 1e3).toFixed(1) + " KB/s";
  return r.toFixed(0) + " B/s";
}
function timeStr(ts) {
  const d = new Date(ts * 1000);
  return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2) + ":" + ("0" + d.getSeconds()).slice(-2);
}

/* ============================ state ============================ */
const S = {
  sys: null, disks: [], mode: null, pending: new Set(),
  io: null,
  proc: null,             // process I/O payload (/api/processes)
  series: {},            // diskId -> {read:[], write:[]} (client ring, 90 pts)
  panelDisk: null,       // id of disk shown in detail panel
  smartCache: {},        // diskId -> smart detail payload
  panelRange: 60,        // io chart seconds
  lastRenderKey: "",
};

/* ============================ boot ============================ */
async function jget(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(url + " " + r.status);
  return r.json();
}
async function jpost(url, body) {
  const r = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return r.json().catch(() => ({}));
}

/* ============================ shell badge ============================ */
function initShellBadge() {
  // native desktop app window (pywebview/WebView2) vs plain web browser
  const isApp = new URLSearchParams(location.search).get("shell") === "app" ||
                typeof window.pywebview !== "undefined";
  const el = $("#host-badge");
  if (!el) return;
  el.textContent = isApp ? "APP" : "WEB";
  el.classList.toggle("is-app", isApp);
  el.title = isApp ? "running as a native desktop application (WebView2 shell)"
                   : "running in a web browser";
}

async function boot() {
  initShellBadge();
  bindUI();
  try {
    const [sys, disks, hist] = await Promise.all([
      jget("/api/system"), jget("/api/disks"), jget("/api/io/history?sec=90"),
    ]);
    S.sys = sys; S.mode = sys.mode;
    ingestHistory(hist);
    setDisks(disks);
    renderAll();
    startLoops();
  } catch (e) {
    $("#ticker").innerHTML = 'LINK ERROR <span class="blink">_</span>';
    toast("API unreachable — is the server running?");
  }
}

function ingestHistory(hist) {
  if (!hist || !hist.ids || !hist.t) return;
  for (const id of hist.ids) {
    const r = S.series[id] || (S.series[id] = { read: [], write: [] });
    r.read = hist.read[id] || [];
    r.write = hist.write[id] || [];
  }
}

function setDisks(payload) {
  S.disks = payload.disks || [];
  S.mode = payload.mode || S.mode;
  S.pending = new Set(payload.pending || []);
  for (const d of S.disks) {
    if (!S.series[d.id]) S.series[d.id] = { read: [], write: [] };
  }
}

/* ============================ polling loops ============================ */
function startLoops() {
  setInterval(pollIo, 1000);
  setInterval(pollDisks, 12000);
  setInterval(pollSys, 6000);
  setInterval(pollEvents, 8000);
  setInterval(pollProcesses, 2000);
  setInterval(() => { $("#clock").textContent = new Date().toLocaleTimeString(); }, 1000);
  pollEvents();
  pollProcesses();
  window.addEventListener("resize", debounce(redrawCharts, 180));
}

async function pollIo() {
  try {
    const io = await jget("/api/io");
    S.io = io;
    const now = Date.now() / 1000;
    for (const [id, v] of Object.entries(io.disks || {})) {
      const s = S.series[id] || (S.series[id] = { read: [], write: [] });
      s.read.push(v.read_bps || 0); s.write.push(v.write_bps || 0);
      if (s.read.length > 90) s.read.shift();
      if (s.write.length > 90) s.write.shift();
    }
    // aggregate throughput series for the overview tile sparkline
    S.agg = S.agg || { read: [], write: [] };
    const t = io.totals || {};
    S.agg.read.push(t.read_bps || 0); S.agg.write.push(t.write_bps || 0);
    if (S.agg.read.length > 90) S.agg.read.shift();
    if (S.agg.write.length > 90) S.agg.write.shift();
    if (!S.ioApplied) { S.ioApplied = true; renderTiles(); redrawCharts(); }
    S._tick = (S._tick || 0) + 1;
    if (S._tick % 2 === 0) redrawCharts();
    updateLive();
    updateTicker();
  } catch (e) { /* transient */ }
}

async function pollDisks() {
  try {
    const disks = await jget("/api/disks");
    setDisks(disks);
    renderDrives();
    renderTiles();
    if (S.panelDisk && $("#panel").classList.contains("open")) refreshPanelLive();
  } catch (e) { /* transient */ }
}

async function pollSys() {
  try { S.sys = await jget("/api/system"); } catch (e) { }
}

async function pollEvents() {
  try {
    const ev = await jget("/api/events");
    renderEvents(ev.events || []);
  } catch (e) { }
}

/* ============================ render: all ============================ */
function renderAll() {
  renderTiles();
  renderDrives();
  renderModeBadge();
  updateLive();
}

function renderModeBadge() {
  const b = $("#mode-badge");
  const sim = S.mode === "sim";
  b.textContent = sim ? "SIM MODE" : "LIVE";
  b.className = "mode-badge " + (sim ? "sim" : "live");
  b.title = sim
    ? "Simulated drives — switch to LIVE via the empty state or API"
    : "Real host telemetry via psutil + SMART";
}

/* ============================ render: tiles ============================ */
function renderTiles() {
  const disks = S.disks;
  const ssds = disks.filter((d) => d.type === "SSD" || d.type === "NVMe");
  const cap = disks.reduce((a, d) => a + (d.size || 0), 0);
  const tbw = ssds.reduce((a, d) => a + ((d.endurance || {}).written_bytes || 0), 0);
  const readT = ssds.reduce((a, d) => a + ((d.endurance || {}).read_bytes || 0), 0);
  const io = S.io || { totals: {} };
  const t = io.totals || {};
  let hot = null;
  for (const d of disks) {
    if (d.temperature != null && (!hot || d.temperature > hot.temperature)) hot = d;
  }
  const worst = disks.find((d) => d.health === "CRIT") || disks.find((d) => d.health === "WARN");
  const healthTxt = disks.length === 0 ? "—" :
    worst ? (worst.health === "CRIT" ? "ATTENTION" : "WARNINGS") : "ALL NOMINAL";

  const tiles = [
    { lbl: "DRIVES DETECTED", val: String(disks.length),
      sub: ssds.length + " SSD · " + (disks.length - ssds.length) + " HDD · " + fmtB(cap) + " raw" },
    { lbl: "AGGREGATE TBW", val: fmtB(tbw), cls: "",
      sub: "lifetime host writes · " + fmtB(readT) + " read" },
    { lbl: "LIVE READ", val: fmtRate(t.read_bps), cls: "cyan", spark: "read",
      sub: fmtNum(t.read_iops) + " IOPS across all drives" },
    { lbl: "LIVE WRITE", val: fmtRate(t.write_bps), cls: "amber", spark: "write",
      sub: fmtNum(t.write_iops) + " IOPS across all drives" },
    hot ? { lbl: "HOTTEST DRIVE", val: (hot.temperature != null ? hot.temperature + "°C" : "—"), cls: "",
        sub: (hot.name || hot.id) + (hot.temperature > 60 ? " · RUNNING HOT" : "") }
      : { lbl: "HOTTEST DRIVE", val: "—", sub: "no temperature data" },
    { lbl: "STORAGE HEALTH", val: healthTxt,
      cls: worst ? (worst.health === "CRIT" ? "amber" : "amber") : "",
      sub: worst ? (worst.name || worst.id) + " needs review" : "all drives report healthy" },
  ];
  $("#tiles").innerHTML = tiles.map((tl) => `
    <div class="tile">
      <div class="lbl">${esc(tl.lbl)}</div>
      <div class="val ${tl.cls || ""}">${esc(tl.val)}</div>
      ${tl.spark ? '<canvas class="spark-mini" data-agg="' + tl.spark + '"></canvas>' : ""}
      <div class="sub">${esc(tl.sub)}</div>
    </div>`).join("");
  redrawAggSparks();
}

function redrawAggSparks() {
  if (!S.agg) return;
  $$(".spark-mini").forEach((el) => {
    if (el.dataset.agg === "read") CH.spark(el, S.agg.read, "#22d3ee");
    if (el.dataset.agg === "write") CH.spark(el, S.agg.write, "#ffb020");
  });
}

/* ============================ render: drive cards ============================ */
function typeTag(d) {
  const t = (d.type || "unknown").toLowerCase();
  return `<span class="tag ${esc(t)}">${esc(d.type || "?")}</span>`;
}
function healthPill(d) {
  const h = (d.health || "UNKNOWN").toUpperCase();
  return `<span class="health-pill hp-${h.toLowerCase()}">${h === "OK" ? "HEALTHY" : h}</span>`;
}
function tempClass(t) { return t == null ? "" : t > 60 ? "t-hot" : "t-temp"; }

function renderDrives() {
  const grid = $("#drives");
  const disks = S.disks;
  $("#drive-count").textContent = disks.length ? disks.length + (disks.length === 1 ? " DRIVE" : " DRIVES") : "";
  $("#empty").hidden = disks.length > 0;

  const key = disks.map((d) => [d.id, d.health, d.name, d.serial, d.type, d.size,
    (d.endurance || {}).rated_tbw, (d.endurance || {}).written_tb, d.smart_pending,
    d.smart_limited].join("|")).join(";;");
  if (key === S.lastRenderKey) { updateLive(); return; }
  S.lastRenderKey = key;

  grid.innerHTML = disks.map(cardHtml).join("");
  $$(".drive-card").forEach((el) => {
    el.addEventListener("click", () => openPanel(el.dataset.id));
    el.addEventListener("keydown", (e) => { if (e.key === "Enter") openPanel(el.dataset.id); });
  });
  redrawCharts();
  redrawAggSparks();
  updateLive();
}

function cardHtml(d) {
  const e = d.endurance || {};
  const io = d.io || {};
  const sess = d.io_session || {};
  const rated = e.rated_tbw;
  const upct = e.used_pct_of_rated;
  const barCls = upct == null ? "" : upct >= 90 ? "crit" : upct >= 75 ? "warn" : "";
  const isSsd = d.type === "SSD" || d.type === "NVMe";
  const smartNote = d.smart_pending
    ? `<div class="chips"><span class="chip">SMART: reading…</span></div>`
    : (d.smart_hint
        ? `<div class="chips"><span class="chip warn">⚠ ${esc(d.smart_hint)}</span></div>`
        : (d.smart_limited
            ? `<div class="chips"><span class="chip warn">SMART: LIMITED · RELIABILITY COUNTERS</span></div>`
            : ""));
  const lifeChip = e.life_pct != null
    ? `<span class="chip ${e.life_pct <= 10 ? "crit" : e.life_pct <= 25 ? "warn" : "ok"}">LIFE <b>${fmtPct(e.life_pct)}</b></span>` : "";
  const dwpdChip = e.dwpd != null ? `<span class="chip">DWPD <b>${e.dwpd}</b></span>` : "";
  const projChip = e.days_to_rated != null
    ? `<span class="chip">EST <b>${fmtDays(e.days_to_rated)}</b> to rated @ current rate</span>` : "";
  const gbDay = e.gb_per_day_avg != null ? `<span class="chip">AVG <b>${fmtNum(e.gb_per_day_avg)}</b> GB/day</span>` : "";

  return `
  <article class="drive-card" data-id="${esc(d.id)}" tabindex="0" role="button"
           aria-label="Open full report for ${esc(d.name)}">
    <div class="dc-head">
      <div class="dc-head-txt">
        <div class="dc-name">${esc(d.name || d.id)}</div>
        <div class="dc-meta">
          ${d.serial ? "<span>S/N <b>" + esc(d.serial) + "</b></span>" : ""}
          ${d.firmware ? "<span>FW <b>" + esc(d.firmware) + "</b></span>" : ""}
          ${d.bus ? "<span>" + esc(d.bus) + "</span>" : ""}
          <span><b>${fmtB(d.size)}</b></span>
          ${d.mountpoints && d.mountpoints.length ? "<span>mounts <b>" + esc(d.mountpoints.join(" ")) + "</b></span>" : ""}
        </div>
      </div>
      ${typeTag(d)}
      ${healthPill(d)}
    </div>

    <hr class="hr"/>

    <div class="dc-duo">
      <div>
        <span class="lbl">${isSsd ? "HOST WRITES · TBW" : "LIFETIME WRITES"}</span>
        <div class="big">${e.written_bytes != null ? fmtB(e.written_bytes) : "—"}
          <span class="unit">${e.written_tb != null ? "(" + e.written_tb + " TB)" : ""}</span>
        </div>
        ${rated ? `
          <div class="bar ${barCls}"><i data-bar="${esc(d.id)}" style="width:${Math.min(100, upct || 0)}%"></i></div>
          <div class="bar-cap"><b>${fmtPct(upct)}</b> of <b>${rated} TB</b> rated
            · ${e.rated_source ? esc(e.rated_source) : ""}</div>` : `
          <div class="bar-cap" style="margin-top:14px">rated TBW unknown — open report to set it</div>`}
        <div class="chips">${lifeChip}${dwpdChip}${gbDay}${projChip}</div>
        ${smartNote}
      </div>
      <div>
        <div class="io-duo">
          <div>
            <span class="lbl">LIVE READ</span>
            <div class="io-big read" data-io="r-${esc(d.id)}">—</div>
            <div class="io-sub" data-io="ri-${esc(d.id)}">— IOPS</div>
          </div>
          <div>
            <span class="lbl">LIVE WRITE</span>
            <div class="io-big write" data-io="w-${esc(d.id)}">—</div>
            <div class="io-sub" data-io="wi-${esc(d.id)}">— IOPS</div>
          </div>
        </div>
        <canvas class="spark" data-spark="${esc(d.id)}" height="54"></canvas>
      </div>
    </div>

    <hr class="hr"/>

    <div class="dc-foot">
      <span>TEMP <b class="${tempClass(d.temperature)}">${d.temperature != null ? d.temperature + "°C" : "—"}</b></span>
      <span>POWER-ON <b>${fmtPoh(d.power_on_hours)}</b></span>
      <span>CYCLES <b>${fmtNum(d.power_cycles)}</b></span>
      <span>SESSION <b data-io="s-${esc(d.id)}">—</b></span>
      <span>BUSY <b data-io="b-${esc(d.id)}">—</b></span>
    </div>
    <button class="btn open-report">OPEN FULL REPORT →</button>
  </article>`;
}

/* live values patched in-place (no DOM rebuild at 1 Hz) */
function updateLive() {
  const io = S.io;
  if (!io) return;
  // live read/write tile values (refresh without full tile rebuild)
  const tiles = $("#tiles");
  if (tiles && tiles.children.length >= 4) {
    const t = io.totals || {};
    const setTile = (i, txt) => {
      const el = tiles.children[i] && tiles.children[i].querySelector(".val");
      if (el && el.textContent !== txt) el.textContent = txt;
    };
    setTile(2, fmtRate(t.read_bps));
    setTile(3, fmtRate(t.write_bps));
  }
  for (const d of S.disks) {
    const v = (io.disks || {})[d.id];
    const s = (io.session || {})[d.id];
    const set = (sel, txt) => {
      const el = document.querySelector(`[data-io="${sel}"]`);
      if (el && el.textContent !== txt) el.textContent = txt;
    };
    if (v) {
      set("r-" + d.id, fmtRate(v.read_bps));
      set("w-" + d.id, fmtRate(v.write_bps));
      set("ri-" + d.id, fmtNum(v.read_iops) + " IOPS");
      set("wi-" + d.id, fmtNum(v.write_iops) + " IOPS");
      set("b-" + d.id, fmtPct(v.busy_pct));
    }
    if (s) {
      set("s-" + d.id, "R " + fmtB(s.read) + " · W " + fmtB(s.write));
    }
  }
  if (S.panelDisk && $("#panel").classList.contains("open")) patchPanelLive();
}

function redrawCharts() {
  for (const el of $$(".spark")) {
    const id = el.dataset.spark;
    const s = S.series[id];
    if (s) CH.spark(el, s.read, "#22d3ee");
  }
  redrawAggSparks();
  if (S.panelDisk && $("#panel").classList.contains("open")) drawPanelCharts();
}

/* ============================ render: events ============================ */
function renderEvents(events) {
  const el = $("#events");
  el.innerHTML = events.length
    ? events.map((e) => `
        <div class="ev ${esc(e.sev || "info")}">
          <span class="ev-t">${timeStr(e.ts)}</span>
          <span class="ev-x">${esc(e.text)}</span>
        </div>`).join("")
    : '<div class="ev info"><span class="ev-t">' + new Date().toLocaleTimeString() +
      '</span><span class="ev-x">monitoring — no events yet</span></div>';
}

/* ============================ process I/O section ============================ */
/* view state for the top-writers table (filter / sort / top-N / grouping) */
const PV = { q: "", sort: "write_rate", top: 15, group: false, open: new Set() };

async function pollProcesses() {
  try {
    S.proc = await jget("/api/processes");
    renderProcesses();
  } catch (e) { /* transient */ }
}

const PROC_SORTS = {
  write_rate: (a, b) => (b.write_rate || 0) - (a.write_rate || 0),
  read_rate: (a, b) => (b.read_rate || 0) - (a.read_rate || 0),
  write_session: (a, b) => (b.write_session || 0) - (a.write_session || 0),
  write_total: (a, b) => (b.write_total || 0) - (a.write_total || 0),
  name: (a, b) => (a.name || "").localeCompare(b.name || ""),
};

function groupProcesses(rows) {
  const by = new Map();
  for (const r of rows) {
    const key = (r.name || "").toLowerCase();
    let g = by.get(key);
    if (!g) {
      g = { name: r.name, pid: 0, read_rate: 0, write_rate: 0,
            read_session: 0, write_session: 0, read_total: 0, write_total: 0, _n: 0,
            _drv: new Map(), _files: [], _hidden: false };
      by.set(key, g);
    }
    g._n += 1;
    g.read_rate += r.read_rate || 0;
    g.write_rate += r.write_rate || 0;
    g.read_session += r.read_session || 0;
    g.write_session += r.write_session || 0;
    g.read_total += r.read_total || 0;
    g.write_total += r.write_total || 0;
    for (const d of (r.drives || [])) {
      const dk = d.drive + "|" + (d.disk_id || "");
      const cur = g._drv.get(dk) || { drive: d.drive, disk_id: d.disk_id, name: d.name,
                                      type: d.type, bus: d.bus, wsum: 0 };
      cur.wsum += (r.write_rate || 0) * (d.share || 0);
      g._drv.set(dk, cur);
    }
    for (const f of (r.files || [])) {
      if (g._files.length < 8 && !g._files.includes(f)) g._files.push(f);
    }
    if (r.files_hidden) g._hidden = true;
  }
  for (const g of by.values()) {
    const list = Array.from(g._drv.values()).sort((a, b) => b.wsum - a.wsum);
    const tot = list.reduce((s, d) => s + d.wsum, 0);
    g.drives = list.map((d) => ({ drive: d.drive, disk_id: d.disk_id, name: d.name,
                                  type: d.type, bus: d.bus,
                                  share: tot > 0 ? Math.round(100 * d.wsum / tot) / 100 : 0 }));
    g.files = g._files;
    g.files_hidden = g._hidden;
    g.conf = list.length > 1 ? "est" : (list.length ? "live" : "none");
  }
  return Array.from(by.values());
}

function renderProcesses() {
  const data = S.proc;
  if (!data) return;
  const t = data.totals || {};
  const procs = data.processes || [];

  /* ---- stat strip ---- */
  const topW = procs.length ? procs[0] : null;
  $("#ps-top").textContent = topW ? (topW.name || "—") : "—";
  $("#ps-top").title = topW ? ("PID " + topW.pid) : "";
  $("#ps-top-sub").textContent = topW
    ? fmtRate(topW.write_rate) + " write · " + fmtRate(topW.read_rate) + " read"
    : "no process activity";
  $("#ps-wr").textContent = fmtRate(t.write_bps);
  $("#ps-wr-sub").textContent = "combined write rate · " + fmtNum(data.tracked) + " processes";
  $("#ps-rd").textContent = fmtRate(t.read_bps);
  $("#ps-rd-sub").textContent = "combined read rate · " + fmtNum(data.tracked) + " processes";
  $("#ps-n").textContent = fmtNum(data.tracked);
  const nsub = $("#ps-n-sub");
  if (data.restricted) {
    nsub.innerHTML = '<span class="warn">' + fmtNum(data.restricted) +
      " protected processes hidden — run as Administrator</span>";
  } else {
    nsub.textContent = data.admin ? "full visibility · elevated" : "user visibility · not elevated";
  }
  /* busiest target drive */
  const bd = data.by_drive || [];
  const bdTop = bd.find((b) => (b.est_write_bps || 0) > 0) || bd[0] || null;
  $("#ps-drv").textContent = bdTop
    ? (bdTop.drive !== "—" ? bdTop.drive : (bdTop.name || "—")) : "—";
  $("#ps-drv").title = bdTop ? (bdTop.name || "") : "";
  $("#ps-drv-sub").textContent = bdTop
    ? ((bdTop.name || "") + " · disk " + fmtRate(bdTop.write_bps) +
       " · programs " + fmtRate(bdTop.est_write_bps))
    : "no drive activity";
  $("#proc-count").textContent = fmtNum(data.tracked) + " TRACKED";
  const demo = $("#proc-demo");
  if (demo) demo.hidden = !data.demo;

  renderProcDrives(bd);

  /* ---- table ---- */
  const body = $("#proc-body");
  if (!body) return;
  if (data.warmup) {
    body.innerHTML = '<tr><td colspan="8" class="proc-wait">sampling process I/O' +
      '<span class="blink">_</span></td></tr>';
    return;
  }
  let rows = procs.slice();
  if (PV.group) rows = groupProcesses(rows);
  if (PV.q) {
    const q = PV.q.toLowerCase();
    rows = rows.filter((r) => (r.name || "").toLowerCase().includes(q));
  }
  rows.sort(PROC_SORTS[PV.sort] || PROC_SORTS.write_rate);
  if (PV.top > 0) rows = rows.slice(0, PV.top);
  const maxW = rows.reduce((m, r) => Math.max(m, r.write_rate || 0), 0);
  if (rows.length) {
    const html = [];
    rows.forEach((r, i) => {
      const key = r._n ? "g:" + (r.name || "").toLowerCase() : String(r.pid);
      html.push(procRowHtml(r, i, maxW, key));
      if (PV.open.has(key)) html.push(procDetailHtml(r));
    });
    body.innerHTML = html.join("");
  } else {
    body.innerHTML = '<tr><td colspan="8" class="proc-wait">no programs match "' + esc(PV.q) + '"</td></tr>';
  }
}

function drvClass(d) {
  if ((d.bus || "").toUpperCase() === "USB") return "t-usb";
  const t = (d.type || "").toUpperCase();
  if (t === "NVME") return "t-nvme";
  if (t === "SSD") return "t-ssd";
  return "t-hdd";
}

function drvTag(d) {
  if ((d.bus || "").toUpperCase() === "USB") return "USB";
  const t = (d.type || "").toUpperCase();
  return t || "DISK";
}

function driveChips(r) {
  const ds = r.drives || [];
  if (!ds.length) {
    return r.files_hidden
      ? '<span class="drv-none" title="file handles hidden — run DiskSentry as Administrator">hidden</span>'
      : '<span class="drv-none" title="no open files attributed yet">—</span>';
  }
  return ds.map((d) => {
    const tip = esc((d.name || "") + (d.bus ? " · " + d.bus : ""));
    const pct = ds.length > 1 && d.share ? "<i>" + Math.round(d.share * 100) + "%</i>" : "";
    return '<span class="drv-chip ' + drvClass(d) + '" title="' + tip + '">' +
      esc(d.drive) + "<b>" + esc(drvTag(d)) + "</b>" + pct + "</span>";
  }).join("");
}

function procRowHtml(r, i, maxW, key) {
  const w = r.write_rate || 0;
  const share = maxW > 0 ? Math.min(100, Math.max(w > 0 ? 2 : 0, (w / maxW) * 100)) : 0;
  const grouped = !!r._n;
  return `
  <tr class="prow${i === 0 ? " top1" : ""}" data-key="${esc(key)}" title="click for file paths & drive split">
    <td class="num rank">${String(i + 1).padStart(2, "0")}</td>
    <td class="pname">${esc(r.name || "—")}${grouped ? '<span class="inst">× ' + r._n + "</span>" : ""}</td>
    <td class="num pid">${grouped ? "× " + r._n : fmtNum(r.pid)}</td>
    <td class="num pr-rd">${fmtRate(r.read_rate)}</td>
    <td class="num pr-wr">${fmtRate(w)}
      <div class="pwbar"><i style="width:${share.toFixed(1)}%"></i></div>
    </td>
    <td class="pr-dr">${driveChips(r)}</td>
    <td class="num pr-sess"><b>W ${fmtB(r.write_session)}</b><span class="dim"> · R ${fmtB(r.read_session)}</span></td>
    <td class="num pr-tot">${fmtB(r.write_total)}</td>
  </tr>`;
}

const PD_CONF = {
  live: "open files · live",
  est: "open files · split",
  stale: "recent files",
  none: "no attribution",
};

function procDetailHtml(r) {
  const files = (r.files || []).slice(0, 8);
  const drives = (r.drives || []);
  const wr = r.write_rate || 0;
  let filesHtml = "";
  if (r.files_hidden && !files.length) {
    filesHtml = '<div class="pd-file dim">file handles hidden — run DiskSentry as Administrator</div>';
  } else if (!files.length) {
    filesHtml = '<div class="pd-file dim">no open files visible right now</div>';
  } else {
    filesHtml = files.map((f) => '<div class="pd-file" title="' + esc(f) + '">' + esc(f) + "</div>").join("");
  }
  let splitHtml = "";
  if (drives.length) {
    splitHtml = drives.map((d) => {
      const w = fmtRate(wr * (d.share || 0));
      return '<div class="pd-split"><span class="drv-chip ' + drvClass(d) + '">' + esc(d.drive) +
        "</span><span class=\"pd-disk\" title=\"" + esc(d.name || "") + "\">" + esc(d.name || d.disk_id || "") +
        "</span><span class=\"pd-w\">≈ " + w + "/s</span><span class=\"pd-share\">" +
        Math.round((d.share || 0) * 100) + "%</span></div>";
    }).join("");
  } else {
    splitHtml = '<div class="pd-file dim">no drive attribution yet</div>';
  }
  const conf = PD_CONF[r.conf] || "";
  return '<tr class="pdetail"><td colspan="8"><div class="pd-grid">' +
    '<div class="pd-col"><div class="pd-lbl">OPEN FILES</div>' + filesHtml + "</div>" +
    '<div class="pd-col"><div class="pd-lbl">DRIVE SPLIT · EST WRITE <span class="pd-conf">' + esc(conf) +
    "</span></div>" + splitHtml + "</div>" +
    "</div></td></tr>";
}

function renderProcDrives(bd) {
  const el = $("#pdrive");
  if (!el) return;
  if (!bd || !bd.length) {
    el.innerHTML = '<div class="db-wait">no drives detected</div>';
    return;
  }
  const maxW = Math.max(1, ...bd.map((b) => b.write_bps || 0));
  el.innerHTML = bd.map((b) => {
    const cls = drvClass(b);
    const tag = (b.bus || "").toUpperCase() === "USB"
      ? "USB · " + (b.type || "HDD")
      : ((b.type || "DISK") + (b.bus && b.bus.toUpperCase() !== (b.type || "").toUpperCase() ? " · " + b.bus : ""));
    const top = (b.top_writers || []).map((tw) => "<b>" + esc(tw.name) + "</b> " + fmtRate(tw.write_bps)).join(" · ");
    const bar = Math.max((b.write_bps || 0) / maxW * 100, (b.write_bps || 0) > 0 ? 2 : 0);
    return `
    <div class="db-row">
      <div class="db-top">
        <span class="db-drv ${cls}">${esc(b.drive)}</span>
        <span class="db-name">${esc(b.name || "")}</span>
        <span class="db-type">${esc(tag)}</span>
        <span class="db-rate">${fmtRate(b.write_bps)}</span>
        <span class="db-share">${b.share_pct || 0}% of all writes</span>
      </div>
      <div class="db-bar"><i style="width:${bar.toFixed(1)}%"></i></div>
      <div class="db-sub">est. process writes ${fmtRate(b.est_write_bps)}${top ? " · " + top : " · no attributed programs"}</div>
    </div>`;
  }).join("");
}

/* ============================ ticker ============================ */
function updateTicker() {
  const parts = [];
  if (S.mode === "sim") parts.push("SIMULATED TELEMETRY");
  else parts.push(S.sys && S.sys.smartctl ? "SMART: SMARTCTL LIVE" : "SMART: DEGRADED (no smartctl)");
  parts.push(S.disks.length + " DRIVES");
  parts.push((S.io && S.io.totals) ? "R " + fmtRate(S.io.totals.read_bps) + " · W " + fmtRate(S.io.totals.write_bps) : "");
  if (S.pending && S.pending.size) parts.push("reading SMART (" + S.pending.size + ")");
  if (S.sys && S.sys.cpu_pct != null) parts.push("CPU " + S.sys.cpu_pct.toFixed(0) + "% · RAM " + S.sys.ram_used_pct.toFixed(0) + "%");
  $("#ticker").innerHTML = esc(parts.filter(Boolean).join("  ·  ")) + ' <span class="blink">_</span>';
  $("#foot-info").textContent = "DiskSentry v1.3.1" +
    (S.sys ? " · " + S.sys.hostname + " · " + S.sys.os : "");
}

/* ============================ toast ============================ */
let toastTimer = null;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
}

/* ============================ ui bindings ============================ */
function bindUI() {
  $("#btn-scan").addEventListener("click", rescan);
  $("#btn-rescan2").addEventListener("click", rescan);
  $("#btn-sim").addEventListener("click", async () => {
    await jpost("/api/config", { mode: "sim" });
    toast("simulation mode enabled");
    S.lastRenderKey = "";
    await pollDisks(); renderModeBadge(); renderTiles();
  });
  $("#btn-close").addEventListener("click", closePanel);
  $("#backdrop").addEventListener("click", closePanel);
  $$("#tabs .tab").forEach((t) => t.addEventListener("click", () => selectTab(t.dataset.tab)));
  // process I/O tools (static markup — bind once)
  const pq = $("#proc-q");
  if (pq) pq.addEventListener("input", () => { PV.q = pq.value.trim(); renderProcesses(); });
  const psort = $("#proc-sort");
  if (psort) psort.addEventListener("change", () => { PV.sort = psort.value; renderProcesses(); });
  const ptop = $("#proc-top");
  if (ptop) ptop.addEventListener("change", () => { PV.top = parseInt(ptop.value, 10) || 0; renderProcesses(); });
  const pgrp = $("#proc-group");
  if (pgrp) pgrp.addEventListener("change", () => { PV.group = pgrp.checked; PV.open.clear(); renderProcesses(); });
  const pbody = $("#proc-body");
  if (pbody) pbody.addEventListener("click", (e) => {
    const tr = e.target.closest("tr.prow");
    if (!tr || !tr.dataset.key) return;
    if (PV.open.has(tr.dataset.key)) PV.open.delete(tr.dataset.key);
    else PV.open.add(tr.dataset.key);
    renderProcesses();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closePanel();
    const typing = /^(input|textarea|select)$/i.test(document.activeElement.tagName);
    if (!typing && (e.key === "r" || e.key === "R")) rescan();
  });
}

async function rescan() {
  toast("rescanning drives…");
  try {
    await jpost("/api/scan");
    S.lastRenderKey = "";
    await pollDisks();
    toast("rescan complete — " + S.disks.length + " drive(s)");
  } catch (e) { toast("rescan failed"); }
}

function debounce(fn, ms) {
  let t = null;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

boot();

/* ============================ detail panel ============================ */
async function openPanel(diskId) {
  const d = S.disks.find((x) => x.id === diskId);
  if (!d) return;
  S.panelDisk = diskId;
  $("#p-title").textContent = d.name || d.id;
  $("#p-sub").innerHTML =
    `${typeTag(d)} &nbsp; ${healthPill(d)} &nbsp; <span class="dim">
     ${d.bus ? esc(d.bus) + " · " : ""}${fmtB(d.size)}
     ${d.serial ? " · S/N " + esc(d.serial) : ""}</span>`;
  $("#panel").classList.add("open");
  $("#panel").setAttribute("aria-hidden", "false");
  const bd = $("#backdrop");
  bd.hidden = false;
  requestAnimationFrame(() => bd.classList.add("show"));
  selectTab("overview");
  renderPanelAll();
  // fetch full smart detail (may take a moment first time)
  try {
    const det = await jget("/api/smart/" + encodeURIComponent(diskId));
    S.smartCache[diskId] = det;
    if (S.panelDisk === diskId) renderPanelAll();
  } catch (e) {
    S.smartCache[diskId] = { error: true };
  }
}

function closePanel() {
  S.panelDisk = null;
  $("#panel").classList.remove("open");
  $("#panel").setAttribute("aria-hidden", "true");
  const bd = $("#backdrop");
  bd.classList.remove("show");
  setTimeout(() => { bd.hidden = true; }, 250);
}

function selectTab(name) {
  $$("#tabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".tabpane").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  renderPane(name);
}

function renderPane(name) {
  if (S.panelDisk == null) return;
  if (name === "overview") renderTabOverview();
  else if (name === "smart") renderTabSmart();
  else if (name === "endurance") renderTabEndurance();
  else if (name === "io") renderTabIo();
  else if (name === "identity") renderTabIdentity();
}

function diskNow() { return S.disks.find((x) => x.id === S.panelDisk); }
function smartNow() {
  const c = S.smartCache[S.panelDisk];
  return c && !c.error ? c : null;
}

function renderPanelAll() {
  renderTabOverview();
  renderTabSmart();
  renderTabEndurance();
  renderTabIo();
  renderTabIdentity();
  drawPanelCharts();
}

function refreshPanelLive() { renderTabOverview(); patchPanelLive(); }

function patchPanelLive() {
  const d = diskNow();
  if (!d) return;
  const v = (S.io.disks || {})[d.id];
  if (!v) return;
  const set = (id, txt) => { const el = $(id); if (el) el.textContent = txt; };
  set("#pl-read", fmtRate(v.read_bps));
  set("#pl-write", fmtRate(v.write_bps));
  set("#pl-read-cell", fmtRate(v.read_bps));
  set("#pl-write-cell", fmtRate(v.write_bps));
  set("#pl-riops", fmtNum(v.read_iops) + " IOPS");
  set("#pl-wiops", fmtNum(v.write_iops) + " IOPS");
  set("#pl-busy", fmtPct(v.busy_pct));
}

/* ---------- tab: overview ---------- */
function renderTabOverview() {
  const d = diskNow();
  if (!d) return;
  const det = smartNow();
  const s = det ? det.smart : null;
  const e = d.endurance || {};
  const io = (S.io.disks || {})[d.id] || {};
  const m = (lbl, val, cls) =>
    `<div class="mcell"><span class="lbl">${esc(lbl)}</span><div class="mval ${cls || ""}">${esc(val)}</div></div>`;
  /* raw variant: val is trusted markup we build ourselves (never user data) */
  const mr = (lbl, html, cls) =>
    `<div class="mcell"><span class="lbl">${esc(lbl)}</span><div class="mval ${cls || ""}">${html}</div></div>`;
  const issues = (s && s.issues) || d.issues || [];
  const rows = [
    m("HEALTH STATUS", d.health || "UNKNOWN", d.health === "OK" ? "ok" : d.health === "WARN" ? "warn" : d.health === "CRIT" ? "crit" : ""),
    m("TEMPERATURE", d.temperature != null ? d.temperature + " °C" : "—", d.temperature > 60 ? "warn" : "cyan"),
    m("TBW WRITTEN", e.written_bytes != null ? fmtB(e.written_bytes) : "—", ""),
    m("LIFE LEFT", fmtPct(e.life_pct), e.life_pct != null && e.life_pct <= 25 ? "warn" : "ok"),
    m("POWER-ON HOURS", fmtPoh(d.power_on_hours) + (d.power_on_hours ? " (" + fmtNum(d.power_on_hours) + " h)" : ""), ""),
    m("POWER CYCLES", fmtNum(d.power_cycles), ""),
    mr("LIVE READ", '<span id="pl-read-cell">—</span>', "cyan"),
    mr("LIVE WRITE", '<span id="pl-write-cell">—</span>', "amber"),
  ];
  $("#tab-overview").innerHTML = `
    <div class="mgrid">${rows.join("")}</div>
    <div class="chips" style="margin-top:20px">
      <span class="chip">BUSY <b id="pl-busy">—</b></span>
      <span class="chip">READ <b id="pl-riops">—</b></span>
      <span class="chip">WRITE <b id="pl-wiops">—</b></span>
      <span class="chip">UNSAFE SHUTDOWNS <b>${fmtNum(s && s.unsafe_shutdowns)}</b></span>
      <span class="chip">MEDIA ERRORS <b>${fmtNum(s && s.media_errors)}</b></span>
      <span class="chip">SMART ERRORS LOGGED <b>${fmtNum(s && s.error_log_count)}</b></span>
    </div>
    ${issues.length ? `<div class="issues"><span class="lbl">ATTENTION</span>${
      issues.map((i) => `<div class="issue ${esc(i.sev)}">${esc(i.text)}</div>`).join("")}</div>` : ""}
    <div class="chart-box" style="margin-top:24px">
      <div class="chart-title">THROUGHPUT · LAST 90s</div>
      <div class="legend"><span><i class="swatch r"></i>READ <b id="pl-read">—</b></span>
      <span><i class="swatch w"></i>WRITE <b id="pl-write">—</b></span></div>
      <canvas class="chart" id="p-chart-ov"></canvas>
    </div>`;
  patchPanelLive();
  drawPanelCharts();
}

/* ---------- tab: smart table ---------- */
function renderTabSmart() {
  const d = diskNow();
  const det = smartNow();
  const el = $("#tab-smart");
  if (!det) {
    el.innerHTML = `<div class="issue info">Reading SMART data<span class="blink">_</span></div>`;
    return;
  }
  const s = det.smart || {};
  if (s.source === "none" || !s.available) {
    /* server-side context-aware advice: which fix applies to THIS drive */
    const advice = s.fallback_advice ||
      "On Windows run DiskSentry as Administrator and install smartmontools (smartctl) for full attribute tables and TBW.";
    const src = s.source === "powershell" ? "SOURCE: POWERSHELL RELIABILITY COUNTERS" : "";
    el.innerHTML = `
      <div class="issue warn">⚠ ${esc((s.issues && s.issues[0] && s.issues[0].text) || "SMART not available")}</div>
      <div class="issue info">${esc(advice)}</div>
      ${src ? `<div class="issue info" style="opacity:.75">${src}</div>` : ""}
      ${s.attributes && s.attributes.length ? smartTableHtml(s) : ""}`;
    return;
  }
  if (s.nvme_log) {
    const L = s.nvme_log;
    const rows = [
      ["Critical Warning", fmtNum(L.critical_warning), L.critical_warning ? "crit" : "ok"],
      ["Temperature", L.temperature != null ? L.temperature + " °C" : "—", "cyan"],
      ["Available Spare", fmtNum(L.available_spare) + " %", (L.available_spare || 0) < 20 ? "warn" : "ok"],
      ["Spare Threshold", fmtNum(L.available_spare_threshold) + " %", ""],
      ["Percentage Used", fmtNum(L.percentage_used) + " %", (L.percentage_used || 0) > 75 ? "warn" : "ok"],
      ["Data Units Written", fmtNum(L.data_units_written), ""],
      ["Data Units Read", fmtNum(L.data_units_read), ""],
      ["Host Read Commands", fmtNum(L.host_reads), ""],
      ["Host Write Commands", fmtNum(L.host_writes), ""],
      ["Power Cycles", fmtNum(L.power_cycles), ""],
      ["Power-On Hours", fmtNum(L.power_on_hours), ""],
      ["Unsafe Shutdowns", fmtNum(L.unsafe_shutdowns), ""],
      ["Media / Integrity Errors", fmtNum(L.media_errors), L.media_errors ? "crit" : "ok"],
    ];
    el.innerHTML = `
      <div class="lbl" style="margin-bottom:14px">NVMe SMART / HEALTH INFORMATION LOG</div>
      <div class="mgrid">${rows.map((r) =>
        `<div class="mcell"><span class="lbl">${esc(r[0])}</span><div class="mval ${r[2]}">${esc(r[1])}</div></div>`).join("")}</div>
      ${s.self_test && s.self_test.status ? `<div class="issue info" style="margin-top:18px">SELF-TEST: ${esc(s.self_test.status)}</div>` : ""}`;
    return;
  }
  el.innerHTML = smartTableHtml(s);
}

function smartTableHtml(s) {
  return `
    <div class="smart-tools">
      <input type="text" id="smart-q" placeholder="filter attributes…" autocomplete="off"/>
      <label><input type="checkbox" id="smart-crit"/> critical only</label>
      <span class="lbl">${(s.attributes || []).length} ATTRIBUTES</span>
    </div>
    <div class="tbl-wrap">
      <table class="tbl">
        <thead><tr>
          <th>ID</th><th>ATTRIBUTE</th><th class="num">VALUE</th><th class="num">WORST</th>
          <th class="num">THRESH</th><th class="num">RAW</th><th>STATUS</th>
        </tr></thead>
        <tbody id="smart-body">
          ${(s.attributes || []).map((a) => `
            <tr class="row-${a.status === "fail" ? "fail" : a.status === "warn" ? "warn" : "ok"}" data-name="${esc((a.name || "").toLowerCase())}" data-crit="${a.status !== "ok"}">
              <td class="id">${esc(a.id_hex)}</td>
              <td class="attrname">${esc(a.name)}</td>
              <td class="num">${a.value != null ? a.value : "—"}</td>
              <td class="num">${a.worst != null ? a.worst : "—"}</td>
              <td class="num">${a.thresh != null ? a.thresh : "—"}</td>
              <td class="num raw">${esc(a.raw_string || "—")}</td>
              <td><span class="dot ${a.status === "ok" ? "ok" : a.status}"></span>${esc(a.status.toUpperCase())}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
}

/* smart table filter (event delegation, bound once) */
document.addEventListener("input", (e) => {
  if (e.target.id === "smart-q" || e.target.id === "smart-crit") {
    const q = ($("#smart-q") || {}).value || "";
    const crit = ($("#smart-crit") || {}).checked;
    $$("#smart-body tr").forEach((tr) => {
      const matchQ = !q || tr.dataset.name.includes(q.toLowerCase());
      const matchC = !crit || tr.dataset.crit === "true";
      tr.style.display = matchQ && matchC ? "" : "none";
    });
  }
});

/* ---------- tab: endurance ---------- */
async function renderTabEndurance() {
  const d = diskNow();
  if (!d) return;
  const e = d.endurance || {};
  const isSsd = d.type === "SSD" || d.type === "NVMe";
  const barCls = (e.used_pct_of_rated || 0) >= 90 ? "crit" : (e.used_pct_of_rated || 0) >= 75 ? "warn" : "";
  const rated = e.rated_tbw;
  $("#tab-endurance").innerHTML = `
    <div class="end-hero">
      <div class="mcell"><span class="lbl">HOST WRITES (TBW)</span>
        <div class="mval">${e.written_bytes != null ? fmtB(e.written_bytes) : "—"}</div>
        <div class="mval" style="font-size:13px;color:var(--txt-dim)">${e.written_tb != null ? e.written_tb + " TB decimal" : ""}</div></div>
      <div class="mcell"><span class="lbl">LIFETIME READS</span>
        <div class="mval cyan">${e.read_bytes != null ? fmtB(e.read_bytes) : "—"}</div>
        <div class="mval" style="font-size:13px;color:var(--txt-dim)">${e.read_tb != null ? e.read_tb + " TB decimal" : ""}</div></div>
      <div class="mcell"><span class="lbl">NAND LIFE LEFT</span>
        <div class="mval ${e.life_pct != null && e.life_pct <= 25 ? "warn" : "ok"}">${fmtPct(e.life_pct)}</div>
        <div class="mval" style="font-size:13px;color:var(--txt-dim)">${e.life_source ? esc(e.life_source) : ""}</div></div>
      <div class="mcell"><span class="lbl">DWPD (5y CONVENTION)</span>
        <div class="mval">${e.dwpd != null ? e.dwpd : "—"}</div>
        <div class="mval" style="font-size:13px;color:var(--txt-dim)">${e.gb_per_day_avg != null ? fmtNum(e.gb_per_day_avg) + " GB/day avg" : ""}</div></div>
    </div>
    ${rated ? `
    <div class="chart-box">
      <div class="chart-title">ENDURANCE BUDGET</div>
      <div class="big" style="font-size:22px">${fmtPct(e.used_pct_of_rated)} <span class="unit">of ${rated} TB rated consumed</span></div>
      <div class="bar ${barCls}" style="margin-top:16px"><i style="width:${Math.min(100, e.used_pct_of_rated || 0)}%"></i></div>
      <div class="bar-cap">rated source: <b>${esc(e.rated_source || "—")}</b>
        ${e.days_to_rated != null ? " · at the current burn rate this budget lasts ≈ <b>" + fmtDays(e.days_to_rated) + "</b>" : ""}</div>
    </div>` : ""}
    ${isSsd ? `
    <div class="rated-edit">
      <span class="lbl">RATED TBW FOR THIS DRIVE (TB):</span>
      <input type="number" min="1" step="1" id="rated-input" value="${rated || ""}" placeholder="e.g. 600"/>
      <button class="btn btn-primary" id="rated-save">SAVE</button>
      ${e.burn_write_bps ? '<span class="lbl">current burn: ' + fmtRate(e.burn_write_bps) + '</span>' : ""}
    </div>` : `<div class="issue info">TBW tracking applies to flash storage (SSD/NVMe). This drive is
      electromechanical — monitor the SMART table for reallocated/pending sectors instead.</div>`}
    <div class="chart-box">
      <div class="chart-title">TBW ACCRUAL · RECORDED HISTORY</div>
      <canvas class="chart tall" id="p-chart-tbw"></canvas>
    </div>`;
  const save = $("#rated-save");
  if (save) save.addEventListener("click", async () => {
    const val = parseFloat(($("#rated-input") || {}).value);
    if (!val || val <= 0) { toast("enter a valid TB value"); return; }
    await jpost("/api/config", { serial: d.serial || d.id, rated_tbw: val });
    toast("rated TBW saved (" + val + " TB)");
    S.lastRenderKey = "";
    await pollDisks();
    renderPanelAll();
  });
  drawTbwChart();
}

async function drawTbwChart() {
  const d = diskNow();
  if (!d || !d.serial) return;
  const key = (d.serial || "").toUpperCase();
  const now = Date.now();
  if (!S._tbwCache || S._tbwCache.key !== key || now - (S._tbwCache.ts || 0) > 30000) {
    try { S._tbwCache = { key, ts: now, data: await jget("/api/history") }; }
    catch (e) { return; }
  }
  const h = S._tbwCache.data;
  const ser = (h.series || {})[d.serial] || (h.series || {})[key];
  const cv = $("#p-chart-tbw");
  if (cv && ser && ser.points && ser.points.length > 1) {
    CH.tbwLine(cv, ser.points.slice(-400), { color: "#00ff66" });
  }
}

/* ---------- tab: io ---------- */
function renderTabIo() {
  const d = diskNow();
  if (!d) return;
  const sess = d.io_session || {};
  $("#tab-io").innerHTML = `
    <div class="range-picks">
      ${[60, 300, 1800].map((s) => `<button class="rpick ${S.panelRange === s ? "active" : ""}" data-range="${s}">${
        s === 60 ? "60s" : s === 300 ? "5m" : "30m"}</button>`).join("")}
    </div>
    <div class="chart-box">
      <div class="chart-title">THROUGHPUT HISTORY</div>
      <div class="legend"><span><i class="swatch r"></i>READ</span><span><i class="swatch w"></i>WRITE</span></div>
      <canvas class="chart tall" id="p-chart-io"></canvas>
    </div>
    <div class="end-hero" style="margin-top:20px">
      <div class="mcell"><span class="lbl">SESSION READ</span><div class="mval cyan">${fmtB(sess.read)}</div></div>
      <div class="mcell"><span class="lbl">SESSION WRITE</span><div class="mval amber">${fmtB(sess.write)}</div></div>
      <div class="mcell"><span class="lbl">SESSION READ IOPS</span><div class="mval">${fmtNum(sess.riops)}</div></div>
      <div class="mcell"><span class="lbl">SESSION WRITE IOPS</span><div class="mval">${fmtNum(sess.wiops)}</div></div>
    </div>`;
  $$("#tab-io .rpick").forEach((b) => b.addEventListener("click", async () => {
    S.panelRange = parseInt(b.dataset.range, 10);
    renderTabIo();
    await drawPanelIoChart(true);
  }));
  drawPanelIoChart(true);
}

async function drawPanelIoChart(force) {
  const d = diskNow();
  if (!d) return;
  const cv = $("#p-chart-io");
  if (!cv) return;
  if (S.panelRange <= 90) {
    const s = S.series[d.id];
    if (s && s.read && s.read.length > 1) CH.dual(cv, s.read, s.write, { spanSec: 90 });
    return;
  }
  const due = force || !S._ioFetchTs || (Date.now() - S._ioFetchTs) > 10000;
  if (!due) return;
  S._ioFetchTs = Date.now();
  try {
    const h = await jget("/api/io/history?sec=" + S.panelRange);
    if (S.panelDisk !== d.id) return;
    const r = (h.read || {})[d.id] || [], w = (h.write || {})[d.id] || [];
    CH.dual(cv, r, w, { spanSec: S.panelRange });
  } catch (e) { }
}

/* ---------- tab: identity ---------- */
function renderTabIdentity() {
  const d = diskNow();
  const det = smartNow();
  const s = det ? det.smart : {};
  if (!d) return;
  const kv = [
    ["MODEL", d.model || d.name],
    ["MODEL FAMILY", d.family || (s && s.family) || "—"],
    ["SERIAL NUMBER", d.serial || "—"],
    ["FIRMWARE", d.firmware || (s && s.firmware) || "—"],
    ["MEDIA TYPE", (d.type || "—") + (d.spindle ? " · " + d.spindle + " rpm" : "")],
    ["INTERFACE", d.bus || (s && s.protocol) || "—"],
    ["CAPACITY", fmtB(d.size || (s && s.capacity))],
    ["MOUNT POINTS", (d.mountpoints || []).join("  ") || "—"],
    ["DEVICE NODE", d.id],
    ["SMART SOURCE", (d.smart_pending ? "reading…" : (d.smart_source || "—"))],
    ["SMART DEVICE", s && (s.device || s.protocol)
      ? (s.device ? s.device + (s.protocol ? "  [" + s.protocol + "]" : "")
                  : s.protocol + (s.temperature != null ? " · temp via " + s.temp_source : ""))
      : "—"],
    ["SMART STATUS", s && s.passed != null ? (s.passed ? "PASSED" : "FAILED") : "—"],
  ];
  $("#tab-identity").innerHTML =
    `<div class="kv">${kv.map((r) => `<div class="k">${esc(r[0])}</div><div class="v">${esc(r[1])}</div>`).join("")}</div>` +
    ((d.smart_hint) ? `<div class="issue warn" style="margin-top:18px">⚠ ${esc(d.smart_hint)}</div>` : "");
}

/* ---------- panel charts ---------- */
function drawPanelCharts() {
  if (!S.panelDisk) return;
  const d = diskNow();
  const ov = $("#p-chart-ov");
  if (ov && d) {
    const s = S.series[d.id];
    if (s && s.read && s.read.length > 1) CH.dual(ov, s.read, s.write, { spanSec: 90 });
  }
  drawPanelIoChart(false);
  drawTbwChart();
}
