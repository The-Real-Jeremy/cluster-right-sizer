"""Phase B: system tables, queried AS THE VIEWER through a SQL warehouse.

Every query filters on workspace_id: system tables are regional and account-wide, so an unfiltered read
would report other workspaces' clusters (and, for billing, the whole account's spend).
Every query is optional: a panel whose table the viewer cannot read is reported as unavailable, never as
"no data".
"""
from concurrent.futures import ThreadPoolExecutor

from . import config, dbapi

CPU = "(cpu_user_percent + cpu_system_percent)"


def _ws_filter(col="workspace_id"):
    ids = config.WORKSPACE_IDS
    if not ids:
        raise dbapi.ApiError(400, "NO_WORKSPACE", "no workspace id configured (DATABRICKS_WORKSPACE_ID / CRS_WORKSPACE_IDS)")
    return f"{col} IN (" + ", ".join(f"'{i}'" for i in ids) + ")"


def q_util(days):
    return f"""
SELECT workspace_id, cluster_id, driver,
       count(*) AS node_minutes, count(DISTINCT instance_id) AS instances, any_value(node_type) AS node_type,
       avg({CPU}) AS cpu_avg, percentile_approx({CPU}, 0.5) AS cpu_p50, percentile_approx({CPU}, 0.95) AS cpu_p95, max({CPU}) AS cpu_max,
       avg(mem_used_percent) AS mem_avg, percentile_approx(mem_used_percent, 0.95) AS mem_p95, max(mem_used_percent) AS mem_max,
       avg(cpu_wait_percent) AS wait_avg, percentile_approx(cpu_wait_percent, 0.95) AS wait_p95, max(mem_swap_percent) AS swap_max,
       sum(CASE WHEN {CPU} < 5 THEN 1 ELSE 0 END) AS idle_minutes,
       min(start_time) AS first_seen, max(end_time) AS last_seen
FROM system.compute.node_timeline
WHERE {_ws_filter()} AND start_time >= timestampadd(DAY, -{int(days)}, current_timestamp())
GROUP BY ALL"""


def q_clusters(days):
    return f"""
SELECT workspace_id, cluster_id, cluster_name, owned_by, driver_node_type, worker_node_type, worker_count,
       min_autoscale_workers, max_autoscale_workers, auto_termination_minutes, cluster_source, data_security_mode,
       dbr_version, policy_id, delete_time, change_time
FROM system.compute.clusters
WHERE {_ws_filter()} AND cluster_source IN ('UI', 'API')
  AND (delete_time IS NULL OR delete_time >= timestampadd(DAY, -{int(days)}, current_timestamp()))
QUALIFY row_number() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC) = 1"""


Q_NODE_TYPES = "SELECT node_type, core_count, memory_mb, gpu_count FROM system.compute.node_types"


def q_usage(days, with_prices):
    price = """
LEFT JOIN system.billing.list_prices p
  ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
 AND u.usage_end_time >= p.price_start_time AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)""" if with_prices else ""
    cost = ", sum(u.usage_quantity * p.pricing.default) AS list_cost, max(p.currency_code) AS currency" if with_prices else ""
    return f"""
SELECT u.workspace_id, u.usage_metadata.cluster_id AS cluster_id, sum(u.usage_quantity) AS dbus{cost},
       min(u.usage_date) AS first_day, max(u.usage_date) AS last_day
FROM system.billing.usage u{price}
WHERE {_ws_filter('u.workspace_id')} AND u.billing_origin_product = 'ALL_PURPOSE'
  AND u.usage_metadata.cluster_id IS NOT NULL AND u.usage_date >= date_sub(current_date(), {int(days)})
GROUP BY ALL"""


