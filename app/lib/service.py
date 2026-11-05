"""Orchestration: decide what the viewer can see, fetch it, merge it, and never fail as a whole.

Every panel ends up in one of: data | empty (we looked, nothing there) | unavailable (with the reason and
the grant that would unlock it). A failure in one source becomes an "unavailable" panel, not a 500.
"""
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import access, config, dbapi, demo, pat, phase_a, phase_b, recommend

_cache = {}
_lock = threading.Lock()


def _cached(key, ttl, fn):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (time.time(), val)
    return val


def forget_cache():
    with _lock:
        _cache.clear()
        _audit_jobs.clear()


def simulate_no_system_tables(acc):
    """Diagnostic (CRS_SP_DIAGNOSTICS=1 only): every system table reads as denied, labelled as simulated."""
    acc = dict(acc, tables={k: dict(v, state="denied", reason="SIMULATED for this diagnostic view: no SELECT on " + v["table"])
                            for k, v in acc["tables"].items()}, phase_b=False, simulated=True)
    return acc


def as_app(headers):
    """Diagnostic view as the app's service principal (CRS_SP_DIAGNOSTICS=1 only)."""
    tok = dbapi.sp_token()
    h = {k: v for k, v in headers.items() if k not in ("cookie",)}
    h.update({"x-forwarded-access-token": tok, "x-forwarded-email": f"{config.CLIENT_ID}", "x-crs-as-app": "1",
              "x-forwarded-preferred-username": "app service principal (diagnostic view)"})
    return h


def viewer_identity(headers):
    return {"as_app": headers.get("x-crs-as-app") == "1","email": headers.get("x-forwarded-email"), "name": headers.get("x-forwarded-preferred-username"),
            "user": headers.get("x-forwarded-user"), "obo": bool(headers.get("x-forwarded-access-token"))}


def _phase_a(days, ps):
    """Cluster list + events with the VIEWER's own PAT (Apps has no user scope for the Clusters API).
    Cached per token hash for 3 minutes; the cache key never contains the token."""
    if ps.state != "ok":
        return {"state": "needs_pat", "pat": ps.public(), "clusters": [], "node_types": {}, "listed": 0}
    def load():
        c = dbapi.Client(lambda: ps.pat, label="your personal access token")
        try:
            res = phase_a.fleet(c, days)
            return {"state": "ok", **res}
        except dbapi.ApiError as e:
            return {"state": "unavailable", "error": e.public(), "clusters": [], "node_types": {}, "listed": 0}
    key = ("phase_a", hashlib.sha256(ps.pat.encode()).hexdigest(), days)
    return _cached(key, 180, load)


_BG = ThreadPoolExecutor(max_workers=4)
_audit_jobs = {}


def _audit_background(viewer, acc, email, days):
    """The audit scan is the slow one (the table is region-wide). Run it in the background and let the page
    poll: {"loading": True} until it lands, then the rows, cached for 10 minutes."""
    key = (email, days)
    with _lock:
        job = _audit_jobs.get(key)
        if job and job["future"].done() and time.time() - job["at"] > 600:
            job = None
        if not job:
            job = {"at": time.time(), "future": _BG.submit(phase_b.audit_fleet, viewer, acc["warehouse"]["id"], acc, days)}
            _audit_jobs[key] = job
    f = job["future"]
    if not f.done():
        return {"loading": True, "since": job["at"]}
    try:
        return f.result()
    except Exception as e:
        return {"error": {"kind": "error", "message": str(e)[:300]}}


def overview(headers, days, use_demo=False, refresh=False):
    return _compute(headers, days, use_demo, refresh)[0]


