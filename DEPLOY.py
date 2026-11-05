# Databricks notebook source
# MAGIC %md
# MAGIC # Deploy Cluster Right-Sizer (browser only)
# MAGIC
# MAGIC Two ways, chosen with the **mode** widget:
# MAGIC - **child**: add it to an App-ception catalog that you deployed from `app-ception-main/` (the default).
# MAGIC   It copies this folder into the catalog's `child_apps/cluster_right_sizer/`, adds the `sql` scope this app needs
# MAGIC   to the catalog app, and redeploys the catalog.
# MAGIC - **standalone**: deploy it as its own Databricks App.
# MAGIC
# MAGIC Needs no CLI, pip or npm: only the Databricks SDK the notebook already has. Everything the app needs is in
# MAGIC this folder (standard-library Python, no CDN), so it installs and runs air-gapped.
# MAGIC
# MAGIC **No grant step.** Each viewer reads clusters with their OWN personal access token (entered once in the app) and
# MAGIC system tables with their OWN grants (on-behalf-of, `sql` scope). The app's service principal needs nothing.
# MAGIC Optional settings below are written into the app's `app.yaml`.

# COMMAND ----------

import os, re, shutil, time, json
ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
SRC = "/Workspace" + os.path.dirname(ctx.notebookPath().get())
PARENT = os.path.dirname(SRC)
# Folder preference: app-ception-main, then app-catalog-main (the old name), then the first sibling with catalog/server.mjs.
# (An old copy renamed aside must not win over the canonical name.)
_cands = [d for d in sorted(os.listdir(PARENT)) if os.path.isfile(os.path.join(PARENT, d, "catalog", "server.mjs"))]
guess = next((os.path.join(PARENT, n) for n in ("app-ception-main", "app-catalog-main") if n in _cands),
             os.path.join(PARENT, _cands[0]) if _cands else "")

dbutils.widgets.dropdown("mode", "child", ["child", "standalone"], "1. Mode")
dbutils.widgets.text("catalog_folder", guess, "2. (child) App-ception folder")
dbutils.widgets.text("catalog_app_name", "app-ception", "3. (child) App-ception app name")
dbutils.widgets.text("budget_policy_id", "", "5. (standalone) Budget policy id, if your workspace requires one")
dbutils.widgets.text("standalone_app_name", "cluster-right-sizer", "4. (standalone) App name")
dbutils.widgets.text("warehouse_id", "", "6. (optional) SQL warehouse id; empty = auto-pick per viewer")
dbutils.widgets.text("workspace_ids", "", "7. (optional) extra workspace ids for system tables, comma-separated")
dbutils.widgets.text("timezone", "UTC", "8. (optional) time zone for the heatmap, e.g. Australia/Sydney")
dbutils.widgets.dropdown("genie_mode", "create", ["create", "existing", "off"], "9. Ask Genie: create a space, use an existing one, or off")
dbutils.widgets.text("genie_space_id", "", "10. (genie_mode=existing) Genie space id")
dbutils.widgets.text("genie_warehouse_id", "", "11. (genie_mode=create) warehouse for the space; empty = warehouse_id, else auto")
MODE = dbutils.widgets.get("mode")
CHILD = "cluster_right_sizer"
APP_ROOT = os.path.join(SRC, "app")      # what a standalone deploy ships
GENIE_MODE = dbutils.widgets.get("genie_mode")
SCOPES = {"sql"} | ({"genie"} if GENIE_MODE != "off" else set())   # user-authorization scopes this app uses
print("this folder:", SRC, "| mode:", MODE)
assert os.path.isdir(APP_ROOT), f"{APP_ROOT} is missing: was the whole repo imported?"

def write_settings(app_yaml):
    """Put the optional widget values into app.yaml (env values only; the file layout is fixed)."""
    vals = {"CRS_WAREHOUSE_ID": dbutils.widgets.get("warehouse_id").strip(),
            "CRS_WORKSPACE_IDS": ",".join(x for x in re.split(r"[\s,]+", dbutils.widgets.get("workspace_ids")) if x.isdigit()),
            "CRS_TIMEZONE": dbutils.widgets.get("timezone").strip() or "UTC",
            "CRS_GENIE_SPACE_ID": GENIE_SPACE_ID}
    lines, cur = open(app_yaml).read().split("\n"), None
    for i, ln in enumerate(lines):
        m = re.match(r"\s*- name: (\w+)", ln)
        if m:
            cur = m.group(1)
        elif cur in vals and re.match(r"\s*value:", ln):
            lines[i] = re.sub(r"value:.*", "value: " + json.dumps(vals[cur]), ln)
            cur = None
    text = "\n".join(lines)
    open(app_yaml, "w").write(text)
    # The deployment snapshot reads the workspace copy through the API, which can lag a /Workspace (FUSE) write by a
    # few seconds: a deploy right after the write shipped the OLD app.yaml once (measured). Wait until the API sees it.
    import base64
    for _ in range(60):
        try:
            got = base64.b64decode(api.do("GET", "/api/2.0/workspace/export", query={"path": app_yaml.replace("/Workspace", "", 1), "format": "AUTO"})["content"]).decode()
            if got == text:
                break
        except Exception:
            pass
        time.sleep(2)
    else:
        print("  (warning: could not confirm app.yaml through the API; if a setting is missing, Run all again)")
    print("settings:", vals)