def q_audit_users(days):
    """Fleet view: who touched each cluster and how often. No join (the join is per cluster, in the detail view),
    because system.access.audit is region-wide and can be very large."""
    return f"""
SELECT workspace_id, request_params['clusterId'] AS cluster_id, user_identity.email AS email,
       CASE WHEN service_name = 'jobs' THEN 'job' WHEN action_name = 'attachNotebook' THEN 'attach' ELSE 'notebook' END AS src,
       count(*) AS commands, count(try_cast(request_params['executionTime'] AS DOUBLE)) AS timed,
       sum(try_cast(request_params['executionTime'] AS DOUBLE)) AS secs_total,
       percentile_approx(try_cast(request_params['executionTime'] AS DOUBLE), 0.5) AS secs_p50,
       percentile_approx(try_cast(request_params['executionTime'] AS DOUBLE), 0.95) AS secs_p95,
       max(try_cast(request_params['executionTime'] AS DOUBLE)) AS secs_max,
       sum(CASE WHEN lower(request_params['status']) IN ('error', 'failed', 'failure') THEN 1 ELSE 0 END) AS failed,
       max(event_time) AS last_seen
FROM system.access.audit
WHERE {_ws_filter()} AND event_date >= date_sub(current_date(), {int(days)})
  AND ((service_name = 'notebook' AND action_name IN ('submitCommand', 'attachNotebook'))
       OR (service_name = 'jobs' AND action_name = 'runCommand'))
  AND request_params['clusterId'] IS NOT NULL
GROUP BY ALL"""


def _audit_cluster_cte(days):
    """Detail view: one cluster's commands with their run times (submitCommand joined to runCommand)."""
    w = f"{_ws_filter()} AND event_date >= date_sub(current_date(), {int(days)})"
    return f"""
WITH sub AS (
  SELECT request_params['commandId'] AS cmd, user_identity.email AS email, event_time
  FROM system.access.audit WHERE {w} AND service_name = 'notebook' AND action_name = 'submitCommand'
    AND request_params['clusterId'] = :cid),
run AS (
  SELECT request_params['commandId'] AS cmd, try_cast(request_params['executionTime'] AS DOUBLE) AS secs,
         lower(request_params['status']) AS status
  FROM system.access.audit WHERE {w} AND service_name = 'notebook' AND action_name = 'runCommand'
    AND event_date IN (SELECT DISTINCT date(event_time) FROM sub)
    AND request_params['commandId'] IN (SELECT cmd FROM sub)),
cmds AS (
  SELECT s.email, 'notebook' AS src, s.event_time, r.secs, r.status FROM sub s LEFT JOIN run r ON s.cmd = r.cmd
  UNION ALL
  SELECT user_identity.email, 'job', event_time, try_cast(request_params['executionTime'] AS DOUBLE), lower(request_params['status'])
  FROM system.access.audit WHERE {w} AND service_name = 'jobs' AND action_name = 'runCommand' AND request_params['clusterId'] = :cid)"""


# ── per-cluster detail ───────────────────────────────────────────────────────────────────────────────
def bucket_seconds(days):
    return 300 if days <= 2 else 1800 if days <= 14 else 3600


def q_timeseries(days):
    b = bucket_seconds(days)
    return f"""
SELECT timestamp_seconds(floor(unix_timestamp(start_time) / {b}) * {b}) AS t, driver,
       avg({CPU}) AS cpu, max({CPU}) AS cpu_max, avg(mem_used_percent) AS mem, max(mem_used_percent) AS mem_max,
       count(DISTINCT instance_id) AS nodes
FROM system.compute.node_timeline
WHERE {_ws_filter()} AND cluster_id = :cid AND start_time >= timestampadd(DAY, -{int(days)}, current_timestamp())
GROUP BY ALL ORDER BY t"""


def q_heatmap(days):
    return f"""
SELECT date(ts) AS d, hour(ts) AS h, driver, avg(cpu) AS cpu, avg(mem) AS mem, count(*) AS minutes
FROM (SELECT from_utc_timestamp(start_time, :tz) AS ts, driver, {CPU} AS cpu, mem_used_percent AS mem
      FROM system.compute.node_timeline
      WHERE {_ws_filter()} AND cluster_id = :cid AND start_time >= timestampadd(DAY, -{int(days)}, current_timestamp()))
GROUP BY ALL ORDER BY d, h"""


def q_nodes(days):
    return f"""
SELECT instance_id, driver, any_value(node_type) AS node_type, count(*) AS minutes,
       avg({CPU}) AS cpu_avg, percentile_approx({CPU}, 0.95) AS cpu_p95, avg(mem_used_percent) AS mem_avg,
       percentile_approx(mem_used_percent, 0.95) AS mem_p95, avg(cpu_wait_percent) AS wait_avg, max(mem_swap_percent) AS swap_max,
       min(start_time) AS first_seen, max(end_time) AS last_seen
FROM system.compute.node_timeline
WHERE {_ws_filter()} AND cluster_id = :cid AND start_time >= timestampadd(DAY, -{int(days)}, current_timestamp())
GROUP BY instance_id, driver ORDER BY driver DESC, first_seen DESC LIMIT 200"""


