"""What can THIS viewer read? Decided per viewer, cached briefly, never guessed.

Each table is one of three states, and the third is not folded into either of the others:
  granted   a LIMIT 0 probe succeeded
  denied    the probe failed with a permission / not-found error (the grant is missing)
  unknown   we could not look (no warehouse, no sql scope, warehouse down, timeout)
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import config, dbapi

TABLES = {
    "node_timeline": "system.compute.node_timeline",
    "clusters": "system.compute.clusters",
    "node_types": "system.compute.node_types",
    "usage": "system.billing.usage",
    "list_prices": "system.billing.list_prices",
    "audit": "system.access.audit",
}

# Which panels each table unlocks (shown in the UI next to the grant that is missing).
UNLOCKS = {
    "node_timeline": "Per-node CPU and memory, driver vs workers, heatmaps, utilisation-based recommendations",
    "clusters": "Cluster configuration history (also for clusters the app cannot see through the API)",
    "node_types": "Node sizes for smaller-node suggestions (falls back to the Clusters API list)",
    "usage": "DBUs per cluster",
    "list_prices": "List-price cost per cluster",
    "audit": "Who is using each cluster, command run times",
}

_cache = {}
_lock = threading.Lock()
TTL = 300


def grants_sql(principal="`<user or group>`"):
    """The exact grants a metastore / account admin runs, per schema."""
    return [
        f"GRANT USE CATALOG ON CATALOG system TO {principal};",
        f"GRANT USE SCHEMA, SELECT ON SCHEMA system.compute TO {principal};",
        f"GRANT USE SCHEMA, SELECT ON SCHEMA system.billing TO {principal};",
        f"GRANT USE SCHEMA, SELECT ON SCHEMA system.access TO {principal};",
    ]


def _classify(err):
    m = (err.message or "").lower()
    if err.kind() == "scope":
        return "unknown", "The app has no 'sql' user-authorization scope, so it cannot run SQL as you."
    if "warehouse" in m and ("permission" in m or "can use" in m or "not authorized" in m):
        return "unknown", "You do not have CAN USE on the SQL warehouse."
    if any(s in m for s in ("insufficient_permissions", "permission_denied", "does not have", "privilege",
                            "table_or_view_not_found", "not found", "cannot be found", "schema_not_found",
                            "catalog_not_found", "access denied", "user does not have")):
        return "denied", err.message[:240]
    return "unknown", err.message[:240]


def pick_warehouse(viewer):
    """The configured warehouse, else the best one the viewer can see. Returns (id, name, how)."""
    if config.WAREHOUSE_ID:
        return config.WAREHOUSE_ID, None, "configured (CRS_WAREHOUSE_ID)"
    try:
        whs = viewer.get("/api/2.0/sql/warehouses").get("warehouses") or []
    except dbapi.ApiError as e:
        return None, None, f"could not list warehouses: {e.kind()} ({e.message[:120]})"
    if not whs:
        return None, None, "no SQL warehouse is visible to you"
    rank = lambda w: (w.get("state") != "RUNNING", not w.get("enable_serverless_compute"), w.get("name", ""))
    w = sorted(whs, key=rank)[0]
    return w["id"], w.get("name"), "auto-picked (running and serverless preferred)"


def probe(viewer, viewer_key, force=False):
    key = viewer_key or "anonymous"
    with _lock:
        hit = _cache.get(key)
        if hit and not force and time.time() - hit["at"] < TTL:
            return hit["value"]
    wh, wh_name, how = pick_warehouse(viewer)
    out = {"warehouse": {"id": wh, "name": wh_name, "how": how}, "tables": {}, "checked_at": time.time()}
    if not wh:
        for k, t in TABLES.items():
            out["tables"][k] = {"table": t, "state": "unknown", "reason": how, "unlocks": UNLOCKS[k]}
    else:
        def one(item):
            k, t = item
            try:
                dbapi.sql(viewer, wh, f"SELECT 1 FROM {t} LIMIT 0", timeout_s=60)
                return k, {"table": t, "state": "granted", "unlocks": UNLOCKS[k]}
            except dbapi.ApiError as e:
                state, reason = _classify(e)
                return k, {"table": t, "state": state, "reason": reason, "unlocks": UNLOCKS[k]}
        with ThreadPoolExecutor(max_workers=len(TABLES)) as ex:
            for k, v in ex.map(one, TABLES.items()):
                out["tables"][k] = v
    t = out["tables"]
    out["phase_b"] = t["node_timeline"]["state"] == "granted"
    out["grants"] = grants_sql()
    out["any_unknown"] = any(v["state"] == "unknown" for v in t.values())
    with _lock:
        _cache[key] = {"at": time.time(), "value": out}
    return out


def app_access(app):
    """Can the app's service principal call the Clusters API at all? (Phase A availability.)"""
    try:
        app.get("/api/2.1/clusters/list", query={"page_size": 1})
        return {"state": "granted"}
    except dbapi.ApiError as e:
        return {"state": "denied" if e.kind() == "permission" else "unknown", "reason": e.message[:240]}
