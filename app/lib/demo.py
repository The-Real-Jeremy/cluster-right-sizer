"""Synthetic demo data, clearly labelled in the UI. Deterministic (seeded), so screenshots are repeatable.

It produces the SAME raw shapes the live code reads (cluster configs + cluster events, node_timeline
aggregates, billing, audit rows), and those go through the same phase_a / recommend code as live data.
So the demo exercises the real rules; it does not paint pre-baked answers.
"""
import datetime as dt
import math
import random
import time

from . import phase_a

WS = "1000000000000001"
PRICE = 0.55   # synthetic list price per DBU for the demo only

NODE_TYPES = {n: {"cores": c, "memory_mb": int(g * 1024), "category": cat, "deprecated": False} for n, c, g, cat in [
    ("Standard_D4s_v3", 4, 16, "General Purpose"), ("Standard_D8s_v3", 8, 32, "General Purpose"),
    ("Standard_D16s_v3", 16, 64, "General Purpose"), ("Standard_D32s_v3", 32, 128, "General Purpose"),
    ("Standard_E4ds_v5", 4, 32, "Memory Optimized"), ("Standard_E8ds_v5", 8, 64, "Memory Optimized"),
    ("Standard_E16ds_v5", 16, 128, "Memory Optimized"), ("Standard_E32ds_v5", 32, 256, "Memory Optimized"),
    ("Standard_DS3_v2", 4, 14, "General Purpose"), ("Standard_DS4_v2", 8, 28, "General Purpose"),
    ("Standard_DS5_v2", 16, 56, "General Purpose"), ("Standard_L8s_v3", 8, 64, "Storage Optimized"),
    ("Standard_L16s_v3", 16, 128, "Storage Optimized")]}

# name, worker type, driver type, (min,max) or fixed n, autoterm, profile, hours (start,end) or "24x7", users
CLUSTERS = [
    ("analytics-shared-xl", "Standard_D16s_v3", None, (2, 20), 120, dict(cpu=22, mem=38, wait=2), (8, 19), 14),
    ("data-science-team", "Standard_E16ds_v5", None, 8, 60, dict(cpu=31, mem=44, wait=3), "24x7", 6),
    ("etl-adhoc", "Standard_D8s_v3", None, (2, 8), 30, dict(cpu=86, mem=71, wait=4), (6, 15), 3),
    ("finance-reporting", "Standard_DS4_v2", "Standard_E32ds_v5", (1, 4), 45, dict(cpu=58, mem=66, wait=3, dcpu=6, dmem=18), (7, 18), 9),
    ("sandbox-analyst", "Standard_D8s_v3", None, (2, 8), 0, dict(cpu=12, mem=25, wait=1, dcpu=7, dmem=22), (9, 17), 1),
    ("streaming-dev", "Standard_L8s_v3", None, 4, 60, dict(cpu=34, mem=52, wait=19), (8, 20), 4),
    ("marketing-insights", "Standard_D16s_v3", None, (1, 6), 90, dict(cpu=9, mem=30, wait=1, idle=0.72), (8, 18), 5),
    ("bi-extracts", "Standard_D8s_v3", None, (2, 6), 30, dict(cpu=67, mem=72, wait=4), (5, 9), 2),
    ("risk-models", "Standard_E8ds_v5", None, (2, 6), 60, dict(cpu=48, mem=88, wait=3, swap=6), (8, 18), 4),
    ("legacy-shared", "Standard_DS5_v2", None, (4, 12), 480, dict(cpu=27, mem=41, wait=2), (8, 17), 11),
]


def _cid(i):
    return f"0901-0{i:02d}100-demo{i:02d}ab"


def _events(rng, i, cfg, hours, days, start_ms, end_ms):
    """Per working day: start, run with resizes, idle tail, inactivity stop. 24x7: one long run."""
    evs = []
    auto = cfg.get("autoscale")
    lo, hi = (auto["min_workers"], auto["max_workers"]) if auto else (cfg["num_workers"], cfg["num_workers"])
    peak_cap = min(hi, {0: 6, 3: 3, 4: 6, 6: 3, 9: 7}.get(i, hi))
    at = cfg["autotermination_minutes"] or 0
    def add(t, typ, cur=None, reason=None):
        d = {}
        if cur is not None:
            d["current_num_workers"] = cur
        if reason:
            d["reason"] = {"code": reason}
        evs.append({"timestamp": int(t), "type": typ, "details": d})
    if hours == "24x7":
        add(start_ms - 3600e3 + 1, "RUNNING", lo)
        t = start_ms
        while t < end_ms:
            t += rng.uniform(4, 9) * 3600e3
            add(min(t, end_ms - 1), "RESIZING", rng.randint(lo, hi)) if auto else None
        return evs
    day0 = dt.datetime.utcfromtimestamp(start_ms / 1000).date()
    for k in range(days + 1):
        day = day0 + dt.timedelta(days=k)
        if day.weekday() >= 5 or rng.random() < 0.08:
            continue
        s = dt.datetime(day.year, day.month, day.day, hours[0]) + dt.timedelta(minutes=rng.uniform(-30, 40))
        e = dt.datetime(day.year, day.month, day.day, hours[1]) + dt.timedelta(minutes=rng.uniform(-60, 45))
        t0, t1 = s.timestamp() * 1000, e.timestamp() * 1000
        if t0 < start_ms or t1 > end_ms:
            continue
        add(t0 - 300e3, "STARTING")
        add(t0, "RUNNING", lo)
        t, cur = t0, lo
        while auto and t < t1 - 3600e3:
            t += rng.uniform(0.6, 2.2) * 3600e3
            nxt = max(lo, min(peak_cap, cur + rng.choice([-2, -1, 1, 2, 2])))
            if nxt != cur:
                add(t, "RESIZING", cur)
                add(t + 180e3, "UPSIZE_COMPLETED" if nxt > cur else "RESIZING", nxt)
                cur = nxt
        if at:
            add(t1 + at * 60e3, "TERMINATING", reason="INACTIVITY")
            add(t1 + at * 60e3 + 120e3, "TERMINATED")
        elif k == days:
            pass
    return evs


