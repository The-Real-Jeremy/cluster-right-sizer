"""Phase A: what the Clusters and Events APIs alone can say about an all-purpose cluster.

No system tables. From the configuration and up to 60 days of cluster events we rebuild a step function of
"how many workers were actually running, when", and derive peak / p95 workers, running hours, restarts,
resizes and idle terminations. Utilisation (CPU, memory) is NOT knowable here; recommendations that need it
say so.
"""
import math
import time
from concurrent.futures import ThreadPoolExecutor

from . import config, dbapi

ALL_PURPOSE_SOURCES = {"UI", "API", None, ""}
# Events that can only happen while the cluster is up. (PINNED, EDITED etc. can happen while it is down.)
UP_EVENTS = {"RUNNING", "UPSIZE_COMPLETED", "RESIZING", "AUTOSCALING_STATS_REPORT", "NODES_LOST", "DRIVER_HEALTHY",
             "DRIVER_NOT_RESPONDING", "DRIVER_UNAVAILABLE", "SPARK_EXCEPTION", "METASTORE_DOWN"}
START_EVENTS = {"CREATING", "STARTING", "RESTARTING"}
DOWN_EVENTS = {"TERMINATING", "TERMINATED"}
SAFE_FIELDS = ("cluster_id", "cluster_name", "state", "cluster_source", "node_type_id", "driver_node_type_id",
               "num_workers", "autoscale", "autotermination_minutes", "spark_version", "data_security_mode",
               "creator_user_name", "single_user_name", "policy_id", "runtime_engine", "start_time",
               "terminated_time", "last_state_loss_time", "state_message", "default_tags", "custom_tags",
               "instance_pool_id", "driver_instance_pool_id", "is_single_node", "kind")


def safe_config(c):
    """Only fields that cannot carry a secret (no spark_conf, no env vars, no init-script paths)."""
    out = {k: c.get(k) for k in SAFE_FIELDS if k in c}
    tags = out.pop("default_tags", None) or {}
    out["owner"] = c.get("single_user_name") or tags.get("Creator") or c.get("creator_user_name")
    return out


def is_all_purpose(c):
    return c.get("cluster_source") in ALL_PURPOSE_SOURCES


def list_clusters(app):
    out, tok = [], None
    for _ in range(50):
        q = {"page_size": 100}
        if tok:
            q["page_token"] = tok
        r = app.get("/api/2.1/clusters/list", query=q)
        out += r.get("clusters") or []
        tok = r.get("next_page_token")
        if not tok:
            break
    return out


def node_types(app):
    try:
        r = app.get("/api/2.1/clusters/list-node-types")
    except dbapi.ApiError:
        return {}
    return {n["node_type_id"]: {"cores": n.get("num_cores"), "memory_mb": n.get("memory_mb"),
                                "category": n.get("category"), "deprecated": n.get("is_deprecated", False)}
            for n in r.get("node_types") or []}


def events(app, cluster_id, start_ms, end_ms, cap=4000):
    out, tok = [], None
    while len(out) < cap:
        body = {"cluster_id": cluster_id, "start_time": start_ms, "end_time": end_ms, "order": "ASC", "page_size": 500}
        if tok:
            body["page_token"] = tok
        r = app.post("/api/2.1/clusters/events", body)
        out += r.get("events") or []
        tok = r.get("next_page_token")
        if not tok:
            break
    return out[:cap]


# ── the step function ────────────────────────────────────────────────────────────────────────────────
def worker_timeline(cfg, evs, start_ms, end_ms, now_state=None):
    """[(t_ms, workers or None if down)] — workers counted from each event's details.current_num_workers."""
    default_workers = (cfg.get("autoscale") or {}).get("min_workers", cfg.get("num_workers") or 0)
    first = next((e for e in evs if e.get("type") in (UP_EVENTS | START_EVENTS | DOWN_EVENTS)), None)
    running = bool(first and first["type"] in (UP_EVENTS | {"TERMINATING"}))
    if not evs:
        running = now_state in ("RUNNING", "RESIZING")
    workers = default_workers
    pts = [(start_ms, workers if running else None)]
    for e in evs:
        t, typ, d = e.get("timestamp", start_ms), e.get("type"), e.get("details") or {}
        cur = d.get("current_num_workers")
        if typ in START_EVENTS:
            running = False
        elif typ in DOWN_EVENTS:
            running = False
        elif typ == "RUNNING" or typ in UP_EVENTS:
            running = True
        if cur is not None and typ not in DOWN_EVENTS:
            workers = cur
        val = workers if running else None
        if pts and pts[-1][0] == t:
            pts[-1] = (t, val)
        elif not pts or pts[-1][1] != val:
            pts.append((t, val))
    pts.append((end_ms, pts[-1][1]))
    return pts


def _weighted(pts):
    """Time-weighted distribution of worker counts while running: list of (workers, ms)."""
    dist = {}
    for (t0, w), (t1, _) in zip(pts, pts[1:]):
        if w is not None and t1 > t0:
            dist[w] = dist.get(w, 0) + (t1 - t0)
    return dist