def _compute(headers, days, use_demo=False, refresh=False):
    days = max(1, min(90, int(days)))
    ident = viewer_identity(headers)
    if refresh:
        forget_cache()
    if use_demo:
        pa_list, b, ntypes = demo.build(days)
        acc = demo.access()
        recs = recommend.merge(demo.WS, pa_list, b, ntypes)
        pa_state = {"state": "ok", "listed": len(pa_list), "source": "synthetic", "pat": {"state": "demo"}}
    else:
        viewer = dbapi.viewer_client(headers)
        ps = pat.from_request(headers)
        with ThreadPoolExecutor(max_workers=2) as ex:   # the probe (SQL) and Phase A (REST) are independent
            f_acc = ex.submit(access.probe, viewer, ident["email"] or ident["user"], refresh)
            f_pa = ex.submit(_phase_a, days, ps)
            acc, pa = f_acc.result(), f_pa.result()
        if headers.get("x-crs-simulate") == "no_system_tables":
            acc = simulate_no_system_tables(acc)
        pa_state = {"state": pa["state"], "listed": pa.get("listed", 0), "error": pa.get("error"), "source": "clusters_api",
                    "pat": ps.public(), "clear_cookie": ps.clear}
        b = None
        if acc["warehouse"]["id"] and any(t["state"] == "granted" for t in acc["tables"].values()):
            key = ("phase_b", ident["email"], days)
            b = dict(_cached(key, 600, lambda: phase_b.fleet(viewer, acc["warehouse"]["id"], acc, days)))
            b["audit_users"] = _audit_background(viewer, acc, ident["email"], days)
        ntypes = dict(pa.get("node_types") or {})
        for nt in ((b or {}).get("node_types", {}).get("rows") or []):
            ntypes.setdefault(nt["node_type"], {"cores": nt.get("core_count"), "memory_mb": nt.get("memory_mb"), "deprecated": False})
        recs = recommend.merge(config.WORKSPACE_ID or "local", pa["clusters"] if pa["state"] == "ok" else None, b, ntypes)
    clusters = sorted(recs.values(), key=lambda r: (r["score"] is None, -(r["score"] or 0)))
    panels = _panel_states(acc, pa_state, b if not use_demo else {"util": {"rows": [1]}, "usage": {"rows": [1]}, "audit_users": {"rows": [1]}})
    payload = {
        "missing": demo_missing() if use_demo else missing(ident, acc, pa_state, recs, b),
        "demo": use_demo, "days": days, "viewer": ident, "access": acc, "phase_a": pa_state, "panels": panels,
        "kpis": _kpis(clusters), "clusters": [_row(r) for r in clusters],
        "settings": config.describe(), "generated_at": time.time(),
    }
    return payload, recs, (pa_list if use_demo else (pa.get("clusters") or [])), b


def _panel_states(acc, pa_state, b):
    t = acc["tables"]
    def from_b(name, table):
        st = t.get(table, {}).get("state")
        if st != "granted":
            return {"state": "unavailable", "why": t.get(table, {}).get("reason") or "no SELECT on " + access.TABLES[table],
                    "table": access.TABLES[table], "access": st}
        res = (b or {}).get(name) or {}
        if res.get("loading"):
            return {"state": "loading", "table": access.TABLES[table]}
        if res.get("error"):
            return {"state": "unavailable", "why": res["error"]["message"], "table": access.TABLES[table], "access": "error"}
        return {"state": "ok" if res.get("rows") else "empty", "table": access.TABLES[table]}
    return {
        "clusters_api": {"state": "ok" if pa_state["state"] == "ok" else "unavailable",
                         "why": (pa_state.get("error") or {}).get("message") or (pa_state.get("pat") or {}).get("message"),
                         "access": "needs_pat" if pa_state["state"] == "needs_pat" else pa_state["state"],
                         "identity": "your personal access token"},
        "utilisation": from_b("util", "node_timeline"),
        "cost": from_b("usage", "usage"),
        "users": from_b("audit_users", "audit"),
        "prices": {"state": t.get("list_prices", {}).get("state")},
    }


def _row(r):
    cfg, pa, w, d = r["config"], r["phase_a"] or {}, r["util"].get("workers") or {}, r["util"].get("driver") or {}
    auto = cfg.get("autoscale") or {}
    return {
        "key": r["key"], "workspace_id": r["workspace_id"], "cluster_id": r["cluster_id"], "name": cfg.get("cluster_name") or r["cluster_id"],
        "state": cfg.get("state"), "owner": cfg.get("owner"), "node_type": cfg.get("node_type_id"),
        "driver_node_type": cfg.get("driver_node_type_id"), "autoscale": bool(auto),
        "min_workers": auto.get("min_workers") if auto else cfg.get("num_workers"),
        "max_workers": auto.get("max_workers") if auto else cfg.get("num_workers"),
        "single_node": bool(cfg.get("is_single_node")) or (not auto and cfg.get("num_workers") == 0),
        "autoterm": cfg.get("autotermination_minutes"), "access_mode": cfg.get("data_security_mode"),
        "runtime": cfg.get("spark_version"), "deleted": cfg.get("deleted", False),
        "workers_peak": pa.get("workers_peak"), "workers_p95": pa.get("workers_p95"), "running_hours": pa.get("running_hours"),
        "node_hours": pa.get("node_hours"), "restarts": (pa.get("starts") or 0) + (pa.get("restarts") or 0), "resizes": pa.get("resizes"),
        "inactivity_terminations": pa.get("inactivity_terminations"),
        "w_cpu_p95": w.get("cpu_p95"), "w_mem_p95": w.get("mem_p95"), "d_cpu_p95": d.get("cpu_p95"), "d_mem_p95": d.get("mem_p95"),
        "dbus": (r.get("usage") or {}).get("dbus"), "list_cost": (r.get("usage") or {}).get("list_cost"),
        "currency": (r.get("usage") or {}).get("currency"),
        "users": r.get("user_count"), "commands": r.get("commands"), "idle_share": r.get("idle_share"),
        "score": r["score"], "score_basis": r["score_basis"], "saving": r["saving"],
        "recommendations": r["recommendations"], "notes": r["notes"], "sources": r["sources"],
    }


