"""Optional "Ask Genie" panel: a Genie space over the right-sizing system tables, asked AS THE VIEWER.

No FMAPI: Genie runs on Databricks-hosted "partner-powered AI" (enabled per account/workspace), and every
generated query runs with the viewer's own grants on the space's warehouse. The app needs the `genie`
user-authorization scope; the viewer needs CAN RUN on the space, CAN USE on its warehouse and SELECT on
the tables it reads. Each of those is probed and named when missing.
"""
import time

from . import config, dbapi

_cache = {}
TTL = 300


def probe(viewer, viewer_key):
    """off | ready | no_scope | no_access | unavailable (with a reason)."""
    sid = config.GENIE_SPACE_ID
    if not sid:
        return {"state": "off", "reason": "No Genie space is configured (DEPLOY genie_mode = off)."}
    hit = _cache.get(viewer_key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    try:
        s = viewer.get(f"/api/2.0/genie/spaces/{sid}")
        out = {"state": "ready", "space_id": sid, "title": s.get("title"), "warehouse_id": s.get("warehouse_id")}
    except dbapi.ApiError as e:
        m = (e.message or "").lower()
        if e.kind() == "scope" or "scope" in m:
            out = {"state": "no_scope", "space_id": sid, "reason": "The app does not have the 'genie' user-authorization scope yet (add it in the app's settings; each viewer consents once)."}
        elif e.status in (403, 404) or e.kind() == "permission":
            out = {"state": "no_access", "space_id": sid, "reason": "You do not have CAN RUN on the Genie space (or it was deleted).",
                   "fix": f"Genie space → Share → add yourself with Can run, or PATCH /api/2.0/permissions/genie/{sid}"}
        else:
            out = {"state": "unavailable", "space_id": sid, "reason": f"Genie answered {e.status}: {e.message[:200]}. Partner-powered AI may be off for this workspace."}
    _cache[viewer_key] = (time.time(), out)
    return out


def ask(viewer, question, conversation_id=None):
    sid = config.GENIE_SPACE_ID
    q = (question or "").strip()[:2000]
    if not q:
        raise dbapi.ApiError(400, "EMPTY", "Ask a question.")
    if conversation_id:
        r = viewer.post(f"/api/2.0/genie/spaces/{sid}/conversations/{conversation_id}/messages", {"content": q})
        return {"conversation_id": conversation_id, "message_id": r.get("message_id") or r.get("id")}
    r = viewer.post(f"/api/2.0/genie/spaces/{sid}/start-conversation", {"content": q})
    return {"conversation_id": r["conversation_id"], "message_id": r["message_id"]}


def poll(viewer, conversation_id, message_id):
    """One poll. The page calls this every ~2 s until state is final (Genie typically takes 10-40 s)."""
    sid = config.GENIE_SPACE_ID
    m = viewer.get(f"/api/2.0/genie/spaces/{sid}/conversations/{conversation_id}/messages/{message_id}")
    status = m.get("status")
    out = {"status": status, "done": status in ("COMPLETED", "FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"),
           "text": None, "sql": None, "description": None, "columns": [], "rows": [], "error": None}
    if m.get("error"):
        out["error"] = (m["error"].get("error") if isinstance(m["error"], dict) else str(m["error"]))[:400]
    for a in m.get("attachments") or []:
        if a.get("text"):
            out["text"] = (a["text"].get("content") or "")[:4000]
        if a.get("query"):
            out["sql"] = a["query"].get("query")
            out["description"] = a["query"].get("description")
            if out["done"] and status == "COMPLETED":
                try:
                    res = viewer.get(f"/api/2.0/genie/spaces/{sid}/conversations/{conversation_id}/messages/{message_id}"
                                     f"/attachments/{a['attachment_id']}/query-result")
                    sr = res.get("statement_response") or {}
                    out["columns"] = [c["name"] for c in (sr.get("manifest") or {}).get("schema", {}).get("columns", [])]
                    out["rows"] = ((sr.get("result") or {}).get("data_array") or [])[:50]
                except dbapi.ApiError as e:
                    out["error"] = f"Could not read the result: {e.message[:200]}"
    return out


def questions_for(cluster_name, cluster_id):
    """Pre-filled questions for the cluster in view."""
    c = f"'{cluster_name}' ({cluster_id})" if cluster_name else f"cluster {cluster_id}"
    return [
        f"What were the average and p95 CPU and memory of the workers and the driver of {c} in the last 14 days?",
        f"Which hours of the day is {c} busiest, by average worker CPU?",
        f"Who ran the most notebook commands on {c} in the last 30 days, and for how long in total?",
        f"How many DBUs did {c} use per day in the last 30 days?",
        f"How many worker nodes did {c} run at its peak each day?",
    ]