def _pct(dist, q):
    total = sum(dist.values())
    if not total:
        return None
    acc = 0
    for w in sorted(dist):
        acc += dist[w]
        if acc >= q * total:
            return w
    return max(dist)


def summarise(cfg, evs, pts, ntypes, start_ms, end_ms):
    dist = _weighted(pts)
    run_ms = sum(dist.values())
    days = max(1e-9, (end_ms - start_ms) / 86400000)
    node_ms = sum((w + 1) * ms for w, ms in dist.items())
    types = [e.get("type") for e in evs]
    reasons = {}
    for e in evs:
        if e.get("type") == "TERMINATING":
            code = ((e.get("details") or {}).get("reason") or {}).get("code") or "UNKNOWN"
            reasons[code] = reasons.get(code, 0) + 1
    active_days = set()
    for (t0, w), (t1, _) in zip(pts, pts[1:]):
        if w is not None:
            d = t0 // 86400000
            while d * 86400000 < t1:
                active_days.add(d)
                d += 1
    auto = cfg.get("autoscale") or {}
    wt = ntypes.get(cfg.get("node_type_id")) or {}
    dt = ntypes.get(cfg.get("driver_node_type_id") or cfg.get("node_type_id")) or {}
    return {
        "window_days": round(days, 1),
        "events": len(evs),
        "events_truncated": len(evs) >= 4000,
        "running_hours": round(run_ms / 3.6e6, 2),
        "node_hours": round(node_ms / 3.6e6, 2),
        "running_share": round(run_ms / max(1, end_ms - start_ms), 4),
        "avg_running_hours_per_active_day": round(run_ms / 3.6e6 / max(1, len(active_days)), 2) if active_days else 0,
        "active_days": len(active_days),
        "workers_peak": max(dist) if dist else None,
        "workers_p95": _pct(dist, 0.95),
        "workers_p50": _pct(dist, 0.50),
        "workers_avg": round(sum(w * ms for w, ms in dist.items()) / run_ms, 2) if run_ms else None,
        "workers_min_cfg": auto.get("min_workers") if auto else cfg.get("num_workers"),
        "workers_max_cfg": auto.get("max_workers") if auto else cfg.get("num_workers"),
        "autoscale": bool(auto),
        "starts": sum(1 for t in types if t in ("STARTING",)),
        "restarts": sum(1 for t in types if t == "RESTARTING"),
        "resizes": sum(1 for t in types if t in ("RESIZING",)),
        "upsizes": sum(1 for t in types if t == "UPSIZE_COMPLETED"),
        "nodes_lost": sum(1 for t in types if t == "NODES_LOST"),
        "terminations": reasons,
        "inactivity_terminations": reasons.get("INACTIVITY", 0),
        "worker_cores": wt.get("cores"), "worker_memory_gb": round(wt["memory_mb"] / 1024, 1) if wt.get("memory_mb") else None,
        "driver_cores": dt.get("cores"), "driver_memory_gb": round(dt["memory_mb"] / 1024, 1) if dt.get("memory_mb") else None,
    }


def series(pts, limit=600):
    """Compact series for the chart: [[t_ms, workers|null], ...], down-sampled by keeping change points."""
    if len(pts) <= limit:
        return [[t, w] for t, w in pts]
    step = math.ceil(len(pts) / limit)
    keep = pts[::step]
    if keep[-1] != pts[-1]:
        keep.append(pts[-1])
    return [[t, w] for t, w in keep]


def recent_events(evs, n=40):
    out = []
    for e in evs[-n:][::-1]:
        d = e.get("details") or {}
        out.append({"t": e.get("timestamp"), "type": e.get("type"),
                    "current_workers": d.get("current_num_workers"), "target_workers": d.get("target_num_workers"),
                    "reason": (d.get("reason") or {}).get("code"), "user": d.get("user")})
    return out


def analyse_cluster(app, c, ntypes, days):
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(min(days, config.EVENT_DAYS_MAX) * 86400000)
    try:
        evs = events(app, c["cluster_id"], start_ms, end_ms)
        err = None
    except dbapi.ApiError as e:
        evs, err = [], e.public()
    pts = worker_timeline(c, evs, start_ms, end_ms, c.get("state"))
    return {"config": safe_config(c), "summary": summarise(c, evs, pts, ntypes, start_ms, end_ms),
            "series": series(pts), "recent_events": recent_events(evs), "events_error": err,
            "window": {"start_ms": start_ms, "end_ms": end_ms}}


def fleet(app, days, detail_ids=None):
    clusters = [c for c in list_clusters(app) if is_all_purpose(c)]
    ntypes = node_types(app)
    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(lambda c: analyse_cluster(app, c, ntypes, days), clusters))
    return {"clusters": res, "node_types": ntypes, "listed": len(clusters)}
