"""Merge Phase A (Clusters/Events API) and Phase B (system tables) into one record per cluster, then apply
the right-sizing rules. Every recommendation carries its evidence and says which data it rests on.

Thresholds are starting points (config.T_*), not Databricks-official limits.
"""
import math
import re

from . import config


def _key(ws, cid):
    return f"{ws}:{cid}"


def _family(name):
    """'m5d.xlarge' -> 'm5d.' ; 'Standard_D16s_v3' -> 'Standard_D#s_v#' ; 'n2-highmem-8' -> 'n2-highmem-#'."""
    if not name:
        return None
    if "." in name and not name.startswith("Standard_"):
        return name.split(".")[0] + "."
    return re.sub(r"\d+", "#", name)


def smaller_node(name, ntypes, mem_needed_mb=None):
    cur = ntypes.get(name) or {}
    cores, fam = cur.get("cores"), _family(name)
    if not cores or not fam:
        return None
    cands = [(n, v) for n, v in ntypes.items()
             if n != name and _family(n) == fam and not v.get("deprecated")
             and v.get("cores") and v["cores"] <= cores / 2
             and (mem_needed_mb is None or (v.get("memory_mb") or 0) >= mem_needed_mb)]
    if not cands:
        return None
    n, v = max(cands, key=lambda kv: (kv[1]["cores"], kv[1].get("memory_mb") or 0))
    return {"node_type": n, "cores": v["cores"], "memory_gb": round((v.get("memory_mb") or 0) / 1024, 1)}


def _r(x, n=1):
    return None if x is None else round(float(x), n)


def merge(ws_id, phase_a, b, ntypes):
    """Build {key: record}. phase_a: list from phase_a.fleet or None. b: dict from phase_b.fleet or None."""
    recs = {}

    def rec(ws, cid):
        k = _key(ws, cid)
        if k not in recs:
            recs[k] = {"key": k, "workspace_id": ws, "cluster_id": cid, "config": {}, "phase_a": None,
                       "util": {}, "usage": None, "users": [], "attaches": 0, "sources": [], "all_purpose": None}
        return recs[k]

    for a in phase_a or []:
        r = rec(ws_id, a["config"]["cluster_id"])
        r["config"] = dict(a["config"])
        r["phase_a"] = a["summary"]
        r["events_error"] = a.get("events_error")
        r["all_purpose"] = True
        r["sources"].append("clusters_api")

    rows = lambda name: (b or {}).get(name, {}).get("rows") or []
    for c in rows("clusters"):
        r = rec(c["workspace_id"], c["cluster_id"])
        r["all_purpose"] = True
        cfg = r["config"]
        if not cfg.get("cluster_name"):
            auto = c.get("max_autoscale_workers") is not None
            cfg.update({"cluster_id": c["cluster_id"], "cluster_name": c.get("cluster_name"), "owner": c.get("owned_by"),
                        "node_type_id": c.get("worker_node_type"), "driver_node_type_id": c.get("driver_node_type"),
                        "num_workers": c.get("worker_count"),
                        "autoscale": {"min_workers": c.get("min_autoscale_workers"), "max_workers": c.get("max_autoscale_workers")} if auto else None,
                        "autotermination_minutes": c.get("auto_termination_minutes"), "spark_version": c.get("dbr_version"),
                        "data_security_mode": c.get("data_security_mode"), "cluster_source": c.get("cluster_source"),
                        "policy_id": c.get("policy_id"), "deleted": c.get("delete_time") is not None})
        r["sources"].append("system.compute.clusters")

    known_ap = {k for k, v in recs.items() if v["all_purpose"]}
    can_classify = bool(phase_a is not None or rows("clusters"))
    for u in rows("util"):
        k = _key(u["workspace_id"], u["cluster_id"])
        if can_classify and k not in known_ap:
            continue   # a job or pipeline cluster: out of scope
        r = rec(u["workspace_id"], u["cluster_id"])
        r["util"]["driver" if u["driver"] else "workers"] = u
        if "system.compute.node_timeline" not in r["sources"]:
            r["sources"].append("system.compute.node_timeline")
    for u in rows("usage"):
        k = _key(u["workspace_id"], u["cluster_id"])
        if k in recs:
            recs[k]["usage"] = u
    for u in rows("audit_users"):
        k = _key(u["workspace_id"], u["cluster_id"])
        if k not in recs:
            continue
        if u.get("src") == "attach":
            recs[k]["attaches"] += u.get("commands") or 0
            if any(x["email"] == u["email"] for x in rows("audit_users") if x is not u and x.get("src") != "attach"
                   and x["cluster_id"] == u["cluster_id"] and x["workspace_id"] == u["workspace_id"]):
                continue
            u = dict(u, commands=0)
        recs[k]["users"].append(u)
    for a in rows("attach"):
        k = _key(a["workspace_id"], a["cluster_id"])
        if k in recs:
            recs[k]["attaches"] += a["attaches"] or 0
            if not any(x["email"] == a["email"] for x in recs[k]["users"]):
                recs[k]["users"].append({"email": a["email"], "src": "attach", "commands": 0, "secs_total": None,
                                         "last_seen": a["last_seen"]})
    for r in recs.values():
        evaluate(r, ntypes)
    return recs