# COMMAND ----------

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
api = WorkspaceClient().api_client
DEFAULT_SCOPES = {"iam.access-control:read", "iam.current-user:read"}

# ── Budget / usage policies ──────────────────────────────────────────────────────────────────────────
# A workspace can require every serverless resource (including an app) to name a budget policy; then a
# create without one fails with "Unable to use fallback policies, please manually select a policy".
# Policies are account objects with no workspace list API, so the candidates are the policy ids already
# in use on apps and jobs you can see.
POLICY_ERR = "fallback polic"

def policy_candidates():
    ids = {}
    try:
        tok = None
        while True:
            r = api.do("GET", "/api/2.0/apps", query={"page_size": 100, **({"page_token": tok} if tok else {})})
            for a in r.get("apps", []):
                for k in ("budget_policy_id", "effective_budget_policy_id"):
                    if a.get(k):
                        ids.setdefault(a[k], set()).add(f"app {a['name']}")
            tok = r.get("next_page_token")
            if not tok:
                break
    except Exception as e:
        print("  (could not list apps:", str(e)[:100], ")")
    try:
        r = api.do("GET", "/api/2.1/jobs/list", query={"limit": 100})
        for j in r.get("jobs", []):
            for k in ("budget_policy_id", "effective_budget_policy_id"):
                v = j.get(k) or (j.get("settings") or {}).get(k)
                if v:
                    ids.setdefault(v, set()).add(f"job {(j.get('settings') or {}).get('name', j.get('job_id'))}")
    except Exception as e:
        print("  (could not list jobs:", str(e)[:100], ")")
    return ids

def keep_policy(a, body):
    """A PATCH is a full replacement: carry the app's policy over, or the update falls back and fails."""
    for k, eff in (("budget_policy_id", "effective_budget_policy_id"), ("usage_policy_id", "effective_usage_policy_id")):
        v = a.get(k) or a.get(eff)
        if v:
            body[k] = v
    return body

def create_app(name, description, scopes, policy_id):
    body = {"name": name, "description": description, "user_api_scopes": sorted(scopes)}
    if policy_id:
        body["budget_policy_id"] = policy_id
    try:
        api.do("POST", "/api/2.0/apps", body=body)
        return
    except Exception as e:
        if POLICY_ERR not in str(e) or policy_id:
            raise
        print("This workspace requires a budget policy on new apps. Looking for the ones in use …")
    cands = policy_candidates()
    for pid, users in cands.items():
        print(f"  {pid}   used by: {', '.join(sorted(users))[:120]}")
    if len(cands) == 1:
        pid = next(iter(cands))
        print("Exactly one policy is in use; using it:", pid)
        body["budget_policy_id"] = pid
        api.do("POST", "/api/2.0/apps", body=body)
        return
    raise RuntimeError(
        "Pick a budget policy: paste one of the ids above into the budget_policy_id widget and Run all again, "
        "OR create the app in the UI (Compute → Apps → Create app → Custom, name '" + name + "', choose the policy, "
        "add the user-authorization scopes " + (", ".join(sorted(scopes)) or "(none)") + ") and Run all again.")

def add_scopes(name, a, scopes):
    """Best effort: add missing scopes to an existing app, keeping its policy and resources."""
    have = set(a.get("user_api_scopes") or []) - DEFAULT_SCOPES
    if scopes <= have:
        return True
    body = keep_policy(a, {"name": name, "description": a.get("description", ""),
                           "user_api_scopes": sorted(have | scopes), "resources": a.get("resources") or []})
    try:
        api.do("PATCH", f"/api/2.0/apps/{name}", body=body)
        print(f"{name}: scopes now {sorted(have | scopes)}")
        return True
    except Exception as e:
        print(f"Could not add the scopes {sorted(scopes - have)} to {name} ({str(e)[:140]}).\n"
              f"Add them in the UI: Compute → Apps → {name} → Edit → User authorization → + Add scope. "
              "Deploying anyway; the app works once the scope is there.")
        return False


