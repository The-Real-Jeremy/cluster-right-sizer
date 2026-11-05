"""Everything read from the environment, in one place.

The app runs three ways: as a standalone Databricks App, as a child of App-ception, and on a laptop.
Each setting below says what happens when it is absent, so a missing value is a named state, not a crash.
"""
import os
import re


def _int(name, default, lo, hi):
    try:
        v = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def _ids(raw):
    """Comma/space separated workspace ids. Only digits survive, so the list is safe to inline in SQL."""
    return [x for x in re.split(r"[\s,]+", raw or "") if x.isdigit()]


# Databricks injects DATABRICKS_HOST without a scheme inside Apps; a laptop profile usually has one.
_host = (os.environ.get("DATABRICKS_HOST") or "").strip().rstrip("/")
HOST = ("https://" + _host) if _host and not _host.startswith("http") else _host

CLIENT_ID = os.environ.get("DATABRICKS_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("DATABRICKS_CLIENT_SECRET", "")
WORKSPACE_ID = (os.environ.get("DATABRICKS_WORKSPACE_ID") or os.environ.get("CRS_WORKSPACE_ID") or "").strip()

# Laptop only: one token used for both the "viewer" and the "app" identity. Never set this in a deployed app.
DEV_TOKEN = os.environ.get("CRS_DEV_TOKEN", "")

PORT = _int("DATABRICKS_APP_PORT", int(os.environ.get("PORT", "8000") or 8000), 1, 65535)
BUILD_ID = os.environ.get("BUILD_ID", "dev")

# SQL warehouse for the system-table panels. Empty = pick one the viewer can see (RUNNING first, serverless first).
WAREHOUSE_ID = os.environ.get("CRS_WAREHOUSE_ID", "").strip()

# Which workspaces the system-table panels cover. System tables are regional and account-wide, so a query
# that does not filter on workspace_id reports other workspaces' clusters. Default: this workspace only.
EXTRA_WORKSPACE_IDS = _ids(os.environ.get("CRS_WORKSPACE_IDS", ""))
WORKSPACE_IDS = list(dict.fromkeys(([WORKSPACE_ID] if WORKSPACE_ID.isdigit() else []) + EXTRA_WORKSPACE_IDS))

LOOKBACK_DAYS = _int("CRS_LOOKBACK_DAYS", 30, 1, 90)   # node_timeline keeps 90 days
EVENT_DAYS_MAX = 60                                    # the cluster event log keeps 60 days
TIMEZONE = os.environ.get("CRS_TIMEZONE", "UTC").strip() or "UTC"

# Synthetic demo data: "off" hides the switch, "allowed" shows it (default), "default" starts in demo mode.
DEMO = (os.environ.get("CRS_DEMO", "allowed").strip().lower() or "allowed")
if DEMO not in ("off", "allowed", "default"):
    DEMO = "allowed"

# Optional Genie space for the "Ask Genie" panel (written by DEPLOY genie_mode=create|existing). Empty = hidden.
GENIE_SPACE_ID = os.environ.get("CRS_GENIE_SPACE_ID", "").strip()

# Diagnostic only, OFF by default: "?as=app" views the app as its own service principal (a non-admin identity with
# no grants), to show what a viewer without permissions sees. Never enable it where the SP has more rights than viewers.
SP_DIAGNOSTICS = os.environ.get("CRS_SP_DIAGNOSTICS", "").strip() == "1"

# Mounted under App-ception the proxy sends x-forwarded-prefix; the page itself only uses relative URLs.
CATALOG_MOUNT = os.environ.get("CATALOG_MOUNT", "")

# Thresholds for the right-sizing rules (percent). Starting points, tunable without a code change.
T_WORKER_CPU_P95 = _int("CRS_T_WORKER_CPU_P95", 40, 1, 100)
T_WORKER_MEM_P95 = _int("CRS_T_WORKER_MEM_P95", 60, 1, 100)
T_DRIVER_CPU_P95 = _int("CRS_T_DRIVER_CPU_P95", 20, 1, 100)
T_DRIVER_MEM_P95 = _int("CRS_T_DRIVER_MEM_P95", 50, 1, 100)
T_AUTOTERM_MAX = _int("CRS_T_AUTOTERM_MAX", 60, 10, 10000)
T_AUTOTERM_TARGET = _int("CRS_T_AUTOTERM_TARGET", 30, 10, 10000)


def describe():
    """Safe-to-show effective settings (no secrets)."""
    return {
        "host": HOST,
        "workspace_id": WORKSPACE_ID or None,
        "workspace_ids": WORKSPACE_IDS,
        "warehouse_id": WAREHOUSE_ID or None,
        "lookback_days": LOOKBACK_DAYS,
        "event_days_max": EVENT_DAYS_MAX,
        "timezone": TIMEZONE,
        "demo": DEMO,
        "sp_diagnostics": SP_DIAGNOSTICS,
        "genie_space_id": GENIE_SPACE_ID or None,
        "app_identity": "service principal" if CLIENT_ID else ("dev token" if DEV_TOKEN else "none"),
        "thresholds": {
            "worker_cpu_p95": T_WORKER_CPU_P95, "worker_mem_p95": T_WORKER_MEM_P95,
            "driver_cpu_p95": T_DRIVER_CPU_P95, "driver_mem_p95": T_DRIVER_MEM_P95,
            "autoterm_max": T_AUTOTERM_MAX, "autoterm_target": T_AUTOTERM_TARGET,
        },
        "build": BUILD_ID,
    }