def build(days):
    """Everything the live endpoints would have fetched, synthesised. Returns (phase_a list, b dict, ntypes)."""
    rng = random.Random(20261105)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(min(days, 60) * 86400e3)
    pa, util, usage, users, clusters_rows = [], [], [], [], []
    for i, (name, wt, dtp, size, at, prof, hours, n_users) in enumerate(CLUSTERS):
        cfg = {"cluster_id": _cid(i), "cluster_name": name, "state": "RUNNING" if hours == "24x7" else "TERMINATED",
               "cluster_source": "UI", "node_type_id": wt, "driver_node_type_id": dtp or wt,
               "autotermination_minutes": at, "spark_version": "17.3.x-scala2.13", "data_security_mode": "USER_ISOLATION",
               "creator_user_name": f"platform.admin{i % 3 + 1}@example.com"}
        if isinstance(size, tuple):
            cfg["autoscale"] = {"min_workers": size[0], "max_workers": size[1]}
        else:
            cfg["num_workers"] = size
        evs = _events(rng, i, cfg, hours, days, start_ms, end_ms)
        pts = phase_a.worker_timeline(cfg, evs, start_ms, end_ms, cfg["state"])
        summ = phase_a.summarise(cfg, evs, pts, NODE_TYPES, start_ms, end_ms)
        pa.append({"config": phase_a.safe_config(cfg), "summary": summ, "series": phase_a.series(pts),
                   "recent_events": phase_a.recent_events(evs), "events_error": None,
                   "window": {"start_ms": start_ms, "end_ms": end_ms}, "_events": evs})
        run_min = summ["running_hours"] * 60
        wmin = int(run_min * (summ["workers_avg"] or 0))
        idle = prof.get("idle", 0.18)
        for drv, nm, cpu, mem in ((False, wmin, prof["cpu"], prof["mem"]), (True, int(run_min), prof.get("dcpu", max(28, prof["cpu"] * 0.6)), prof.get("dmem", max(55, prof["mem"] * 0.9)))):
            if nm <= 0:
                continue
            util.append({"workspace_id": WS, "cluster_id": cfg["cluster_id"], "driver": drv, "node_minutes": nm,
                         "instances": max(1, (summ["workers_peak"] or 1) * 3) if not drv else max(1, summ["starts"]),
                         "node_type": (dtp or wt) if drv else wt, "cpu_avg": cpu * 0.55, "cpu_p50": cpu * 0.45, "cpu_p95": cpu,
                         "cpu_max": min(100, cpu * 1.3), "mem_avg": mem * 0.8, "mem_p95": mem, "mem_max": min(100, mem * 1.1),
                         "wait_avg": prof["wait"] if not drv else 1.0, "wait_p95": prof["wait"] * 2, "swap_max": prof.get("swap", 0) if not drv else 0,
                         "idle_minutes": int(nm * idle)})
        dbus = sum((((NODE_TYPES[t]["cores"]) * 0.1875) * m / 60) for t, m in ((wt, wmin), (dtp or wt, run_min)))
        usage.append({"workspace_id": WS, "cluster_id": cfg["cluster_id"], "dbus": round(dbus, 1),
                      "list_cost": round(dbus * PRICE, 2), "currency": "USD"})
        for u in range(n_users):
            cmds = int(rng.uniform(20, 400) * (summ["active_days"] or 1) / 20)
            users.append({"workspace_id": WS, "cluster_id": cfg["cluster_id"], "email": f"user{(i * 7 + u) % 40 + 1:02d}@example.com",
                          "src": "job" if (u == 0 and i in (2, 7)) else "notebook", "commands": cmds, "timed": cmds,
                          "secs_total": cmds * rng.uniform(4, 90), "secs_p50": rng.uniform(1, 12), "secs_p95": rng.uniform(30, 600),
                          "secs_max": rng.uniform(600, 3600), "failed": int(cmds * rng.uniform(0, 0.05)),
                          "last_seen": dt.datetime.utcfromtimestamp(end_ms / 1000 - rng.uniform(0, 5) * 86400).isoformat()})
    b = {"util": {"rows": util}, "usage": {"rows": usage}, "audit_users": {"rows": users}, "attach": {"rows": []},
         "clusters": {"skipped": True}, "node_types": {"skipped": True}}
    return pa, b, NODE_TYPES