def get_app(name):
    try:
        return api.do("GET", f"/api/2.0/apps/{name}")
    except NotFound:
        return None

def wait_compute(name, states, minutes=15):
    st = None
    for _ in range(minutes * 12):
        a = get_app(name)
        st = (a.get("compute_status") or {}).get("state")
        if st in states:
            return a
        time.sleep(5)
    raise TimeoutError(f"compute of {name} did not reach {states}; last {st}")

def deploy(name, source):
    a = get_app(name)
    if (a.get("compute_status") or {}).get("state") != "ACTIVE":
        print("starting compute …")
        try:
            api.do("POST", f"/api/2.0/apps/{name}/start", body={})
        except Exception as e:
            print("  start:", str(e)[:120])
        wait_compute(name, {"ACTIVE"})
    # Starting a stopped app redeploys its last build first; a new deployment is refused until that one finishes.
    def _busy(a):
        return any(((a.get(k) or {}).get("status") or {}).get("state") in ("IN_PROGRESS", "PENDING")
                   for k in ("pending_deployment", "active_deployment"))
    for _ in range(120):
        if not _busy(get_app(name) or {}):
            break
        time.sleep(10)
    print(f"deploying {source} → {name} …")
    for attempt in range(30):
        try:
            dep = api.do("POST", f"/api/2.0/apps/{name}/deployments", body={"source_code_path": source, "mode": "SNAPSHOT"})
            break
        except Exception as e:
            if "deployment in progress" not in str(e) or attempt == 29:
                raise
            print("  a previous deployment is still running (starting a stopped app redeploys it first); waiting …")
            time.sleep(20)
    t0 = time.time()
    while True:
        d = api.do("GET", f"/api/2.0/apps/{name}/deployments/{dep['deployment_id']}")
        st = (d.get("status") or {}).get("state")
        if st not in ("IN_PROGRESS", "PENDING", None):
            break
        if time.time() - t0 > 25 * 60:
            raise TimeoutError("deployment still running after 25 min: check Compute → Apps → Logs")
        time.sleep(10)
    print(f"deployment: {st} — {(d.get('status') or {}).get('message')} ({time.time() - t0:.0f}s)")
    assert st == "SUCCEEDED", "deployment failed: open Compute → Apps → this app → Logs"
    return get_app(name)

def try_get(url):
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {ctx.apiToken().get()}"})
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:
        return None, str(e)[:160]

# COMMAND ----------

# ── Ask Genie (optional) ────────────────────────────────────────────────────────────────────────────
# create  : a Genie space over the right-sizing system tables (reused if one with the same title exists), CAN RUN for
#           the 'users' group. Genie needs partner-powered AI (no FMAPI). Answers run as each viewer, with their grants.
# existing: validate genie_space_id.   off: no space; the app hides the panel.
GENIE_TITLE = "Cluster Right-Sizer"
GENIE_TABLES = sorted(["system.access.audit", "system.billing.list_prices", "system.billing.usage",
                       "system.compute.clusters", "system.compute.node_timeline", "system.compute.node_types"])

def _ws_ids():
    ids = [str(WorkspaceClient().get_workspace_id())]
    ids += [x for x in re.split(r"[\s,]+", dbutils.widgets.get("workspace_ids")) if x.isdigit() and x not in ids]
    return ids

def _genie_warehouse():
    w = dbutils.widgets.get("genie_warehouse_id").strip() or dbutils.widgets.get("warehouse_id").strip()
    if w:
        return w
    whs = api.do("GET", "/api/2.0/sql/warehouses").get("warehouses") or []
    whs.sort(key=lambda x: (x.get("state") != "RUNNING", not x.get("enable_serverless_compute"), x.get("name", "")))
    return whs[0]["id"] if whs else None