def _kpis(clusters):
    s = lambda k: sum((c.get(k) or 0) for c in clusters)
    cost = sum((c["saving"] or {}).get("list_cost") or 0 for c in clusters)
    dbu = sum((c["saving"] or {}).get("dbus") or 0 for c in clusters)
    return {
        "clusters": len(clusters),
        "with_recommendations": sum(1 for c in clusters if c["recommendations"]),
        "running_hours": round(sum((c.get("phase_a") or {}).get("running_hours") or 0 for c in clusters), 1),
        "node_hours": round(sum((c.get("phase_a") or {}).get("node_hours") or 0 for c in clusters), 1),
        "dbus": round(sum(((c.get("usage") or {}).get("dbus") or 0) for c in clusters), 1),
        "list_cost": round(sum(((c.get("usage") or {}).get("list_cost") or 0) for c in clusters), 2),
        "saving_cost": round(cost, 2), "saving_dbus": round(dbu, 1),
        "users": len({u.get("email") for c in clusters for u in c.get("users") or [] if u.get("email")}),
    }


def cluster_detail(headers, key, days, use_demo=False, part="core"):
    days = max(1, min(90, int(days)))
    payload, recs, pa_list, b = _compute(headers, days, use_demo)
    r = recs.get(key)
    if not r:
        return None
    ws, _, cid = key.partition(":")
    item = next((p for p in pa_list if p["config"]["cluster_id"] == cid), None) if r["phase_a"] else None
    acc = payload["access"]
    if use_demo:
        det = demo.detail(item, [u for u in b["util"]["rows"] if u["cluster_id"] == cid],
                          [u for u in b["audit_users"]["rows"] if u["cluster_id"] == cid], days)
    elif acc["warehouse"]["id"]:
        ck = ("detail", payload["viewer"]["email"], key, days, part)
        det = _cached(ck, 600, lambda: phase_b.detail(dbapi.viewer_client(headers), acc["warehouse"]["id"], acc, cid, days, part))
    else:
        det = {"bucket_seconds": phase_b.bucket_seconds(days)}
    row = _row(r)
    cu = (det.get("cluster_users") or {}).get("rows")
    row["users_detail"] = sorted(cu if cu is not None else r["users"], key=lambda u: -(u.get("commands") or 0))
    return {"demo": use_demo, "days": days, "part": part, "cluster": row, "panels": payload["panels"],
            "worker_series": item["series"] if item else [], "events": item["recent_events"] if item else [],
            "window": item["window"] if item else None, "phase_a": r["phase_a"], "util": r["util"],
            "detail": det, "access": acc, "timezone": "UTC" if use_demo else config.TIMEZONE}


# ── what this viewer is missing, with the fix for each ───────────────────────────────────────────────
def _q(email):
    return "`" + (email or "<you>").replace("`", "") + "`"


def _principal(who):
    """Permissions-API principal field: users have an e-mail; a service principal is its application id."""
    return {"user_name": who} if "@" in who else {"service_principal_name": who}


