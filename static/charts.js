/* DISK://SENTRY chart engine — vanilla canvas, DPR-aware, offline-friendly */
"use strict";

const CH = (() => {

  function setup(cv) {
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth || cv.parentElement.clientWidth || 300;
    const h = cv.clientHeight || 60;
    cv.width = Math.max(10, Math.round(w * dpr));
    cv.height = Math.max(10, Math.round(h * dpr));
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }

  function niceMax(v) {
    if (!isFinite(v) || v <= 0) return 1;
    const exp = Math.pow(10, Math.floor(Math.log10(v)));
    const f = v / exp;
    const nf = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
    return nf * exp;
  }

  function fmtB(b) {
    if (b == null || !isFinite(b)) return "—";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0, v = b;
    while (v >= 1000 && i < u.length - 1) { v /= 1000; i++; }
    return (v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)) + " " + u[i];
  }

  function fmtBps(b) { return b == null ? "—" : fmtB(b) + "/s"; }

  /* ---------- sparkline (card tiles) ---------- */
  function spark(cv, data, color) {
    if (!cv || !data || !data.length) return;
    const { ctx, w, h } = setup(cv);
    const max = Math.max(1e-3, ...data);
    const step = w / Math.max(1, data.length - 1);
    const y = (v) => h - 4 - (v / max) * (h - 10);

    ctx.beginPath();
    ctx.moveTo(0, y(data[0]));
    data.forEach((v, i) => ctx.lineTo(i * step, y(v)));
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.6;
    ctx.shadowColor = color; ctx.shadowBlur = 7;
    ctx.stroke();
    ctx.shadowBlur = 0;

    ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
    const g = ctx.createLinearGradient(0, 0, 0, h);
    g.addColorStop(0, color + "33");
    g.addColorStop(1, color + "00");
    ctx.fillStyle = g;
    ctx.fill();
  }

  /* ---------- dual-series area chart (READ / WRITE) ---------- */
  function dual(cv, readArr, writeArr, opts = {}) {
    if (!cv) return;
    const { ctx, w, h } = setup(cv);
    const padL = 62, padR = 12, padT = 12, padB = 24;
    const iw = Math.max(10, w - padL - padR), ih = Math.max(10, h - padT - padB);
    const n = Math.max(readArr.length, writeArr.length, 2);
    const maxV = niceMax(Math.max(1e-3, ...readArr, ...writeArr) * 1.15);
    const X = (i) => padL + (i / (n - 1)) * iw;
    const Y = (v) => padT + ih - (v / maxV) * ih;

    // grid + y labels
    ctx.font = "10px " + MONO;
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    for (let i = 0; i <= 3; i++) {
      const v = (maxV / 3) * i, y = Y(v);
      ctx.strokeStyle = "rgba(42,82,56,.35)";
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
      ctx.fillStyle = "#517a63";
      ctx.fillText(fmtBps(v), padL - 8, y);
    }
    // x labels (relative seconds)
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    const span = opts.spanSec || n;
    for (let i = 0; i <= 4; i++) {
      const x = padL + (i / 4) * iw;
      const sec = Math.round(span - (span / 4) * i);
      ctx.fillStyle = "#517a63";
      ctx.fillText(sec >= 60 ? "-" + Math.round(sec / 60) + "m" : "-" + sec + "s", x, padT + ih + 8);
    }

    const series = (arr, color) => {
      if (!arr || arr.length < 2) return;
      const step = iw / Math.max(1, arr.length - 1);
      ctx.beginPath();
      arr.forEach((v, i) => (i ? ctx.lineTo : ctx.moveTo).call(ctx, padL + i * step, Y(v)));
      ctx.strokeStyle = color; ctx.lineWidth = 1.8;
      ctx.shadowColor = color; ctx.shadowBlur = 8;
      ctx.stroke(); ctx.shadowBlur = 0;
      ctx.lineTo(padL + (arr.length - 1) * step, padT + ih);
      ctx.lineTo(padL, padT + ih); ctx.closePath();
      const g = ctx.createLinearGradient(0, padT, 0, padT + ih);
      g.addColorStop(0, color + "2e"); g.addColorStop(1, color + "00");
      ctx.fillStyle = g; ctx.fill();
    };
    series(readArr, opts.readColor || "#22d3ee");
    series(writeArr, opts.writeColor || "#ffb020");
  }

  /* ---------- TBW history line ---------- */
  function tbwLine(cv, points, opts = {}) {
    if (!cv || !points || points.length < 2) return;
    const { ctx, w, h } = setup(cv);
    const padL = 70, padR = 14, padT = 12, padB = 26;
    const iw = Math.max(10, w - padL - padR), ih = Math.max(10, h - padT - padB);
    const color = opts.color || "#00ff66";
    let vmax = Math.max(...points.map(p => p[1]));
    let vmin = Math.min(...points.map(p => p[1]));
    if (vmax === vmin) { vmin = Math.max(0, vmin - 1); vmax = vmax + 1; }
    const pad = (vmax - vmin) * 0.1;
    vmax += pad; vmin = Math.max(0, vmin - pad);
    const t0 = points[0][0], t1 = points[points.length - 1][0] || t0 + 1;
    const X = (t) => padL + ((t - t0) / Math.max(1, t1 - t0)) * iw;
    const Y = (v) => padT + ih - ((v - vmin) / (vmax - vmin)) * ih;

    ctx.font = "10px " + MONO;
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    for (let i = 0; i <= 3; i++) {
      const v = vmin + ((vmax - vmin) / 3) * i, y = Y(v);
      ctx.strokeStyle = "rgba(42,82,56,.35)";
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
      ctx.fillStyle = "#517a63";
      ctx.fillText(fmtB(v), padL - 8, y);
    }
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    for (let i = 0; i <= 3; i++) {
      const t = t0 + ((t1 - t0) / 3) * i;
      const d = new Date(t * 1000);
      ctx.fillStyle = "#517a63";
      ctx.fillText(
        (t1 - t0) > 3 * 86400
          ? (d.getMonth() + 1) + "/" + d.getDate()
          : ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2),
        X(t), padT + ih + 8);
    }

    ctx.beginPath();
    points.forEach((p, i) => (i ? ctx.lineTo : ctx.moveTo).call(ctx, X(p[0]), Y(p[1])));
    ctx.strokeStyle = color; ctx.lineWidth = 2;
    ctx.shadowColor = color; ctx.shadowBlur = 9;
    ctx.stroke(); ctx.shadowBlur = 0;
    const last = points[points.length - 1];
    ctx.lineTo(X(t1), padT + ih); ctx.lineTo(X(t0), padT + ih); ctx.closePath();
    const g = ctx.createLinearGradient(0, padT, 0, padT + ih);
    g.addColorStop(0, color + "2a"); g.addColorStop(1, color + "00");
    ctx.fillStyle = g; ctx.fill();
    ctx.beginPath();
    ctx.arc(X(last[0]), Y(last[1]), 3.4, 0, 7);
    ctx.fillStyle = color; ctx.shadowColor = color; ctx.shadowBlur = 12;
    ctx.fill(); ctx.shadowBlur = 0;
  }

  return { spark, dual, tbwLine, fmtB, fmtBps, niceMax };
})();

const MONO = '"Cascadia Code","JetBrains Mono","Fira Code",Consolas,monospace';