def q_cluster_audit(days):
    """One scan for the whole Users panel: per-user totals, per-day totals and the 15 longest commands."""
    return _audit_cluster_cte(days) + """,
agg AS (
  SELECT CASE WHEN grouping(email) = 0 THEN 'user' ELSE 'day' END AS kind, email, src, date(event_time) AS d,
         count(*) AS commands, count(secs) AS timed, sum(secs) AS secs_total,
         percentile_approx(secs, 0.5) AS secs_p50, percentile_approx(secs, 0.95) AS secs_p95, max(secs) AS secs_max,
         sum(CASE WHEN status IN ('error', 'failed', 'failure') THEN 1 ELSE 0 END) AS failed, max(event_time) AS last_seen,
         count(DISTINCT email) AS users, CAST(NULL AS STRING) AS status
  FROM cmds GROUP BY GROUPING SETS ((email, src), (date(event_time))))
SELECT * FROM agg"""


def split_cluster_audit(rows):
    users = [r for r in rows if r["kind"] == "user"]
    daily = sorted(({"d": r["d"], "commands": r["commands"], "users": r["users"], "secs": r["secs_total"]}
                    for r in rows if r["kind"] == "day" and r["d"]), key=lambda x: x["d"])
    top = sorted(({"email": r["email"], "src": r["src"], "event_time": r["last_seen"], "secs": r["secs_max"], "status": None}
                  for r in users if r["secs_max"] is not None), key=lambda x: -(x["secs"] or 0))[:15]
    return users, daily, top


def _run(viewer, wh, name, stmt, params=None):
    try:
        return name, {"rows": dbapi.sql(viewer, wh, stmt, params)}
    except dbapi.ApiError as e:
        return name, {"error": e.public()}


def audit_fleet(viewer, wh, access, days):
    if access["tables"].get("audit", {}).get("state") != "granted":
        return {"skipped": True}
    return _run(viewer, wh, "audit_users", q_audit_users(days))[1]


def fleet(viewer, wh, access, days):
    """All fleet-level queries the viewer's grants allow, in parallel. Returns {name: {rows|error|skipped}}."""
    t = access["tables"]
    ok = lambda k: t.get(k, {}).get("state") == "granted"
    jobs = []
    if ok("node_timeline"):
        jobs.append(("util", q_util(days)))
    if ok("clusters"):
        jobs.append(("clusters", q_clusters(days)))
    if ok("node_types"):
        jobs.append(("node_types", Q_NODE_TYPES))
    if ok("usage"):
        jobs.append(("usage", q_usage(days, ok("list_prices"))))
    out = {k: {"skipped": True} for k in ("util", "clusters", "node_types", "usage", "audit_users", "attach")}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for name, res in ex.map(lambda j: _run(viewer, wh, *j), jobs):
            out[name] = res
    return out


def detail(viewer, wh, access, cluster_id, days, part="core"):
    """part="core": node_timeline views (fast). part="audit": users and run times (the audit table can be slow)."""
    t = access["tables"]
    ok = lambda k: t.get(k, {}).get("state") == "granted"
    p = {"cid": cluster_id}
    jobs, names = [], []
    if part == "core":
        names = ["timeseries", "heatmap", "nodes"]
        if ok("node_timeline"):
            jobs += [("timeseries", q_timeseries(days), p), ("heatmap", q_heatmap(days), dict(p, tz=config.TIMEZONE)),
                     ("nodes", q_nodes(days), p)]
    else:
        names = ["long_commands", "daily_commands", "cluster_users"]
        if ok("audit"):
            name, res = _run(viewer, wh, "cluster_audit", q_cluster_audit(days), p)
            if "rows" in res:
                u, d, t = split_cluster_audit(res["rows"])
                return {"cluster_users": {"rows": u}, "daily_commands": {"rows": d}, "long_commands": {"rows": t},
                        "bucket_seconds": bucket_seconds(days)}
            return {k: res for k in names} | {"bucket_seconds": bucket_seconds(days)}
    out = {k: {"skipped": True} for k in names}
    with ThreadPoolExecutor(max_workers=3) as ex:
        for name, res in ex.map(lambda j: _run(viewer, wh, *j), jobs):
            out[name] = res
    out["bucket_seconds"] = bucket_seconds(days)
    return out