def missing(ident, acc, pa_state, recs, b):
    who = ident.get("email") or ident.get("user") or "<you>"
    items = []
    ps = pa_state.get("pat") or {}
    if pa_state["state"] == "ok":
        items.append({"area": "Clusters and events", "state": "ok",
                      "detail": f"Your token lists {pa_state['listed']} all-purpose clusters (the ones you have a permission on)."})
    elif pa_state["state"] == "needs_pat":
        items.append({"area": "Clusters and events", "state": "missing", "detail": ps.get("message"),
                      "fix": "Create a personal access token (Settings → Developer → Access tokens) and enter it here. "
                             "If token creation is disabled for you, a workspace admin grants 'Can Use' on tokens (Settings → Advanced → Personal Access Tokens → Permission Settings)."})
    else:
        items.append({"area": "Clusters and events", "state": "unknown", "detail": (pa_state.get("error") or {}).get("message")})
    wh = acc["warehouse"]
    t = acc["tables"]
    wh_unknown = [v for v in t.values() if v["state"] == "unknown" and "CAN USE" in (v.get("reason") or "")]
    if not wh["id"]:
        items.append({"area": "SQL warehouse", "state": "missing", "detail": wh["how"],
                      "fix": "Ask an admin for CAN USE on a SQL warehouse, or set CRS_WAREHOUSE_ID on the app."})
    elif wh_unknown:
        items.append({"area": f"SQL warehouse {wh.get('name') or wh['id']}", "state": "missing", "detail": "You do not have CAN USE on it.",
                      "fix": f"Warehouse → Permissions → add {who} with Can use.",
                      "api": json.dumps({"access_control_list": [dict(_principal(who), permission_level="CAN_USE")]}),
                      "api_path": f"PATCH /api/2.0/permissions/warehouses/{wh['id']}"})
    else:
        items.append({"area": f"SQL warehouse {wh.get('name') or wh['id']}", "state": "ok", "detail": wh["how"]})
    schema_of = {"node_timeline": "system.compute", "clusters": "system.compute", "node_types": "system.compute",
                 "usage": "system.billing", "list_prices": "system.billing", "audit": "system.access"}
    for k, v in t.items():
        if v["state"] == "granted":
            items.append({"area": v["table"], "state": "ok", "detail": v["unlocks"]})
            continue
        sch = schema_of[k]
        items.append({"area": v["table"], "state": "missing" if v["state"] == "denied" else "unknown",
                      "detail": (v.get("reason") or "")[:200], "unlocks": v["unlocks"],
                      "grant": [f"GRANT USE CATALOG ON CATALOG system TO {_q(who)};",
                                f"GRANT USE SCHEMA ON SCHEMA {sch} TO {_q(who)};",
                                f"GRANT SELECT ON TABLE {v['table']} TO {_q(who)};"]})
    # clusters that exist (system table) but that this viewer's token cannot see
    unseen, how = [], None
    if pa_state["state"] != "ok":
        how = "Enter your token first: without it the app cannot tell which clusters you can see."
    elif t["clusters"]["state"] != "granted":
        how = "Could not determine: listing clusters you cannot see needs SELECT on system.compute.clusters."
    else:
        seen = {r["cluster_id"] for r in recs.values() if "clusters_api" in r["sources"]}
        used = {u["cluster_id"] for u in ((b or {}).get("util", {}).get("rows") or [])}
        cutoff = time.time() - 30 * 86400
        for row in ((b or {}).get("clusters", {}).get("rows") or []):
            if row["workspace_id"] != config.WORKSPACE_ID or row["cluster_id"] in seen or row.get("delete_time"):
                continue
            changed = row.get("change_time")
            recent = row["cluster_id"] in used
            try:
                recent = recent or (changed and time.mktime(time.strptime(str(changed)[:19], "%Y-%m-%dT%H:%M:%S")) >= cutoff)
            except ValueError:
                pass
            if recent:
                unseen.append({"cluster_id": row["cluster_id"], "cluster_name": row.get("cluster_name"), "owner": row.get("owned_by"),
                               "api_path": f"PATCH /api/2.0/permissions/clusters/{row['cluster_id']}",
                               "api": json.dumps({"access_control_list": [dict(_principal(who), permission_level="CAN_ATTACH_TO")]})})
        how = ("ok" if not unseen else
               f"{len(unseen)} all-purpose clusters in this workspace (used or changed in the last 30 days) are not visible to your token. "
               "CAN ATTACH TO on a cluster is enough to read its configuration, events and metrics.")
    errs = [{"cluster_id": r["cluster_id"], "name": r["config"].get("cluster_name"), "error": r["events_error"]["message"]}
            for r in recs.values() if r.get("events_error")]
    return {"who": who, "items": items, "unseen_clusters": unseen, "unseen_check": how, "event_errors": errs,
            "verbose_audit": "Per-cell run times need Verbose Audit Logs (workspace admin → Settings → Advanced)."}


def demo_missing():
    return {"who": "demo", "items": [{"area": "Synthetic demo data", "state": "ok", "detail": "Nothing is read from the workspace."}],
            "unseen_clusters": [], "unseen_check": "ok", "event_errors": [], "verbose_audit": ""}