def access():
    tables = ("node_timeline", "clusters", "node_types", "usage", "list_prices", "audit")
    from .access import TABLES, UNLOCKS, grants_sql
    return {"warehouse": {"id": "demo", "name": "Synthetic demo data", "how": "demo mode"},
            "tables": {k: {"table": TABLES[k], "state": "granted", "unlocks": UNLOCKS[k]} for k in tables},
            "phase_b": True, "grants": grants_sql(), "any_unknown": False, "demo": True}


def detail(pa_item, util_rows, users_rows, days):
    """Synthetic per-cluster detail consistent with the cluster's own synthetic events and profile."""
    rng = random.Random(hash(pa_item["config"]["cluster_id"]) & 0xffff)
    w = next((u for u in util_rows if not u["driver"]), None)
    d = next((u for u in util_rows if u["driver"]), None)
    b = 300 if days <= 2 else 1800 if days <= 14 else 3600
    pts = pa_item["series"]
    ts, heat = [], {}
    for (t0, wk), (t1, _) in zip(pts, pts[1:]):
        if wk is None:
            continue
        t = (t0 // (b * 1000)) * b * 1000
        while t < t1:
            hour = dt.datetime.utcfromtimestamp(t / 1000)
            diurnal = 0.55 + 0.45 * math.sin(math.pi * max(0, min(1, (hour.hour - 7) / 11)))
            for role, base in (("workers", w), ("driver", d)):
                if not base:
                    continue
                cpu = max(0.5, min(100, base["cpu_p95"] * diurnal * rng.uniform(0.35, 1.0)))
                mem = max(5, min(100, base["mem_p95"] * rng.uniform(0.7, 1.0)))
                ts.append({"t": dt.datetime.utcfromtimestamp(t / 1000).isoformat(), "driver": role == "driver", "cpu": cpu,
                           "cpu_max": min(100, cpu * 1.4), "mem": mem, "mem_max": min(100, mem * 1.05),
                           "nodes": (wk if role == "workers" else 1)})
                k = (hour.date().isoformat(), hour.hour, role == "driver")
                a = heat.setdefault(k, [0, 0, 0])
                a[0] += cpu; a[1] += mem; a[2] += 1
            t += b * 1000
    heatmap = [{"d": k[0], "h": k[1], "driver": k[2], "cpu": v[0] / v[2], "mem": v[1] / v[2], "minutes": v[2] * b // 60}
               for k, v in sorted(heat.items())]
    nodes = []
    for j in range(min(12, (w or {}).get("instances", 0))):
        cpu = w["cpu_p95"] * rng.uniform(0.6, 1.15)
        nodes.append({"instance_id": f"demo-{j:03d}", "driver": False, "node_type": w["node_type"], "minutes": int(w["node_minutes"] / max(1, w["instances"])),
                      "cpu_avg": cpu * 0.55, "cpu_p95": cpu, "mem_avg": w["mem_p95"] * 0.8, "mem_p95": w["mem_p95"] * rng.uniform(0.85, 1.05),
                      "wait_avg": w["wait_avg"], "swap_max": w["swap_max"]})
    if d:
        nodes.insert(0, {"instance_id": "demo-driver", "driver": True, "node_type": d["node_type"], "minutes": d["node_minutes"],
                         "cpu_avg": d["cpu_avg"], "cpu_p95": d["cpu_p95"], "mem_avg": d["mem_avg"], "mem_p95": d["mem_p95"],
                         "wait_avg": d["wait_avg"], "swap_max": d["swap_max"]})
    long_cmds = sorted(({"email": u["email"], "src": u["src"], "event_time": u["last_seen"], "secs": u["secs_max"] * rng.uniform(0.4, 1),
                         "status": "finished"} for u in users_rows), key=lambda x: -x["secs"])[:15]
    daily = {}
    for (t0, wk), (t1, _) in zip(pts, pts[1:]):
        if wk is not None:
            day = dt.datetime.utcfromtimestamp(t0 / 1000).date().isoformat()
            daily.setdefault(day, 0)
            daily[day] += (t1 - t0) / 3.6e6
    dc = [{"d": k, "commands": int(v * rng.uniform(8, 30)), "users": max(1, int(len(users_rows) * rng.uniform(0.3, 1))),
           "secs": v * 600} for k, v in sorted(daily.items())]
    return {"timeseries": {"rows": ts}, "heatmap": {"rows": heatmap}, "nodes": {"rows": nodes},
            "long_commands": {"rows": long_cmds}, "daily_commands": {"rows": dc}, "bucket_seconds": b}
