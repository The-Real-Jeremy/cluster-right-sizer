/* Hand-rolled SVG charts: no library, no CDN (the app must work air-gapped). */
(function () {
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs = {}, parent) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) n.setAttribute(k, v);
    if (parent) parent.appendChild(n);
    return n;
  };
  const tip = () => document.getElementById("tip");
  function showTip(html, x, y) {
    const t = tip(); t.innerHTML = html; t.hidden = false;
    const w = t.offsetWidth, h = t.offsetHeight;
    t.style.left = Math.min(window.innerWidth - w - 8, x + 14) + "px";
    t.style.top = Math.max(8, y - h - 10) + "px";
  }
  function hideTip() { tip().hidden = true; }

  const fmtTime = (ms, spanMs) => {
    const d = new Date(ms);
    if (spanMs <= 2 * 86400e3) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return d.toLocaleDateString([], { day: "numeric", month: "short" });
  };
  const fmtFull = (ms) => new Date(ms).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

  function niceMax(v) {
    if (!v || v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }

  /**
   * line(container, {series, height, yMax, yFmt, step, bands, hlines, xMin, xMax})
   * series: [{name, color, points: [[t_ms, value|null]], area, dash, width}]
   */
  function line(container, o) {
    container.innerHTML = "";
    const W = Math.max(320, container.clientWidth || 640), H = o.height || 220;
    const m = { l: 42, r: 14, t: 10, b: 26 };
    const pts = o.series.flatMap((s) => s.points.filter((p) => p[1] !== null && p[1] !== undefined));
    if (!pts.length) { container.innerHTML = '<div class="empty">No data points in this window.</div>'; return; }
    const xMin = o.xMin ?? Math.min(...pts.map((p) => p[0])), xMax = o.xMax ?? Math.max(...pts.map((p) => p[0]));
    const yMax = o.yMax ?? niceMax(Math.max(...pts.map((p) => p[1]), ...(o.hlines || []).map((h) => h.y)) * 1.08);
    const X = (t) => m.l + ((t - xMin) / Math.max(1, xMax - xMin)) * (W - m.l - m.r);
    const Y = (v) => H - m.b - (v / yMax) * (H - m.t - m.b);
    const svg = el("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, width: W, height: H }, container);
    const g = el("g", { class: "grid" }, svg);
    const yFmt = o.yFmt || ((v) => Math.round(v));
    for (let i = 0; i <= 4; i++) {
      const v = (yMax * i) / 4, y = Y(v);
      el("line", { x1: m.l, x2: W - m.r, y1: y, y2: y }, g);
      el("text", { x: m.l - 6, y: y + 4, "text-anchor": "end" }, svg).textContent = yFmt(v);
    }
    const span = xMax - xMin;
    const ticks = Math.min(7, Math.max(2, Math.floor((W - 80) / 110)));
    for (let i = 0; i <= ticks; i++) {
      const t = xMin + (span * i) / ticks;
      el("text", { x: X(t), y: H - 8, "text-anchor": i === 0 ? "start" : i === ticks ? "end" : "middle" }, svg).textContent = fmtTime(t, span);
    }
    for (const b of o.bands || []) {
      el("rect", { x: m.l, width: W - m.l - m.r, y: Y(b.y1), height: Math.max(1, Y(b.y0) - Y(b.y1)), fill: b.color, opacity: b.opacity ?? 0.5 }, svg);
      if (b.label) el("text", { x: W - m.r - 4, y: Y(b.y1) + 12, "text-anchor": "end", style: "fill:#475569" }, svg).textContent = b.label;
    }
    for (const h of o.hlines || []) {
      el("line", { x1: m.l, x2: W - m.r, y1: Y(h.y), y2: Y(h.y), stroke: h.color || "#94a3b8", "stroke-dasharray": "4 4", "stroke-width": 1.2 }, svg);
      if (h.label) el("text", { x: m.l + 6, y: Y(h.y) - 4, style: `fill:${h.color || "#64748b"};font-weight:600` }, svg).textContent = h.label;
    }
    const paths = [];
    for (const s of o.series) {
      let d = "", a = "", prev = null, open = false;
      const sp = s.points.slice().sort((p, q) => p[0] - q[0]);
      sp.forEach((p, i) => {
        if (p[1] === null || p[1] === undefined) { if (open && s.area) a += `L${X(prev[0])},${Y(0)}Z`; open = false; prev = null; return; }
        const x = X(p[0]), y = Y(p[1]);
        if (!open) { d += `M${x},${y}`; if (s.area) a += `M${x},${Y(0)}L${x},${y}`; open = true; }
        else if (o.step || s.step) { d += `H${x}V${y}`; if (s.area) a += `H${x}V${y}`; }
        else { d += `L${x},${y}`; if (s.area) a += `L${x},${y}`; }
        prev = p;
        if (i === sp.length - 1 && s.area) a += `L${x},${Y(0)}Z`;
      });
      if (s.area && a) el("path", { d: a, class: "a", fill: s.color }, svg);
      const path = el("path", { d, class: "l draw", stroke: s.color, "stroke-width": s.width || 1.8, "stroke-dasharray": s.dash }, svg);
      paths.push(path);
    }
    requestAnimationFrame(() => paths.forEach((p) => { if (!p.getAttribute("stroke-dasharray")) { const L = p.getTotalLength(); p.style.setProperty("--len", L); } else p.classList.remove("draw"); }));
    // hover crosshair
    const cross = el("line", { y1: m.t, y2: H - m.b, stroke: "#0f172a", "stroke-width": 1, opacity: 0 }, svg);
    const hit = el("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent" }, svg);
    hit.addEventListener("mousemove", (ev) => {
      const r = svg.getBoundingClientRect();
      const t = xMin + ((ev.clientX - r.left) * (W / r.width) - m.l) / (W - m.l - m.r) * span;
      cross.setAttribute("x1", X(t)); cross.setAttribute("x2", X(t)); cross.setAttribute("opacity", 0.25);
      const rows = o.series.map((s) => {
        let best = null;
        for (const p of s.points) { if (p[0] <= t) best = p; else break; }
        if (!o.step && !s.step) best = s.points.reduce((b, p) => (!b || Math.abs(p[0] - t) < Math.abs(b[0] - t) ? p : b), null);
        return best ? `<div><i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${s.color};margin-right:6px"></i>${s.name}: <b>${best[1] === null ? "stopped" : yFmt(best[1])}</b></div>` : "";
      }).join("");
      showTip(`<div style="margin-bottom:3px;color:#cbd5e1">${fmtFull(t)}</div>${rows}`, ev.clientX, ev.clientY);
    });
    hit.addEventListener("mouseleave", () => { cross.setAttribute("opacity", 0); hideTip(); });
  }

  const ramp = (v) => {
    // 0 → pale, 100 → deep. Blue-violet ramp; readable on white.
    const stops = [[0, [241, 245, 249]], [10, [219, 234, 254]], [30, [147, 197, 253]], [55, [59, 130, 246]], [80, [29, 78, 216]], [100, [30, 27, 75]]];
    let a = stops[0], b = stops[stops.length - 1];
    for (let i = 0; i < stops.length - 1; i++) if (v >= stops[i][0] && v <= stops[i + 1][0]) { a = stops[i]; b = stops[i + 1]; break; }
    const f = (v - a[0]) / Math.max(1, b[0] - a[0]);
    const c = a[1].map((x, i) => Math.round(x + (b[1][i] - x) * f));
    return `rgb(${c.join(",")})`;
  };

  /** heatmap(container, rows [{d, h, cpu, mem, minutes}], metric) — days down, hours across */
  function heatmap(container, rows, metric) {
    container.innerHTML = "";
    if (!rows.length) { container.innerHTML = '<div class="empty">No running minutes in this window.</div>'; return; }
    const days = [...new Set(rows.map((r) => r.d))].sort();
    const map = new Map(rows.map((r) => [r.d + "|" + r.h, r]));
    const grid = document.createElement("div");
    grid.className = "heat";
    grid.style.gridTemplateColumns = `78px repeat(24, 1fr)`;
    grid.appendChild(document.createElement("div"));
    for (let h = 0; h < 24; h++) { const x = document.createElement("div"); x.textContent = h % 3 === 0 ? String(h).padStart(2, "0") : ""; x.style.textAlign = "center"; grid.appendChild(x); }
    const maxDays = 45, shown = days.slice(-maxDays);
    for (const d of shown) {
      const lab = document.createElement("div");
      const dt = new Date(d + "T00:00:00");
      lab.textContent = dt.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
      grid.appendChild(lab);
      for (let h = 0; h < 24; h++) {
        const r = map.get(d + "|" + h), c = document.createElement("div");
        c.className = "cell";
        c.style.background = r ? ramp(r[metric]) : "#f8fafc";
        if (r) {
          c.addEventListener("mousemove", (ev) => showTip(`<b>${lab.textContent} ${String(h).padStart(2, "0")}:00</b><br>CPU ${r.cpu.toFixed(0)}% · memory ${r.mem.toFixed(0)}%<br>${r.minutes} node-minutes`, ev.clientX, ev.clientY));
          c.addEventListener("mouseleave", hideTip);
        }
        grid.appendChild(c);
      }
    }
    container.appendChild(grid);
    const leg = document.createElement("div");
    leg.className = "legend"; leg.style.marginTop = "10px";
    leg.innerHTML = `<span>${metric === "cpu" ? "CPU" : "Memory"} %</span>` + [0, 20, 40, 60, 80, 100].map((v) => `<span><i style="background:${ramp(v)}"></i>${v}</span>`).join("") + `<span><i style="background:#f8fafc;border:1px solid #e2e8f0"></i>not running</span>` + (days.length > maxDays ? `<span>last ${maxDays} of ${days.length} days</span>` : "");
    container.appendChild(leg);
  }

  /** bars(container, rows [{label, value}], {color, fmt}) */
  function bars(container, rows, o = {}) {
    container.innerHTML = "";
    if (!rows.length) { container.innerHTML = '<div class="empty">Nothing recorded in this window.</div>'; return; }
    const W = Math.max(320, container.clientWidth || 600), H = o.height || 150, m = { l: 36, r: 8, t: 8, b: 22 };
    const svg = el("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, width: W, height: H }, container);
    const max = niceMax(Math.max(...rows.map((r) => r.value)));
    const bw = (W - m.l - m.r) / rows.length;
    for (let i = 0; i <= 3; i++) {
      const y = H - m.b - ((H - m.t - m.b) * i) / 3;
      el("line", { x1: m.l, x2: W - m.r, y1: y, y2: y, stroke: "#eef2f7" }, svg);
      el("text", { x: m.l - 5, y: y + 4, "text-anchor": "end" }, svg).textContent = Math.round((max * i) / 3);
    }
    rows.forEach((r, i) => {
      const h = (r.value / max) * (H - m.t - m.b);
      const rect = el("rect", { x: m.l + i * bw + bw * 0.15, width: Math.max(1, bw * 0.7), y: H - m.b, height: 0, rx: 2, fill: o.color || "#3b82f6" }, svg);
      requestAnimationFrame(() => { rect.style.transition = `all .6s cubic-bezier(.2,.8,.2,1) ${i * 12}ms`; rect.setAttribute("y", H - m.b - h); rect.setAttribute("height", h); });
      rect.addEventListener("mousemove", (ev) => showTip(`<b>${r.label}</b><br>${o.fmt ? o.fmt(r) : r.value}`, ev.clientX, ev.clientY));
      rect.addEventListener("mouseleave", hideTip);
      if (rows.length <= 16 || i % Math.ceil(rows.length / 10) === 0) el("text", { x: m.l + i * bw + bw / 2, y: H - 6, "text-anchor": "middle" }, svg).textContent = r.short || r.label;
    });
  }

  window.Charts = { line, heatmap, bars, ramp, showTip, hideTip };
})();
