/* Cluster Right-Sizer UI. Relative URLs only: it runs at "/" standalone and under "/apps/<name>/" in App-ception. */
(function () {
  const $ = (s, r = document) => r.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const DEMO_SETTING = document.body.dataset.demoSetting || "allowed";
  const S = { genieConv: {}, days: 30, demo: DEMO_SETTING === "default", data: null, sort: { k: "score", asc: false }, sel: null, detail: null, tab: "recs", q: "", onlyRecs: false };
  try { const saved = JSON.parse(sessionStorage.getItem("crs") || "{}"); Object.assign(S, { days: saved.days || S.days, demo: saved.demo ?? S.demo }); } catch (e) { /* ignore */ }
  S.asApp = new URLSearchParams(location.search).get("as") === "app";
  S.simulate = new URLSearchParams(location.search).get("simulate");
  const save = () => sessionStorage.setItem("crs", JSON.stringify({ days: S.days, demo: S.demo }));

  const num = (v, d = 0) => (v === null || v === undefined || Number.isNaN(v) ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }));
  const money = (v, cur) => (v === null || v === undefined ? "—" : (cur && cur !== "USD" ? cur + " " : "$") + Number(v).toLocaleString(undefined, v >= 1000 ? { maximumFractionDigits: 0 } : { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
  const pct = (v) => (v === null || v === undefined ? "—" : Math.round(v) + "%");
  const ts = (s) => { if (typeof s === "number") return s; if (!s) return null; const t = /[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : s + "Z"; return Date.parse(t); };
  const ago = (s) => { const t = ts(s); if (!t) return "—"; const d = (Date.now() - t) / 86400e3; return d < 1 ? "today" : d < 2 ? "yesterday" : Math.floor(d) + " days ago"; };

  async function api(path) {
    const sep = path.includes("?") ? "&" : "?";
    const r = await fetch(`api/${path}${sep}days=${S.days}${S.demo ? "&demo=1" : ""}${S.asApp ? "&as=app" : ""}${S.simulate ? "&simulate=" + S.simulate : ""}`, { credentials: "same-origin", headers: { Accept: "application/json" } });
    const ct = r.headers.get("content-type") || "";
    if (!ct.includes("json")) throw new Error(r.status === 401 || r.redirected ? "Your session expired: reload the page to sign in again." : `Unexpected response (${r.status}).`);
    const j = await r.json();
    if (!r.ok) throw new Error((j.error && (j.error.message || j.error)) || `HTTP ${r.status}`);
    return j;
  }

  // ── load ───────────────────────────────────────────────────────────────────────────────────────────
  async function load(refresh) {
    $("#refresh").classList.add("spin");
    $("#demoBanner").hidden = !S.demo;
    if (S.asApp && !$("#asAppBanner")) { const b = document.createElement("div"); b.id = "asAppBanner"; b.className = "banner demo"; b.innerHTML = "Diagnostic view: you are seeing exactly what the app's <b>own service principal</b> can see (a non-admin identity: only what it has been granted). Turn off with CRS_SP_DIAGNOSTICS." + (S.simulate ? " <b>System-table access is SIMULATED as missing</b> for this view." : ""); $("#access").before(b); }
    document.querySelectorAll("#sourceToggle button").forEach((b) => b.classList.toggle("on", (b.dataset.src === "demo") === S.demo));
    $("#days").value = String(S.days);
    if (!S.data || refresh === "switch") {
      $("#fleet tbody").innerHTML = "";
      const e = $("#fleetEmpty"); e.hidden = false;
      e.innerHTML = `<div class="loading"><span class="spinner"></span><h3>${S.demo ? "Building the synthetic demo…" : "Reading your clusters and system tables…"}</h3>${S.demo ? "" : "The first load can take up to a minute while the SQL warehouse starts; later loads are cached for three minutes."}</div>`;
      if (!S.data) $("#kpis").innerHTML = Array(5).fill('<div class="kpi"><div class="chip skel" style="width:60%;height:14px"></div><div class="chip skel" style="width:45%;height:28px;margin-top:10px"></div></div>').join("");
    }
    try {
      S.data = await api("overview" + (refresh === true ? "?refresh=1" : ""));
      $("#errBanner")?.remove();
      S.noAnim = refresh === "poll";
      if (refresh !== "poll") api("genie").then((g) => { S.genie = g; $("#genieTab").hidden = !g || g.state === "off"; }).catch(() => { S.genie = { state: "unavailable", reason: "Could not check Genie." }; });
      renderWho(); renderAccess(); if (refresh !== "poll" || S.data.panels.users.state !== "loading") renderKpis(); renderTable();
      clearTimeout(S.poll);
      if (S.data.panels.users.state === "loading" && (S.pollCount = (S.pollCount || 0) + 1) < 40) S.poll = setTimeout(() => load("poll"), 5000);
      else S.pollCount = 0;
      const ps = S.data.phase_a.pat || {};
      if (refresh === "poll") return;
      if (!S.demo && ["expired", "wrong_user"].includes(ps.state)) openPat(ps.message);
      else if (!S.demo && ps.state === "none" && !sessionStorage.getItem("crs-pat-asked")) { sessionStorage.setItem("crs-pat-asked", "1"); openPat(); }
      if (S.sel && refresh !== "poll") openCluster(S.sel, true);
    } catch (e) {
      showError(e.message);
    } finally {
      $("#refresh").classList.remove("spin");
    }
  }
  function showError(msg) {
    let b = $("#errBanner");
    if (!b) { b = document.createElement("div"); b.id = "errBanner"; b.className = "banner err"; $("#access").before(b); }
    b.textContent = msg;
  }

  function renderWho() {
    const v = S.data.viewer || {};
    const name = v.name || v.email || (v.obo ? "Signed in" : "Local / no user token");
    const init = (name.match(/[A-Za-z]/g) || ["?"]).slice(0, 2).join("").toUpperCase();
    $("#who").innerHTML = `<span class="av">${esc(init)}</span><span title="${esc(v.email || "")}">${esc(name)}</span>`;
  }

  // ── access chips ───────────────────────────────────────────────────────────────────────────────────
  function renderAccess() {
    const p = S.data.panels, a = S.data.access;
    const chip = (state, label, detail) => `<span class="chip ${state === "ok" || state === "empty" ? "ok" : state === "unknown" || state === "loading" ? "unknown" : "locked"}" title="${esc(detail || "")}"><span class="dot"></span><b>${label}</b> ${state === "ok" ? "" : state === "empty" ? "· no rows yet" : state === "loading" ? "· loading…" : state === "unknown" ? "· could not check" : "· locked"}</span>`;
    const st = (k) => (p[k].state === "unavailable" ? (p[k].access === "unknown" ? "unknown" : "locked") : p[k].state);
    const pa = S.data.phase_a, needPat = pa.state === "needs_pat";
    const clustersChip = needPat
      ? `<span class="chip action" id="patChip" title="${esc(p.clusters_api.why || "")}"><span class="dot"></span><b>Clusters &amp; events</b> · enter your token</span>`
      : chip(p.clusters_api.state === "ok" ? "ok" : "locked", "Clusters & events", p.clusters_api.state === "ok" ? (S.data.demo ? "synthetic" : `with your token (${pa.pat && pa.pat.user}) · ${pa.listed} all-purpose clusters you can see`) : p.clusters_api.why);
    $("#access").innerHTML =
      clustersChip +
      chip(st("utilisation"), "Utilisation", p.utilisation.why || "system.compute.node_timeline") +
      chip(st("cost"), "Cost", p.cost.why || "system.billing.usage") +
      chip(st("users"), "Users & run times", p.users.why || "system.access.audit") +
      `<span class="chip" title="${esc(a.warehouse.how || "")}">SQL warehouse: <b>${esc(a.warehouse.name || a.warehouse.id || "none")}</b></span>` +
      `<button class="link" id="howto">Your access &amp; what is missing</button>` +
      (!S.data.demo && pa.pat && pa.pat.state === "ok" ? `<button class="link" id="patManage">Token…</button>` : "");
    $("#howto").onclick = openHowto;
    if ($("#patChip")) $("#patChip").onclick = () => openPat();
    if ($("#patManage")) $("#patManage").onclick = () => openPat();
  }

  function openHowto() {
    const m = S.data.missing || {}, items = m.items || [];
    const lab = { ok: "you have it", missing: "missing", unknown: "could not check" };
    const pre = (txt) => `<pre><button type="button" class="copy" data-copy="${esc(txt)}">copy</button>${esc(txt)}</pre>`;
    let h = `<p>Everything here is checked <b>as you</b> (${esc(m.who || "")}). Clusters and events use your own token; the system-table panels run on your sign-in through a SQL warehouse, so each person sees what their own grants allow.</p>`;
    h += items.map((it) => `<div class="miss ${it.state}"><div class="h"><b>${esc(it.area)}</b><span class="st ${it.state === "ok" ? "ok" : it.state === "missing" ? "no" : "unk"}">${lab[it.state] || it.state}</span></div>
      ${it.unlocks && it.state !== "ok" ? `<div class="d">Unlocks: ${esc(it.unlocks)}</div>` : it.state === "ok" && it.detail ? `<div class="d">${esc(it.detail)}</div>` : ""}
      ${it.state !== "ok" && it.detail ? `<div class="d">${esc(it.detail)}</div>` : ""}
      ${it.fix ? `<div class="d"><b>Fix:</b> ${esc(it.fix)}</div>` : ""}
      ${it.grant ? `<div class="d">A metastore or account admin runs:</div>${pre(it.grant.join("\n"))}` : ""}
      ${it.api ? `<div class="d">Or with the Permissions API:</div>${pre(it.api_path + "\n" + it.api)}` : ""}</div>`).join("");
    const g = S.genie || {};
    if (!S.data.demo) h += `<div class="miss ${g.state === "ready" ? "ok" : g.state === "off" ? "ok" : "missing"}"><div class="h"><b>Ask Genie</b><span class="st ${g.state === "ready" ? "ok" : g.state === "off" ? "unk" : "no"}">${g.state === "ready" ? "you have it" : g.state === "off" ? "not set up" : "missing"}</span></div><div class="d">${esc(g.state === "ready" ? "Space " + (g.title || g.space_id) : g.reason || "")}</div>${g.fix ? `<div class="d"><b>Fix:</b> ${esc(g.fix)}</div>` : ""}</div>`;
    const un = m.unseen_clusters || [];
    h += `<h3 style="margin:16px 0 6px">Clusters you cannot see</h3>`;
    if (m.unseen_check === "ok" && !un.length) h += `<div class="miss ok"><div class="d">None: every all-purpose cluster used or changed in the last 30 days in this workspace is visible to your token.</div></div>`;
    else if (!un.length) h += `<div class="miss unknown"><div class="d">${esc(m.unseen_check || "")}</div></div>`;
    else {
      h += `<div class="d" style="margin-bottom:8px">${esc(m.unseen_check)} The owner (or a workspace admin) adds you on each: Compute → cluster → ⋮ → Permissions → <b>Can Attach To</b>.</div>`;
      h += `<table class="small-table"><thead><tr><th>Cluster</th><th>Owner</th><th>Grant (Permissions API)</th></tr></thead><tbody>${un.map((c) => `<tr><td><b>${esc(c.cluster_name || c.cluster_id)}</b><div class="note" style="margin:0">${esc(c.cluster_id)}</div></td><td>${esc(c.owner || "—")}</td><td>${pre(c.api_path + "\n" + c.api)}</td></tr>`).join("")}</tbody></table>`;
    }
    if ((m.event_errors || []).length) h += `<h3 style="margin:16px 0 6px">Clusters whose events could not be read</h3>` + m.event_errors.map((e) => `<div class="miss unknown"><div class="h"><b>${esc(e.name || e.cluster_id)}</b></div><div class="d">${esc(e.error)}</div></div>`).join("");
    if (m.verbose_audit) h += `<p class="note" style="margin-top:14px">${esc(m.verbose_audit)} node_timeline keeps 90 days; the cluster event log keeps 60.</p>`;
    $("#howtoBody").innerHTML = h;
    $("#howtoBody").querySelectorAll(".copy").forEach((b) => (b.onclick = (e) => { e.preventDefault(); navigator.clipboard?.writeText(b.dataset.copy); b.textContent = "copied"; setTimeout(() => (b.textContent = "copy"), 1200); }));
    $("#howtoDlg").showModal();
  }

  // ── the viewer's PAT ───────────────────────────────────────────────────────────────────────────────
  function openPat(msg) {
    const st = (S.data && S.data.phase_a && S.data.phase_a.pat) || {};
    $("#patMsg").textContent = msg || (st.state && st.state !== "ok" && st.state !== "none" ? st.message : "");
    $("#patErr").hidden = true; $("#patInput").value = "";
    $("#patForget").hidden = st.state !== "ok";
    $("#patMemNote").hidden = st.key_mode !== "memory";
    $("#patDlg").showModal(); setTimeout(() => $("#patInput").focus(), 50);
  }
  async function patCall(method, body) {
    const r = await fetch("api/pat", { method, credentials: "same-origin", headers: { "content-type": "application/json", "x-crs": "1" }, body: body ? JSON.stringify(body) : undefined });
    let j = {}; try { j = await r.json(); } catch (e) { /* non-JSON */ }
    if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
    return j;
  }
  $("#patForm").onsubmit = async (e) => {
    e.preventDefault();
    $("#patSave").disabled = true; $("#patErr").hidden = true;
    try { await patCall("POST", { pat: $("#patInput").value }); $("#patInput").value = ""; $("#patDlg").close(); load(true); }
    catch (err) { $("#patErr").textContent = err.message; $("#patErr").hidden = false; }
    finally { $("#patSave").disabled = false; }
  };
  $("#patCancel").onclick = () => $("#patDlg").close();
  $("#patForget").onclick = async () => { try { await patCall("DELETE"); } catch (e) { /* cleared anyway */ } $("#patDlg").close(); load(true); };

  // ── KPIs ───────────────────────────────────────────────────────────────────────────────────────────
  function countUp(node, to, fmt) {
    const t0 = performance.now(), from = 0, dur = 700;
    const step = (t) => { const f = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - f, 3); node.textContent = fmt(from + (to - from) * e); if (f < 1) requestAnimationFrame(step); };
    requestAnimationFrame(step);
  }
  function renderKpis() {
    const k = S.data.kpis, p = S.data.panels;
    const costOK = p.cost.state === "ok";
    const cur = (S.data.clusters.find((c) => c.currency) || {}).currency;
    const tiles = [
      { label: "All-purpose clusters", v: k.clusters, f: (x) => num(x), hint: `${k.with_recommendations} with recommendations` },
      { label: "Running hours", v: k.running_hours, f: (x) => num(x), hint: k.node_hours ? `${num(k.node_hours)} node-hours (driver + workers)` : "from the cluster event log" },
      costOK ? { label: `Cost · ${S.days} days`, v: k.list_cost, f: (x) => money(x, cur), hint: `${num(k.dbus)} DBUs at list price` }
             : { label: `Cost · ${S.days} days`, lock: p.cost.state === "empty" ? "No billing rows for these clusters yet (billing lags up to 12 h)." : "Needs SELECT on system.billing.usage" },
      { label: "Estimated saving", hero: true, v: costOK ? k.saving_cost : null, f: (x) => money(x, cur),
        hint: costOK ? `${num(k.saving_dbus)} DBUs · combined per cluster, never added` : "", lock: costOK ? null : "Shown in DBUs/cost once billing is readable; per-cluster savings are shown as a share" },
      p.users.state === "ok" ? { label: "Distinct users", v: k.users, f: (x) => num(x), hint: "ran or attached to a cluster" }
                             : { label: "Distinct users", lock: p.users.state === "loading" ? "Reading the audit log…" : p.users.state === "empty" ? "No notebook or job command events yet." : "Needs SELECT on system.access.audit" },
    ];
    $("#kpis").innerHTML = tiles.map((t, i) => `<div class="kpi ${t.hero ? "hero" : ""}"><div class="label">${esc(t.label)}</div>${t.lock && t.v == null ? `<div class="lock">🔒 ${esc(t.lock)}</div>` : `<div class="value" data-i="${i}">0</div><div class="hint">${esc(t.hint || "")}</div>`}</div>`).join("");
    tiles.forEach((t, i) => { const n = $(`.value[data-i="${i}"]`); if (n && t.v != null) countUp(n, t.v, t.f); });
  }

  // ── fleet table ────────────────────────────────────────────────────────────────────────────────────
  const scoreClass = (s) => (s == null ? "" : s >= 55 ? "s-bad" : s >= 30 ? "s-warn" : "s-good");
  function ring(s) {
    const r = 14, C = 2 * Math.PI * r, off = C * (1 - (s || 0) / 100);
    return `<span class="score ${scoreClass(s)}"><svg class="ring" viewBox="0 0 34 34"><circle class="bg" cx="17" cy="17" r="${r}"/><circle class="fg" cx="17" cy="17" r="${r}" stroke-dasharray="${C}" stroke-dashoffset="${C}" data-off="${off}"/></svg><span class="n">${s == null ? "—" : s}</span></span>`;
  }
  function wbar(c, maxAll) {
    if (c.single_node) return '<span class="tag">single node</span>';
    if (c.max_workers == null) return '<span class="na">—</span>';
    const W = 150, sc = (v) => (Math.min(v, maxAll) / maxAll) * W;
    const lo = c.min_workers ?? c.max_workers, hi = c.max_workers;
    const peak = c.workers_peak;
    return `<div class="wbar" title="configured ${lo}–${hi}${peak != null ? ", peak " + peak : ""}"><div class="track"></div><div class="range" style="left:${sc(lo)}px;width:${Math.max(3, sc(hi) - sc(lo))}px"></div>${peak != null ? `<div class="peak" style="left:${Math.max(0, sc(peak) - 1)}px"></div>` : ""}<span class="lbl">${lo === hi ? hi : lo + "–" + hi} · ${peak ?? "?"}</span></div>`;
  }
  function mini(cpu, mem, drv, locked) {
    if (locked) return `<span class="lockcell">🔒 locked</span>`;
    if (cpu == null && mem == null) return '<span class="na">no data</span>';
    return `<div class="mini ${drv ? "drv" : ""}"><div class="b"><i data-w="${Math.min(100, cpu || 0)}"></i></div><span>${pct(cpu)}</span><div class="b m"><i data-w="${Math.min(100, mem || 0)}"></i></div><span>${pct(mem)}</span></div>`;
  }
  function renderTable() {
    const p = S.data.panels, rows0 = S.data.clusters;
    const utilLocked = p.utilisation.state === "unavailable", costLocked = p.cost.state === "unavailable", usersLocked = p.users.state === "unavailable";
    const q = S.q.toLowerCase();
    let rows = rows0.filter((c) => (!S.onlyRecs || c.recommendations.length) && (!q || [c.name, c.owner, c.node_type, c.cluster_id].some((x) => (x || "").toLowerCase().includes(q))));
    const { k, asc } = S.sort;
    rows.sort((a, b) => { const x = a[k], y = b[k]; if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1; return (typeof x === "string" ? x.localeCompare(y) : x - y) * (asc ? 1 : -1); });
    document.querySelectorAll("#fleet th").forEach((th) => { th.classList.toggle("sorted", th.dataset.k === k); th.classList.toggle("asc", th.dataset.k === k && asc); });
    const maxAll = Math.max(4, ...rows0.map((c) => Math.max(c.max_workers || 0, c.workers_peak || 0)));
    $("#fleet tbody").innerHTML = rows.map((c, i) => `
      <tr data-key="${esc(c.key)}" class="${S.sel === c.key ? "sel" : ""} ${S.noAnim ? "poll" : ""}" style="animation-delay:${Math.min(i, 20) * 18}ms">
        <td><div class="cname" title="${esc(c.name)}">${esc(c.name)}</div><div class="csub">${c.state === "RUNNING" ? '<span class="tag run">running</span>' : ""}<span class="tag">${esc(c.node_type || "?")}</span>${c.recommendations.length ? `<span class="tag recs">${c.recommendations.length} rec${c.recommendations.length > 1 ? "s" : ""}</span>` : ""}${!S.data.demo && c.workspace_id && S.data.settings.workspace_ids.length > 1 ? `<span class="tag" title="workspace">${esc(c.workspace_id)}</span>` : ""}</div></td>
        <td>${wbar(c, maxAll)}</td>
        <td class="num">${c.autoterm == null ? "—" : c.autoterm === 0 ? '<span style="color:var(--bad);font-weight:600">off</span>' : c.autoterm + " min"}</td>
        <td class="num">${num(c.running_hours, 1)}</td>
        <td>${c.single_node && c.w_cpu_p95 == null ? '<span class="na">no workers</span>' : mini(c.w_cpu_p95, c.w_mem_p95, false, utilLocked)}</td>
        <td>${mini(c.d_cpu_p95, c.d_mem_p95, true, utilLocked)}</td>
        <td class="num">${usersLocked ? '<span class="lockcell">🔒</span>' : p.users.state === "loading" ? '<span class="spinner sm"></span>' : num(c.users)}</td>
        <td class="num">${costLocked ? '<span class="lockcell">🔒</span>' : c.list_cost != null ? money(c.list_cost, c.currency) : c.dbus != null ? num(c.dbus) + " DBU" : '<span class="na">—</span>'}</td>
        <td class="num">${ring(c.score)}</td>
      </tr>`).join("");
    const empty = $("#fleetEmpty");
    if (!rows.length) {
      empty.hidden = false;
      const pa = S.data.panels.clusters_api;
      empty.innerHTML = rows0.length ? "<h3>No cluster matches the filter.</h3>" :
        S.data.phase_a.state === "needs_pat" ? `<h3>Enter your token to see your clusters</h3><p>${esc(pa.why || "")}</p><p><button class="primary" id="emptyPat">Enter token</button></p>` :
        pa.state !== "ok" ? `<h3>Your token cannot read clusters</h3><p>${esc(pa.why || "")}</p>` :
        `<h3>No all-purpose clusters visible to you</h3><p>Your token sees ${S.data.phase_a.listed} all-purpose clusters${S.data.panels.utilisation.state === "ok" ? " and node_timeline has none for the configured workspaces" : ""}. CAN ATTACH TO on a cluster is enough${DEMO_SETTING !== "off" ? ", or switch to <b>Synthetic demo</b> to see the app with data" : ""}.</p>`;
      if ($("#emptyPat")) $("#emptyPat").onclick = () => openPat();
    } else empty.hidden = true;
    // Rows can come from system tables only (other workspaces, or a workspace with no classic compute): say so.
    let note = $("#fleetNote");
    const pa0 = S.data.phase_a, apiRows = rows0.filter((c) => (c.sources || []).includes("clusters_api")).length;
    const msg = !S.data.demo && pa0.state === "ok" && pa0.listed === 0 && rows0.length
      ? `Your token lists no all-purpose clusters in <b>this</b> workspace${S.data.settings.workspace_id ? " (" + esc(S.data.settings.workspace_id) + ")" : ""}. A serverless-only workspace has none (no classic compute). The rows below come from system tables for workspace ${esc(S.data.settings.workspace_ids.filter((w) => w !== S.data.settings.workspace_id).join(", ") || "-")}, so their events tab is empty: the Clusters API only answers for the workspace the app runs in.`
      : !S.data.demo && pa0.state === "needs_pat" && rows0.length
      ? "These rows come from system tables. Enter your token to add each cluster's configuration and event history (workers over time, restarts, idle shutdowns)."
      : "";
    if (msg) {
      if (!note) { note = document.createElement("div"); note.id = "fleetNote"; note.className = "infonote"; $(".fleet .card-head").after(note); }
      note.innerHTML = msg;
    } else if (note) note.remove();
    requestAnimationFrame(() => {
      document.querySelectorAll(".mini i").forEach((i) => (i.style.width = i.dataset.w + "%"));
      document.querySelectorAll(".ring .fg").forEach((c) => (c.style.strokeDashoffset = c.dataset.off));
    });
    document.querySelectorAll("#fleet tbody tr").forEach((tr) => (tr.onclick = () => openCluster(tr.dataset.key)));
  }

  // ── drawer ─────────────────────────────────────────────────────────────────────────────────────────
  async function openCluster(key, quiet) {
    if (S.sel !== key && !quiet) S.tab = "recs";   // a different cluster starts on its recommendations
    S.sel = key;
    document.querySelectorAll("#fleet tbody tr").forEach((tr) => tr.classList.toggle("sel", tr.dataset.key === key));
    const row = S.data.clusters.find((c) => c.key === key);
    $("#drawer").classList.add("on"); $("#scrim").classList.add("on"); $("#drawer").setAttribute("aria-hidden", "false");
    if (row) head(row);
    if (!quiet) $("#dBody").innerHTML = '<div class="empty">Loading cluster detail…</div>';
    try {
      const myKey = key;
      S.detail = await api("cluster?key=" + encodeURIComponent(key));
      S.detail.auditLoading = !S.demo && S.data.panels.users.state !== "unavailable";
      if (S.genie && S.genie.state === "ready" && !S.demo) api(`genie?name=${encodeURIComponent(S.detail.cluster.name || "")}&id=${encodeURIComponent(S.detail.cluster.cluster_id)}`).then((g) => { if (S.detail) { S.detail.genieQuestions = g.questions; if (S.tab === "genie") renderTab(); } }).catch(() => {});
      head(S.detail.cluster);
      renderTab();
      if (S.detail.auditLoading) {
        api("cluster?part=audit&key=" + encodeURIComponent(key)).then((a) => {
          if (S.sel !== myKey || !S.detail) return;
          Object.assign(S.detail.detail, a.detail);
          S.detail.cluster.users_detail = a.cluster.users_detail;
          S.detail.auditLoading = false;
          if (S.tab === "users") renderTab();
        }).catch((e) => { if (S.detail) { S.detail.auditLoading = false; S.detail.auditError = e.message; if (S.tab === "users") renderTab(); } });
      }
    } catch (e) {
      $("#dBody").innerHTML = `<div class="banner err">${esc(e.message)}</div>`;
    }
  }
  function closeDrawer() { $("#drawer").classList.remove("on"); $("#scrim").classList.remove("on"); $("#drawer").setAttribute("aria-hidden", "true"); S.sel = null; document.querySelectorAll("#fleet tbody tr.sel").forEach((t) => t.classList.remove("sel")); }
  function head(c) {
    $("#dEyebrow").textContent = `${c.cluster_id}${c.workspace_id ? " · workspace " + c.workspace_id : ""}`;
    $("#dTitle").textContent = c.name;
    const f = [
      c.node_type && `workers ${c.node_type}`, c.driver_node_type && c.driver_node_type !== c.node_type && `driver ${c.driver_node_type}`,
      c.max_workers != null && (c.autoscale ? `autoscale ${c.min_workers}–${c.max_workers}` : `${c.max_workers} worker${c.max_workers === 1 ? "" : "s"} fixed`),
      c.autoterm != null && (c.autoterm ? `auto-term ${c.autoterm} min` : "auto-term off"), c.access_mode && c.access_mode.replace("USER_ISOLATION", "standard").replace("SINGLE_USER", "dedicated").toLowerCase(),
      c.runtime, c.owner && `owner ${c.owner}`,
    ].filter(Boolean);
    $("#dFacts").innerHTML = f.map((x) => `<span class="tag">${esc(x)}</span>`).join("") + (c.score != null ? `<span class="tag" style="font-weight:700">oversize ${c.score}/100 · ${esc(c.score_basis)}</span>` : "");
    document.querySelectorAll("#dTabs button").forEach((b) => {
      const need = { util: "utilisation", heat: "utilisation", nodes: "utilisation", users: "users" }[b.dataset.t];
      const locked = need && S.data.panels[need].state === "unavailable";
      b.innerHTML = b.textContent.replace(/ 🔒$/, "") + (locked ? ' <span class="lk">🔒</span>' : "");
    });
  }
  function locked(panelKey, what) {
    const p = S.data.panels[panelKey];
    return `<div class="locked-panel"><h4>🔒 ${esc(what)} needs <code>${esc(p.table || "")}</code></h4>
      <div class="why">${esc(p.why || "")}</div>
      <p style="margin:10px 0 0">Ask a metastore or account admin for read access (these run as <b>you</b>, so each viewer needs their own grant):</p>
      <pre>${esc(S.data.access.grants.join("\n"))}</pre></div>`;
  }
  function renderTab() {
    document.querySelectorAll("#dTabs button").forEach((b) => b.classList.toggle("on", b.dataset.t === S.tab));
    const D = S.detail, c = D.cluster, body = $("#dBody");
    const P = S.data.panels;
    const html = {
      recs: () => {
        const recs = c.recommendations || [], notes = c.notes || [];
        const sv = c.saving || {};
        let h = "";
        if (sv.fraction > 0) h += `<div class="box" style="display:flex;gap:18px;align-items:center;margin-bottom:12px"><div><div class="eyebrow">Estimated saving if all applied</div><div style="font-size:26px;font-weight:700;color:var(--good)">${Math.round(sv.fraction * 100)}%${sv.list_cost ? ` · ${money(sv.list_cost, c.currency)}` : ""}</div></div><div class="note" style="margin:0">${sv.dbus ? num(sv.dbus) + " DBUs" : ""}${sv.node_hours ? (sv.dbus ? " · " : "") + num(sv.node_hours) + " node-hours" : ""} over ${S.days} days. Savings are combined, not added, because they overlap. An estimate: re-measure after each change.</div></div>`;
        if (!recs.length && !notes.length) h += `<div class="allgood">✓ <div><b>Nothing to change on this evidence.</b><div>${c.score_basis === "configuration and events only" ? "Only configuration and events were readable; utilisation from system tables may still show headroom." : "Utilisation and configuration both look proportionate."}</div></div></div>`;
        h += recs.map((r, i) => `<div class="rec ${r.severity}" style="animation-delay:${i * 50}ms"><div class="bar"></div><div><h4>${esc(r.title)}</h4><p>${esc(r.detail)}</p><div class="ev">${(r.evidence || []).map((e) => `<span>${esc(e)}</span>`).join("")}</div></div><div class="side">${r.saving && r.saving.fraction ? `<div class="save">−${Math.round(r.saving.fraction * 100)}%</div>` : `<div class="basis">${esc((r.saving || {}).note || "")}</div>`}<div class="basis">${esc(r.severity)} · ${esc(r.basis)}</div></div></div>`).join("");
        h += notes.map((n) => `<div class="rec ${n.kind}"><div class="bar"></div><div><h4>${esc(n.title)}</h4><p>${esc(n.detail)}</p><div class="ev">${(n.evidence || []).map((e) => `<span>${esc(e)}</span>`).join("")}</div></div><div class="side"><div class="basis">${n.kind === "keep" ? "keep as is" : "check"}</div></div></div>`).join("");
        if (P.utilisation.state === "unavailable") h += `<div class="note">Utilisation-based advice (smaller node types, driver size) appears once you can read <code>system.compute.node_timeline</code>.</div>`;
        return h;
      },
      workers: () => {
        const s = D.worker_series || [];
        if (!s.length) return `<div class="empty"><h3>No event history</h3>${c.sources.includes("clusters_api") ? "The cluster has no events in the last 60 days." : "This cluster is known only from system tables; the event log is read through the Clusters API in its own workspace."}</div>`;
        const pa = D.phase_a || {};
        return `<div class="legend"><span><i style="background:#1d4ed8"></i>workers running</span><span><i style="background:#c7d7fe"></i>configured range</span><span>gaps = terminated</span></div><div id="cWorkers"></div>
          <div class="grid2" style="margin-top:14px"><div class="box"><h3>Size</h3><dl class="kv">
            <dt>Configured</dt><dd>${c.autoscale ? `autoscale ${c.min_workers}–${c.max_workers}` : `${c.max_workers} fixed`}</dd>
            <dt>Peak / p95 / median</dt><dd>${num(pa.workers_peak)} / ${num(pa.workers_p95)} / ${num(pa.workers_p50)} workers</dd>
            <dt>Average while running</dt><dd>${num(pa.workers_avg, 1)} workers</dd>
            <dt>Node sizes</dt><dd>worker ${num(pa.worker_cores)} cores / ${num(pa.worker_memory_gb, 1)} GB · driver ${num(pa.driver_cores)} cores / ${num(pa.driver_memory_gb, 1)} GB</dd></dl></div>
          <div class="box"><h3>Lifecycle (${num(pa.window_days)} days)</h3><dl class="kv">
            <dt>Running</dt><dd>${num(pa.running_hours, 1)} h (${Math.round((pa.running_share || 0) * 100)}% of the window) · ${num(pa.node_hours, 1)} node-hours</dd>
            <dt>Active days</dt><dd>${num(pa.active_days)} · ${num(pa.avg_running_hours_per_active_day, 1)} h per active day</dd>
            <dt>Starts / restarts</dt><dd>${num(pa.starts)} / ${num(pa.restarts)}</dd>
            <dt>Resizes</dt><dd>${num(pa.resizes)} (${num(pa.upsizes)} upsizes completed)</dd>
            <dt>Terminations</dt><dd>${Object.entries(pa.terminations || {}).map(([k, v]) => `${v} × ${k.toLowerCase()}`).join(", ") || "none"}</dd></dl></div></div>`;
      },
      util: () => {
        if (P.utilisation.state === "unavailable") return locked("utilisation", "Per-node CPU and memory");
        const rows = (D.detail.timeseries || {}).rows || [];
        if ((D.detail.timeseries || {}).error) return `<div class="banner err">${esc(D.detail.timeseries.error.message)}</div>`;
        if (!rows.length) return `<div class="empty"><h3>No node_timeline rows for this cluster in ${S.days} days</h3>Rows arrive about an hour after the minute they describe; nodes that ran under 10 minutes may be missing.</div>`;
        const w = D.util.workers || {}, d = D.util.driver || {};
        return `<div class="row"><div class="legend" style="margin:0"><span><i style="background:var(--workers)"></i>workers</span><span><i style="background:var(--driver)"></i>driver</span></div>
            <div class="subtoggle" id="utilMode"><button data-m="together" class="on">Together</button><button data-m="separate">Separately</button></div></div>
          <div id="utilCharts"></div>
          <div class="grid2" style="margin-top:14px">${[["Workers", w, "var(--workers)"], ["Driver", d, "var(--driver)"]].map(([n, x, col]) => `<div class="box"><h3 style="color:${col}">${n}</h3>${x.node_minutes ? `<dl class="kv">
            <dt>CPU avg / p95 / max</dt><dd>${pct(x.cpu_avg)} / <b>${pct(x.cpu_p95)}</b> / ${pct(x.cpu_max)}</dd>
            <dt>Memory avg / p95 / max</dt><dd>${pct(x.mem_avg)} / <b>${pct(x.mem_p95)}</b> / ${pct(x.mem_max)}</dd>
            <dt>I/O wait avg</dt><dd>${pct(x.wait_avg)}</dd><dt>Swap max</dt><dd>${pct(x.swap_max)}</dd>
            <dt>Idle (&lt;5% CPU)</dt><dd>${x.node_minutes ? Math.round((x.idle_minutes / x.node_minutes) * 100) + "% of " + num(x.node_minutes) + " node-minutes" : "—"}</dd></dl>` : '<div class="note">No rows.</div>'}</div>`).join("")}</div>`;
      },
      heat: () => {
        if (P.utilisation.state === "unavailable") return locked("utilisation", "The utilisation heatmap");
        return `<div class="row"><h3 style="margin:0">When is it busy? <span class="note">(hour of day, ${esc(D.timezone)})</span></h3>
          <div class="subtoggle" id="heatRole"><button data-r="workers" class="on">Workers</button><button data-r="driver">Driver</button></div>
          <div class="subtoggle" id="heatMetric" style="margin-left:6px"><button data-m="cpu" class="on">CPU</button><button data-m="mem">Memory</button></div></div><div id="heat"></div>`;
      },
      nodes: () => {
        if (P.utilisation.state === "unavailable") return locked("utilisation", "Per-node figures");
        const rows = (D.detail.nodes || {}).rows || [];
        if (!rows.length) return '<div class="empty">No nodes recorded in this window.</div>';
        return `<div class="note">Each VM the cluster ran on in the window (newest first, up to 200). Outliers point at skew rather than size.</div>
          <table class="small-table"><thead><tr><th>Node</th><th>Role</th><th>Type</th><th class="num">Minutes</th><th class="num">CPU avg</th><th class="num">CPU p95</th><th class="num">Mem avg</th><th class="num">Mem p95</th><th class="num">I/O wait</th><th class="num">Swap max</th></tr></thead><tbody>
          ${rows.map((n) => `<tr><td class="mono">${esc(String(n.instance_id).slice(0, 22))}</td><td>${n.driver ? '<span class="pill drv">driver</span>' : '<span class="pill">worker</span>'}</td><td>${esc(n.node_type)}</td><td class="num">${num(n.minutes)}</td><td class="num">${pct(n.cpu_avg)}</td><td class="num"><b>${pct(n.cpu_p95)}</b></td><td class="num">${pct(n.mem_avg)}</td><td class="num"><b>${pct(n.mem_p95)}</b></td><td class="num">${pct(n.wait_avg)}</td><td class="num">${pct(n.swap_max)}</td></tr>`).join("")}</tbody></table>`;
      },
      users: () => {
        if (P.users.state === "unavailable") return locked("users", "Who uses this cluster");
        if (D.auditLoading) return `<div class="empty loading"><span class="spinner"></span><h3>Reading the audit log for this cluster…</h3>Per-cell run times join two audit events; on a large account this takes up to a minute.</div>`;
        if (D.auditError) return `<div class="banner err">${esc(D.auditError)}</div>`;
        const users = c.users_detail || [];
        const lc = (D.detail.long_commands || {}).rows || [];
        if (!users.length) return `<div class="empty"><h3>No notebook or job commands recorded on this cluster</h3>Audit events arrive with a delay; per-cell run times need Verbose Audit Logs (workspace admin → Advanced).</div>`;
        return `<div class="box"><h3>Commands per day</h3><div id="daily"></div></div>
          <div class="box"><h3>Who uses it (${users.filter((u) => u.email).length})</h3><table class="small-table"><thead><tr><th>User</th><th>Via</th><th class="num">Commands</th><th class="num">Run time total</th><th class="num">p50</th><th class="num">p95</th><th class="num">Longest</th><th class="num">Failed</th><th>Last seen</th></tr></thead><tbody>
          ${users.map((u) => `<tr><td>${esc(u.email || "—")}</td><td><span class="pill ${u.src === "job" ? "job" : ""}">${esc(u.src)}</span></td><td class="num">${num(u.commands)}</td><td class="num">${u.secs_total != null ? num(u.secs_total / 3600, 1) + " h" : "—"}</td><td class="num">${u.secs_p50 != null ? num(u.secs_p50, 1) + " s" : "—"}</td><td class="num">${u.secs_p95 != null ? num(u.secs_p95, 0) + " s" : "—"}</td><td class="num">${u.secs_max != null ? num(u.secs_max / 60, 1) + " min" : "—"}</td><td class="num">${num(u.failed)}</td><td>${ago(u.last_seen)}</td></tr>`).join("")}</tbody></table>
          <div class="note">Run times come from per-cell <code>runCommand</code> audit events (verbose audit logs). Who used the cluster is measured; how much CPU or memory each person used is not, so per-user cost would be an estimate.</div></div>
          ${lc.length ? `<div class="box"><h3>Longest commands</h3><table class="small-table"><thead><tr><th>User</th><th>Via</th><th>When</th><th class="num">Duration</th><th>Status</th></tr></thead><tbody>${lc.map((r) => `<tr><td>${esc(r.email)}</td><td><span class="pill ${r.src === "job" ? "job" : ""}">${esc(r.src)}</span></td><td>${ts(r.event_time) ? new Date(ts(r.event_time)).toLocaleString() : "—"}</td><td class="num"><b>${num(r.secs / 60, 1)} min</b></td><td>${esc(r.status || "")}</td></tr>`).join("")}</tbody></table></div>` : ""}`;
      },
      genie: () => {
        const g = S.genie || {};
        if (S.demo) return `<div class="empty"><h3>Genie answers from the live system tables</h3>Switch to <b>Live</b> to ask about real clusters; the synthetic demo has no tables behind it.</div>`;
        if (g.state !== "ready") return `<div class="locked-panel"><h4>🔒 Ask Genie is not available to you</h4><div class="why">${esc(g.reason || "")}</div>${g.fix ? `<p style="margin:8px 0 0">${esc(g.fix)}</p>` : ""}</div>`;
        const conv = S.genieConv[c.key] || { turns: [] };
        const qs = (D.genieQuestions || []).map((q) => `<button type="button" data-q="${esc(q)}">${esc(q)}</button>`).join("");
        return `<div class="note" style="margin-top:0">Ask in plain English. Genie writes the SQL over the system tables and runs it <b>as you</b>, so answers respect your own grants. No model serving endpoint is used.</div>
          <div class="genie-q">${qs}</div>
          <form class="ask" id="askForm"><input id="askInput" placeholder="Ask about ${esc(c.name)}…" autocomplete="off"><button class="primary" id="askBtn">Ask</button></form>
          <div id="turns">${conv.turns.map(turnHtml).join("")}</div>
          <div class="gnote">Space: ${esc(g.title || g.space_id)} · answers typically take 10–40 s.</div>`;
      },
      events: () => {
        const ev = D.events || [];
        if (!ev.length) return '<div class="empty">No cluster events in the window (or the cluster is known only from system tables).</div>';
        return `<div class="note">Most recent ${ev.length} events from the cluster event log (kept 60 days).</div><table class="small-table"><thead><tr><th>When</th><th>Event</th><th class="num">Workers</th><th class="num">Target</th><th>Reason</th></tr></thead><tbody>
          ${ev.map((e) => `<tr><td>${new Date(e.t).toLocaleString()}</td><td><span class="pill">${esc(e.type)}</span></td><td class="num">${num(e.current_workers)}</td><td class="num">${num(e.target_workers)}</td><td>${esc((e.reason || "").toLowerCase())}${e.user ? " · " + esc(e.user) : ""}</td></tr>`).join("")}</tbody></table>`;
      },
    }[S.tab]();
    body.innerHTML = `<div class="pane">${html}</div>`;
    afterTab();
  }

  function afterTab() {
    const D = S.detail, c = D.cluster;
    if (S.tab === "workers" && $("#cWorkers")) {
      const s = D.worker_series;
      const w = D.window || {};
      Charts.line($("#cWorkers"), {
        height: 240, step: true, xMin: w.start_ms, xMax: w.end_ms, yMax: Math.max(2, (c.max_workers || 0) + 1, ...s.map((p) => p[1] || 0)) ,
        bands: c.max_workers != null ? [{ y0: c.min_workers ?? c.max_workers, y1: c.max_workers, color: "#dbe6ff", opacity: 0.7, label: `configured ${c.min_workers ?? c.max_workers}–${c.max_workers}` }] : [],
        series: [{ name: "workers", color: "#1d4ed8", points: s, area: true, step: true, width: 2 }],
        yFmt: (v) => (Number.isInteger(v) ? v : v.toFixed(1)),
      });
    }
    if (S.tab === "util" && $("#utilCharts")) drawUtil("together");
    if (S.tab === "genie" && $("#askForm")) {
      document.querySelectorAll(".genie-q button").forEach((b) => (b.onclick = () => askGenie(b.dataset.q)));
      $("#askForm").onsubmit = (e) => { e.preventDefault(); const v = $("#askInput").value.trim(); if (v) { $("#askInput").value = ""; askGenie(v); } };
    }
    if (S.tab === "heat" && $("#heat")) drawHeat("workers", "cpu");
    if (S.tab === "users" && $("#daily")) {
      const rows = ((D.detail.daily_commands || {}).rows || []).map((r) => ({ label: r.d, short: new Date(r.d + "T00:00:00").toLocaleDateString([], { day: "numeric", month: "short" }), value: r.commands, users: r.users, secs: r.secs }));
      Charts.bars($("#daily"), rows, { color: "#0ea5e9", fmt: (r) => `${r.value} commands · ${r.users} users${r.secs ? " · " + num(r.secs / 3600, 1) + " h run time" : ""}` });
    }
  }
  const fmtCell = (v) => { if (v == null) return ""; const s = String(v); if (/^-?\d+\.\d{3,}$/.test(s)) return Number(s).toLocaleString(undefined, { maximumFractionDigits: 2 }); return s.length > 40 ? s.slice(0, 40) + "…" : s; };
  function turnHtml(t) {
    const md = (x) => esc(x).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>");
    const body = (txt) => { const lines = (txt || "").split("\n"); let h = "", inList = false;
      for (const l of lines) { if (/^\s*[-*] /.test(l)) { if (!inList) { h += "<ul>"; inList = true; } h += `<li>${md(l.replace(/^\s*[-*] /, ""))}</li>`; }
        else { if (inList) { h += "</ul>"; inList = false; } if (l.trim()) h += `<p>${md(l)}</p>`; } }
      return h + (inList ? "</ul>" : ""); };
    let a;
    if (!t.done) a = `<div class="thinking"><span class="spinner sm"></span> Genie is working… ${t.status ? "(" + esc(t.status.toLowerCase().replace(/_/g, " ")) + ")" : ""}</div>`;
    else if (t.error && !t.text) a = `<div class="banner err" style="margin:0">${esc(t.error)}</div>`;
    else a = body(t.text || t.description || "Done.") +
      (t.rows && t.rows.length ? `<div class="gtable"><table class="small-table" style="margin-top:6px"><thead><tr>${t.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>${t.rows.slice(0, 20).map((r) => `<tr>${r.map((v) => `<td>${esc(fmtCell(v))}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : "") +
      (t.sql ? `<details><summary>Show the SQL Genie ran</summary><pre>${esc(t.sql)}</pre></details>` : "");
    return `<div class="turn"><div class="you">${esc(t.q)}</div><div class="ans">${a}</div></div>`;
  }
  async function askGenie(question) {
    const key = S.sel, conv = (S.genieConv[key] = S.genieConv[key] || { turns: [] });
    const t = { q: question, done: false }; conv.turns.push(t);
    if (S.tab === "genie") renderTab();
    try {
      const r = await fetch("api/genie", { method: "POST", credentials: "same-origin", headers: { "content-type": "application/json", "x-crs": "1" }, body: JSON.stringify({ question, conversation_id: conv.id }) });
      const j = await r.json(); if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
      conv.id = j.conversation_id;
      const t0 = Date.now();
      while (Date.now() - t0 < 180000) {
        await new Promise((res) => setTimeout(res, 2000));
        const p = await api(`genie?cid=${encodeURIComponent(j.conversation_id)}&mid=${encodeURIComponent(j.message_id)}`);
        Object.assign(t, p);
        if (S.tab === "genie" && S.sel === key) { const box = $("#turns"); if (box) box.innerHTML = conv.turns.map(turnHtml).join(""); }
        if (p.done) break;
      }
      if (!t.done) { t.done = true; t.error = "Genie did not answer within 3 minutes."; }
    } catch (e) { t.done = true; t.error = e.message; }
    if (S.tab === "genie" && S.sel === key) { const box = $("#turns"); if (box) box.innerHTML = conv.turns.map(turnHtml).join(""); }
  }
  function drawUtil(mode) {
    const rows = (S.detail.detail.timeseries || {}).rows || [];
    const pick = (drv, k) => rows.filter((r) => r.driver === drv).map((r) => [ts(r.t), r[k]]);
    const box = $("#utilCharts"); box.innerHTML = "";
    const mk = (title) => { const d = document.createElement("div"); d.className = "box"; d.innerHTML = `<h3>${title}</h3><div></div>`; box.appendChild(d); return d.lastChild; };
    const common = { height: 200, yMax: 100, yFmt: (v) => Math.round(v) + "%", hlines: [] };
    if (mode === "together") {
      Charts.line(mk("CPU (user + system), average per bucket"), { ...common, series: [{ name: "workers CPU", color: "#0ea5e9", points: pick(false, "cpu"), area: true }, { name: "driver CPU", color: "#7c3aed", points: pick(true, "cpu") }], hlines: [{ y: 40, label: "worker downsize line 40%", color: "#94a3b8" }] });
      Charts.line(mk("Memory used"), { ...common, series: [{ name: "workers memory", color: "#f59e0b", points: pick(false, "mem"), area: true }, { name: "driver memory", color: "#7c3aed", points: pick(true, "mem"), dash: "5 3" }], hlines: [{ y: 60, label: "60%", color: "#94a3b8" }] });
    } else {
      Charts.line(mk("Workers: CPU and memory"), { ...common, series: [{ name: "CPU", color: "#0ea5e9", points: pick(false, "cpu"), area: true }, { name: "CPU max", color: "#0ea5e9", points: pick(false, "cpu_max"), dash: "2 3", width: 1 }, { name: "memory", color: "#f59e0b", points: pick(false, "mem") }] });
      Charts.line(mk("Driver: CPU and memory"), { ...common, series: [{ name: "CPU", color: "#7c3aed", points: pick(true, "cpu"), area: true }, { name: "CPU max", color: "#7c3aed", points: pick(true, "cpu_max"), dash: "2 3", width: 1 }, { name: "memory", color: "#f59e0b", points: pick(true, "mem") }] });
      Charts.line(mk("Worker nodes running"), { height: 140, step: true, series: [{ name: "worker nodes", color: "#1d4ed8", points: pick(false, "nodes"), area: true, step: true }] });
    }
    document.querySelectorAll("#utilMode button").forEach((b) => { b.classList.toggle("on", b.dataset.m === mode); b.onclick = () => drawUtil(b.dataset.m); });
  }
  function drawHeat(role, metric) {
    const rows = ((S.detail.detail.heatmap || {}).rows || []).filter((r) => r.driver === (role === "driver"));
    Charts.heatmap($("#heat"), rows, metric);
    document.querySelectorAll("#heatRole button").forEach((b) => { b.classList.toggle("on", b.dataset.r === role); b.onclick = () => drawHeat(b.dataset.r, metric); });
    document.querySelectorAll("#heatMetric button").forEach((b) => { b.classList.toggle("on", b.dataset.m === metric); b.onclick = () => drawHeat(role, b.dataset.m); });
  }

  // ── wiring ─────────────────────────────────────────────────────────────────────────────────────────
  if (DEMO_SETTING === "off") $("#sourceToggle").hidden = true;
  document.querySelectorAll("#sourceToggle button").forEach((b) => (b.onclick = () => { S.demo = b.dataset.src === "demo"; S.sel = null; closeDrawer(); save(); load("switch"); }));
  $("#days").onchange = (e) => { S.days = +e.target.value; save(); load("switch"); };
  $("#refresh").onclick = () => load(true);
  $("#q").oninput = (e) => { S.q = e.target.value; renderTable(); };
  $("#onlyRecs").onchange = (e) => { S.onlyRecs = e.target.checked; renderTable(); };
  document.querySelectorAll("#fleet th").forEach((th) => (th.onclick = () => { const k = th.dataset.k; S.sort = { k, asc: S.sort.k === k ? !S.sort.asc : k === "name" }; renderTable(); }));
  document.querySelectorAll("#dTabs button").forEach((b) => (b.onclick = () => { S.tab = b.dataset.t; if (S.detail) renderTab(); }));
  $("#dClose").onclick = closeDrawer; $("#scrim").onclick = closeDrawer;
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
  let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => S.detail && $("#drawer").classList.contains("on") && afterTab(), 200); });
  load();
})();