def _genie_payload(ws_ids):
    ws = ", ".join(f"'{i}'" for i in ws_ids)
    instr = (f"You answer questions about right-sizing classic all-purpose Databricks clusters. Always filter every table on workspace_id IN ({ws}) "
             "because system tables cover the whole account in this region. All-purpose clusters are rows of system.compute.clusters with cluster_source IN ('UI','API'); "
             "use the latest row per cluster_id (max change_time) for the current configuration. CPU utilisation is cpu_user_percent + cpu_system_percent in "
             "system.compute.node_timeline (one row per node per minute); the driver column is true for the driver node and false for workers; report driver and "
             "workers separately unless asked otherwise, using avg and percentile_approx(..., 0.95). Memory is mem_used_percent; I/O wait is cpu_wait_percent; swap is mem_swap_percent. "
             "Cost: system.billing.usage rows with billing_origin_product = 'ALL_PURPOSE', usage_metadata.cluster_id, usage_quantity in DBUs, priced by joining "
             "system.billing.list_prices on sku_name with usage_end_time between price_start_time and price_end_time (pricing.default). Who uses a cluster: "
             "system.access.audit rows with service_name = 'notebook' and action_name IN ('submitCommand','attachNotebook') where request_params['clusterId'] is the cluster, "
             "user_identity.email is the user; command run time is request_params['executionTime'] in seconds on action_name = 'runCommand' joined on request_params['commandId']; "
             "jobs on a cluster are service_name = 'jobs' and action_name = 'runCommand'. node_timeline keeps 90 days. Prefer cluster_name in answers and show cluster_id too.")
    qs = ["Which all-purpose clusters had the lowest p95 worker CPU in the last 30 days?",
          "Which all-purpose clusters cost the most DBUs in the last 30 days?",
          "Who are the top users of each all-purpose cluster by notebook commands this month?",
          "Which clusters have a driver p95 CPU under 20% over the last 14 days?",
          "Show average worker CPU by hour of day across all all-purpose clusters this week."]
    return {"version": 2, "data_sources": {"tables": [{"identifier": t} for t in GENIE_TABLES]},
            "instructions": {"text_instructions": [{"content": [" ".join(instr.split())]}]},
            "config": {"sample_questions": [{"id": __import__("hashlib").md5(q.encode()).hexdigest(), "question": [q]} for q in qs]}}

def setup_genie():
    if GENIE_MODE == "off":
        print("Ask Genie: off (the app hides the panel)")
        return ""
    if GENIE_MODE == "existing":
        sid = dbutils.widgets.get("genie_space_id").strip()
        try:
            s = api.do("GET", f"/api/2.0/genie/spaces/{sid}")
            print(f"Ask Genie: using existing space {s.get('title')!r} ({sid})")
            return sid
        except Exception as e:
            print(f"Ask Genie: genie_space_id {sid!r} is not usable ({str(e)[:160]}). Falling back to off.")
            return ""
    try:
        wh = _genie_warehouse()
        assert wh, "no SQL warehouse visible to you"
        body = {"warehouse_id": wh, "title": GENIE_TITLE, "description": "Right-sizing classic all-purpose clusters (created by the Cluster Right-Sizer DEPLOY)",
                "serialized_space": json.dumps(_genie_payload(_ws_ids()))}
        found, tok = None, None
        for _ in range(20):
            r = api.do("GET", "/api/2.0/genie/spaces", query={"page_size": 100, **({"page_token": tok} if tok else {})})
            found = next((x for x in r.get("spaces") or [] if x.get("title") == GENIE_TITLE), None)
            tok = r.get("next_page_token")
            if found or not tok:
                break
        if found:
            sid = found["space_id"]
            api.do("PATCH", f"/api/2.0/genie/spaces/{sid}", body=body)
            print(f"Ask Genie: updated the existing space {GENIE_TITLE!r} ({sid})")
        else:
            sid = api.do("POST", "/api/2.0/genie/spaces", body=body)["space_id"]
            print(f"Ask Genie: created space {GENIE_TITLE!r} ({sid}) on warehouse {wh}")
        try:
            api.do("PATCH", f"/api/2.0/permissions/genie/{sid}", body={"access_control_list": [{"group_name": "users", "permission_level": "CAN_RUN"}]})
            print("  CAN RUN granted to the 'users' group (each viewer still needs SELECT on the system tables and CAN USE on the warehouse)")
        except Exception as e:
            print(f"  could not grant CAN RUN to 'users' ({str(e)[:120]}); share the space in the UI")
        return sid
    except Exception as e:
        print(f"Ask Genie: could not create the space ({str(e)[:200]}).\n"
              "You may lack the right to create Genie spaces or to read the system tables. Falling back to off; "
              "re-run with genie_mode=existing and a space someone else created, or ask an admin.")
        return ""

GENIE_SPACE_ID = setup_genie()
if not GENIE_SPACE_ID:
    SCOPES.discard("genie")

# COMMAND ----------