def evaluate(r, ntypes):
    cfg, pa, util = r["config"], r["phase_a"], r["util"]
    w, d = util.get("workers"), util.get("driver")
    recs, notes = [], []
    auto = cfg.get("autoscale") or {}
    max_w = auto.get("max_workers") if auto else cfg.get("num_workers")
    min_w = auto.get("min_workers") if auto else cfg.get("num_workers")
    at = cfg.get("autotermination_minutes")
    usage = r.get("usage") or {}
    cost = usage.get("list_cost")
    dbus = usage.get("dbus")
    total_nm = sum((x or {}).get("node_minutes") or 0 for x in (w, d))

    def saving(frac):
        frac = max(0.0, min(1.0, frac))
        out = {"fraction": round(frac, 3)}
        if cost:
            out["list_cost"] = round(cost * frac, 2)
        if dbus:
            out["dbus"] = round(dbus * frac, 1)
        if pa and pa.get("node_hours"):
            out["node_hours"] = round(pa["node_hours"] * frac, 1)
        return out

    # ── utilisation (Phase B) ────────────────────────────────────────────────────────────────────────
    enough = lambda x: x and (x.get("node_minutes") or 0) >= 60
    worker_share = (w or {}).get("node_minutes", 0) / total_nm if total_nm else 0
    driver_share = (d or {}).get("node_minutes", 0) / total_nm if total_nm else 0
    io_bound = enough(w) and (w.get("wait_avg") or 0) >= 10
    mem_pressure = any(enough(x) and (x.get("swap_max") or 0) > 1 for x in (w, d))
    if enough(w):
        cpu95, mem95 = w.get("cpu_p95") or 0, w.get("mem_p95") or 0
        ev = [f"workers p95 CPU {cpu95:.0f}%", f"p95 memory {mem95:.0f}%", f"avg I/O wait {(w.get('wait_avg') or 0):.1f}%",
              f"{w['node_minutes']:,} node-minutes"]
        if io_bound:
            notes.append({"kind": "keep", "title": "Workers are I/O-bound", "detail": "High CPU wait: a different CPU size will not help; look at the data layout or caching first.", "evidence": ev})
        elif mem_pressure:
            notes.append({"kind": "keep", "title": "Memory swap seen", "detail": "Do not downsize while nodes swap.", "evidence": ev})
        elif cpu95 < config.T_WORKER_CPU_P95 and mem95 < config.T_WORKER_MEM_P95:
            cur_mem = (ntypes.get(cfg.get("node_type_id")) or {}).get("memory_mb")
            need = cur_mem * mem95 / 100 * 1.3 if cur_mem else None
            sm = smaller_node(cfg.get("node_type_id"), ntypes, need)
            if sm:
                recs.append({"id": "worker_node_smaller", "severity": "high", "basis": "system tables",
                             "title": f"Workers: {cfg.get('node_type_id')} → {sm['node_type']}",
                             "detail": f"Half the cores or less ({sm['cores']} cores, {sm['memory_gb']} GB) still leaves 30% memory headroom over the observed p95.",
                             "evidence": ev, "saving": saving(0.5 * worker_share)})
            elif auto and max_w and max_w > 1:
                peak = (pa or {}).get("workers_peak") or max_w
                load = max(cpu95 / 70, mem95 / 80)
                new_max = max(min_w or 1, math.ceil(peak * load))
                recs.append({"id": "worker_count_lower", "severity": "high", "basis": "system tables",
                             "title": f"Workers: lower max workers ({max_w} → {new_max})",
                             "detail": f"No smaller node in the same family fits. At this load about {new_max} workers carry the observed peak of {peak} at up to 70% CPU / 80% memory.",
                             "evidence": ev, "saving": saving(0.3 * worker_share)})
            else:
                recs.append({"id": "worker_underused", "severity": "medium", "basis": "system tables",
                             "title": "Workers are under-used", "detail": "Consider a smaller node type or fewer workers.", "evidence": ev,
                             "saving": saving(0.3 * worker_share)})
        elif cpu95 >= 85 or mem95 >= 90:
            notes.append({"kind": "keep", "title": "Workers are busy", "detail": "p95 near the ceiling: do not downsize.", "evidence": ev})
    if enough(d):
        cpu95, mem95 = d.get("cpu_p95") or 0, d.get("mem_p95") or 0
        dcores = (ntypes.get(cfg.get("driver_node_type_id") or cfg.get("node_type_id")) or {}).get("cores") or 0
        ev = [f"driver p95 CPU {cpu95:.0f}%", f"p95 memory {mem95:.0f}%"]
        if cpu95 < config.T_DRIVER_CPU_P95 and mem95 < config.T_DRIVER_MEM_P95 and dcores >= 4 and not mem_pressure:
            dn = cfg.get("driver_node_type_id") or cfg.get("node_type_id")
            dmem = (ntypes.get(dn) or {}).get("memory_mb")
            sm = smaller_node(dn, ntypes, dmem * mem95 / 100 * 1.5 if dmem else None)
            recs.append({"id": "driver_smaller", "severity": "medium", "basis": "system tables",
                         "title": f"Driver: {dn} → {sm['node_type'] if sm else 'a smaller type'}",
                         "detail": "The driver uses the worker type by default; this one is mostly idle.",
                         "evidence": ev, "saving": saving(0.5 * driver_share)})
    idle_share = None
    if total_nm:
        idle_share = sum((x or {}).get("idle_minutes") or 0 for x in (w, d)) / total_nm
        if idle_share >= 0.5 and not any(x["id"] == "worker_node_smaller" for x in recs):
            recs.append({"id": "mostly_idle", "severity": "medium", "basis": "system tables",
                         "title": f"Idle {idle_share:.0%} of the time it runs",
                         "detail": "Under 5% CPU for most running minutes: shorten auto-termination, or share a smaller cluster.",
                         "evidence": [f"{idle_share:.0%} of node-minutes under 5% CPU"], "saving": saving(idle_share * 0.5)})

    # ── configuration and lifecycle (Phase A) ────────────────────────────────────────────────────────
    if pa:
        peak, p95 = pa.get("workers_peak"), pa.get("workers_p95")
        if auto and peak is not None and max_w and peak < max_w and pa.get("running_hours", 0) >= 2 \
                and not any(x["id"] == "worker_count_lower" for x in recs):
            new_max = max(min_w or 1, peak + 1 if peak < max_w - 1 else peak)
            if max_w - new_max >= max(2, 0.25 * max_w):
                recs.append({"id": "max_workers", "severity": "medium", "basis": "events API",
                             "title": f"Max workers {max_w} → {new_max}",
                             "detail": f"It never ran more than {peak} workers in {pa['window_days']:.0f} days. A lower cap limits the worst case at no cost to what ran.",
                             "evidence": [f"peak {peak}, p95 {p95}, configured {min_w}–{max_w}", f"{pa['running_hours']} running hours"],
                             "saving": {"fraction": 0, "note": "caps exposure; no measured saving"}})
        if auto and min_w and min_w > 1 and p95 is not None and p95 <= min_w and pa.get("running_hours", 0) >= 4:
            recs.append({"id": "min_workers", "severity": "low", "basis": "events API",
                         "title": f"Min workers {min_w} → {max(1, min_w // 2)}",
                         "detail": "It sits at its minimum 95% of the running time" + ("" if w else "; confirm with utilisation (system tables) before lowering."),
                         "evidence": [f"p95 workers {p95} = minimum {min_w}"], "saving": saving(0.5 * (1 - 1 / max(min_w, 1)) * (worker_share or 0.6) * 0.5)})
        if not auto and (cfg.get("num_workers") or 0) >= 2 and pa.get("running_hours", 0) >= 8:
            n = cfg["num_workers"]
            recs.append({"id": "enable_autoscale", "severity": "medium", "basis": "events API",
                         "title": f"Fixed {n} workers → autoscale {max(1, n // 2)}–{n}",
                         "detail": "Interactive load is bursty; autoscaling releases workers between bursts.",
                         "evidence": [f"{pa['running_hours']} running hours at a fixed {n} workers"], "saving": saving(0.2 * (worker_share or 0.7))})
        inact = pa.get("inactivity_terminations", 0)
        if at == 0:
            recs.append({"id": "autoterm_enable", "severity": "high", "basis": "configuration",
                         "title": f"Turn on auto-termination ({config.T_AUTOTERM_TARGET} min)",
                         "detail": "It never stops on its own; idle hours are billed until someone terminates it.",
                         "evidence": [f"running {pa.get('running_share', 0):.0%} of the window"], "saving": saving(0.25)})
        elif at and at > config.T_AUTOTERM_MAX:
            idle_h = inact * (at - config.T_AUTOTERM_TARGET) / 60 * ((pa.get("workers_avg") or 0) + 1)
            frac = idle_h / pa["node_hours"] if pa.get("node_hours") else 0
            recs.append({"id": "autoterm_shorter", "severity": "medium" if inact else "low", "basis": "events API",
                         "title": f"Auto-termination {at} → {config.T_AUTOTERM_TARGET} min",
                         "detail": f"{inact} idle shutdowns in the window, each billed {at} idle minutes first." if inact else "Long idle tail before it stops.",
                         "evidence": [f"{inact} inactivity terminations", f"≈{idle_h:.1f} node-hours spent idling before shutdown"],
                         "saving": saving(frac)})
        if (pa.get("avg_running_hours_per_active_day") or 0) >= 20 and pa.get("active_days", 0) >= 5:
            recs.append({"id": "always_on", "severity": "medium", "basis": "events API",
                         "title": "Runs around the clock",
                         "detail": "About 24 h on every active day. If nobody uses it overnight, let it terminate, or move scheduled work to job compute.",
                         "evidence": [f"{pa['avg_running_hours_per_active_day']} h per active day over {pa['active_days']} days"], "saving": saving(0.3)})
        if pa.get("driver_cores") and pa.get("worker_cores") and pa["driver_cores"] > pa["worker_cores"] and not d:
            notes.append({"kind": "check", "title": "Driver larger than workers",
                          "detail": "Often justified (collect(), many notebooks). Driver memory in system tables shows whether it is.",
                          "evidence": [f"driver {pa['driver_cores']} cores vs worker {pa['worker_cores']}"]})

    # ── users ────────────────────────────────────────────────────────────────────────────────────────
    users = sorted({u["email"] for u in r["users"] if u.get("email")})
    r["user_count"] = len(users)
    r["commands"] = sum(u.get("commands") or 0 for u in r["users"] if u.get("src") != "attach")
    if r["users"] and len(users) == 1 and max_w and max_w >= 4:
        recs.append({"id": "single_user", "severity": "low", "basis": "audit logs",
                     "title": "One user on a shared-size cluster",
                     "detail": "Only one person used it in the window; a personal or single-node compute may be enough.",
                     "evidence": [f"user: {users[0]}"], "saving": saving(0.2)})

    # ── score ────────────────────────────────────────────────────────────────────────────────────────
    score_b = None
    if enough(w) or enough(d):
        x = w if enough(w) else d
        util_frac = max((x.get("cpu_p95") or 0) / 85, (x.get("mem_p95") or 0) / 90)
        score_b = 100 * (1 - max(0.0, min(1.0, util_frac)))
        if io_bound or mem_pressure:
            score_b *= 0.4
    score_a = 0.0
    if pa:
        if auto and pa.get("workers_peak") is not None and max_w:
            score_a += 35 * (1 - pa["workers_peak"] / max_w)
        if at == 0:
            score_a += 35
        elif at and at > config.T_AUTOTERM_MAX:
            score_a += min(30, 30 * at / 240)
        if not auto and (cfg.get("num_workers") or 0) >= 2:
            score_a += 15
        if (pa.get("avg_running_hours_per_active_day") or 0) >= 20:
            score_a += 20
        score_a = min(100.0, score_a)
    score = (0.75 * score_b + 0.25 * score_a) if score_b is not None else (score_a if pa else None)
    r["score"] = None if score is None else round(score)
    r["score_basis"] = "utilisation + configuration" if score_b is not None else ("configuration and events only" if pa else "none")
    sev = {"high": 0, "medium": 1, "low": 2}
    r["recommendations"] = sorted(recs, key=lambda x: (sev.get(x["severity"], 3), -(x.get("saving") or {}).get("fraction", 0)))
    r["notes"] = notes
    r["idle_share"] = _r(idle_share, 3)
    # Savings overlap (a smaller node AND a shorter idle tail), so combine them, never add them.
    keep = 1.0
    for x in recs:
        keep *= 1 - min(0.9, (x.get("saving") or {}).get("fraction", 0))
    best = min(0.8, 1 - keep)
    r["saving"] = {"fraction": round(best, 3), "list_cost": round(cost * best, 2) if cost else None,
                   "dbus": round(dbus * best, 1) if dbus else None,
                   "node_hours": round(pa["node_hours"] * best, 1) if pa and pa.get("node_hours") else None}