if MODE == "child":
    CAT = dbutils.widgets.get("catalog_folder").strip().rstrip("/")
    CAT_APP = dbutils.widgets.get("catalog_app_name").strip()
    if CAT_APP == "app-ception" and get_app("app-ception") is None and get_app("app-catalog") is not None:
        CAT_APP = "app-catalog"     # an install from before the rename
        print("no app 'app-ception'; using the existing 'app-catalog' app")
    assert CAT and os.path.isfile(f"{CAT}/catalog/server.mjs"), \
        f"catalog_folder must be the App-ception folder (with catalog/server.mjs); got {CAT!r}. Import app-ception-main.zip first."
    dest = f"{CAT}/child_apps/{CHILD}"
    if os.path.exists(dest):   # keep the previous copy, OUTSIDE child_apps so it is not discovered as an app
        bak = f"{CAT}/.bak-child-{CHILD}-{time.strftime('%Y%m%d-%H%M%S')}"
        os.rename(dest, bak)
        for _ in range(30):     # /Workspace FUSE: a renamed path lingers for a few seconds
            if not os.path.exists(dest):
                break
            time.sleep(1)
        print("previous copy kept at", bak)
    # Copy only what the child needs. Workspace NOTEBOOK objects (DEPLOY, setup/demo_load) cannot be copied through
    # /Workspace FUSE (Errno 95 "Operation not supported"), and the catalog does not need them.
    NEEDED = ["app", "catalog.yaml", "databricks.yml", "README.md", "LICENSE"]
    os.makedirs(dest)
    skipped = []
    def _copy(src, dst):
        try:
            shutil.copy2(src, dst)
        except OSError as e:
            if e.errno not in (95, getattr(__import__("errno"), "EOPNOTSUPP", 95), getattr(__import__("errno"), "ENOTSUP", 95)):
                raise
            skipped.append(src)          # a notebook object: not part of the app
    for name in NEEDED:
        src_p = os.path.join(SRC, name)
        if os.path.isdir(src_p):
            shutil.copytree(src_p, os.path.join(dest, name), copy_function=_copy)
        elif os.path.isfile(src_p):
            _copy(src_p, os.path.join(dest, name))
    if skipped:
        print("skipped notebook objects (not part of the app):", [os.path.relpath(x, SRC) for x in skipped])
    write_settings(f"{dest}/app/app.yaml")
    n_src = sum(len(fs) for r, _, fs in os.walk(os.path.join(SRC, "app"))) + sum(os.path.isfile(os.path.join(SRC, n)) for n in NEEDED[1:]) - len(skipped)
    n_dst = sum(len(fs) for _, _, fs in os.walk(dest))
    assert n_dst == n_src, f"copied {n_dst} of {n_src} files into {dest}"
    print(f"copied {n_dst} files into {dest}")
    # Child mode never creates or replaces an app: the child runs INSIDE the existing catalog app.
    a = get_app(CAT_APP)
    assert a is not None, f"no app {CAT_APP!r}: deploy App-ception first (its DEPLOY notebook), or set catalog_app_name"
    if SCOPES - (set(a.get("user_api_scopes") or []) - DEFAULT_SCOPES):
        add_scopes(CAT_APP, wait_compute(CAT_APP, {"ACTIVE", "STOPPED", "ERROR"}), SCOPES)
    a = deploy(CAT_APP, CAT)
    url = a.get("url")
    print("\nCATALOG URL:", url)
    print("THIS APP   :", f"{url}/apps/{CHILD}/")
    st, body = try_get(f"{url}/_catalog/api/health")
    h = None
    if st == 200 and body.lstrip().startswith("{"):   # the Apps proxy may answer the notebook's token with a sign-in page
        try:
            h = json.loads(body)
        except ValueError:
            h = None
    if h is not None:
        mine = [c for c in h.get("children", []) if c["name"] == CHILD]
        print("health: offline =", h.get("offline"), "|", CHILD, "→", mine[0].get("prepare") if mine else "NOT FOUND", mine[0].get("problems") if mine else "")
    else:
        print("(open", f"{url}/_catalog/api/health", "in the browser: this app should show \"prepare\": \"ready\")")
else:
    NAME = dbutils.widgets.get("standalone_app_name").strip()
    assert NAME and NAME == NAME.lower(), "app name: lowercase letters, digits and '-'"
    a = get_app(NAME)
    if a is None:
        print(f"creating app {NAME} …")
        create_app(NAME, "Cluster Right-Sizer", SCOPES, dbutils.widgets.get("budget_policy_id").strip())
    a = wait_compute(NAME, {"ACTIVE", "STOPPED", "ERROR"})
    add_scopes(NAME, a, SCOPES)
    write_settings(os.path.join(APP_ROOT, "app.yaml"))
    a = deploy(NAME, APP_ROOT)
    url = a.get("url")
    print("\nAPP URL:", url)
    st, body = try_get(f"{url}/api/health")
    ok = st == 200 and not (body or "").lstrip().lower().startswith("<!doctype")
    print("health:", (body or "")[:200] if ok else "(open the URL in the browser to check it)")
