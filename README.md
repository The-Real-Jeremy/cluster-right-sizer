# Cluster Right-Sizer — a Databricks App for classic all-purpose clusters

Shows who uses each all-purpose cluster, how hard its driver and workers actually work (together and per
node), how long commands run, and what it could be resized to. Each viewer sees only what **their own**
permissions allow; panels they cannot read say exactly which grant is missing.

> **Prototype edition, for isolated dev/demo use.** The cluster list and events are read with each viewer's own
> personal access token, entered once in the app, because Databricks Apps has no user-authorization scope for the
> Clusters API yet. The standard edition will not need a token. The system-table panels and Ask Genie work without it.

Standard-library Python, no `requirements.txt`, no CDN or web font from the internet, no model serving or FMAPI
(the recommendations are rules; the optional Ask Genie panel uses Genie), no Lakebase, no writes. It installs and runs in an air-gapped workspace.

## Install (browser only: no terminal, no CLI, works air-gapped)

Both ways use the `DEPLOY` notebook.

### A) Install on its own (a standalone Databricks App)
1. On this GitHub page: **Code → Download ZIP** (`cluster-right-sizer-main.zip`). No need to unzip it.
2. In Databricks: **Workspace** → a folder (e.g. your home) → **⋮ → Import** → drop the `.zip` → **Import**.
   Databricks unzips it into `cluster-right-sizer-main/`.
3. Open **`cluster-right-sizer-main/DEPLOY`**, attach **Serverless**, set **mode = `standalone`**, optionally set
   `warehouse_id`, `workspace_ids`, `timezone` and the **Ask Genie** widgets (below), then **Run all**.
4. The last cell prints the **app URL**.

### B) Install as a child of App-ception
1. Install [App-ception](https://github.com/The-Real-Jeremy/app-ception) first, so `app-ception-main/` is in your workspace.
2. On this GitHub page: **Code → Download ZIP** (`cluster-right-sizer-main.zip`).
3. In Databricks: open **the same folder that holds `app-ception-main/`** → **⋮ → Import** → drop the `.zip`.
4. Open **`cluster-right-sizer-main/DEPLOY`**, attach **Serverless**, leave **mode = `child`** (`catalog_folder` is filled
   in for you; `catalog_app_name` defaults to `app-ception`), optionally set the **Ask Genie** widgets, then **Run all**.
   It copies the app into the catalog's `child_apps/cluster_right_sizer/`, adds the `sql` (and `genie`) scopes to the
   catalog app, and redeploys the catalog. The last cell prints `<catalog URL>/apps/cluster_right_sizer/`.

**Ask Genie (optional), widget `genie_mode`:**
- `create` (default): creates a Genie space "Cluster Right-Sizer" over the right-sizing system tables (reused and updated
  if one with that title exists), with right-sizing instructions and sample questions, grants **CAN RUN** to the
  `users` group, adds the `genie` user scope to the app (keeping its budget policy) and writes the space id into the app.
  `genie_warehouse_id` picks the space's warehouse (default: `warehouse_id`, else a running serverless one).
- `existing`: uses `genie_space_id` (validated first).
- `off`: no space, no `genie` scope; the app hides the panel.
- If the installer cannot create the space (no right to create spaces or read the system tables), DEPLOY says so and
  falls back to `off`. Genie needs partner-powered AI on the workspace, not Foundation Model APIs. A newly added
  `genie` scope can take ~10-15 minutes to reach viewers; each viewer consents once.

**Budget policies:** child mode never creates an app. For standalone mode in a workspace that requires a budget policy,
set `budget_policy_id`, or create the app in the UI first (pick the policy, add the `sql` user-authorization scope),
then Run all. If exactly one policy is in use it is picked automatically.

**To update** either way: first **rename or delete the existing `cluster-right-sizer-main/` folder**. Importing a ZIP onto
a folder that already exists is refused ("already exists"), and nothing is overwritten. Then import the new ZIP and
run `DEPLOY` again. In child mode the previous copy inside the catalog is kept as `.bak-child-…`.

**No grant step for the app.** The app's own service principal needs nothing: clusters are read with each viewer's
own token and system tables with each viewer's own grants.

## What each viewer needs

| Panel | Read with | The viewer needs |
|---|---|---|
| Clusters, configuration, workers over time, restarts, idle shutdowns | the viewer's **own personal access token**, entered once in the app | a PAT (Settings → Developer → Access tokens); **CAN ATTACH TO** (or any permission) on each cluster to see it |
| Per-node CPU / memory, driver vs workers, heatmap, utilisation advice | the viewer's sign-in (on-behalf-of, `sql` scope) on a SQL warehouse | **CAN USE** on the warehouse; `SELECT` on `system.compute.node_timeline` (+ `clusters`, `node_types`) |
| Cost per cluster | same | `SELECT` on `system.billing.usage` (+ `list_prices` for cost) |
| Who uses each cluster, command run times | same | `SELECT` on `system.access.audit`; run times need **Verbose Audit Logs** (workspace admin → Advanced) |
| Ask Genie (optional) | the viewer's sign-in (`genie` scope) | **CAN RUN** on the space (DEPLOY grants `users`), plus the same warehouse and system-table grants; answers run as the viewer |

Grants (a metastore or account admin):

```sql
GRANT USE CATALOG ON CATALOG system TO `<user or group>`;
GRANT USE SCHEMA, SELECT ON SCHEMA system.compute TO `<user or group>`;
GRANT USE SCHEMA, SELECT ON SCHEMA system.billing TO `<user or group>`;
GRANT USE SCHEMA, SELECT ON SCHEMA system.access TO `<user or group>`;
```

The app's **Your access & what is missing** dialog lists, for the person looking, each missing grant with the exact
statement, the warehouse permission, and every cluster in the workspace they cannot see with the Permissions API call
that would add them.

### Why a personal access token
Databricks Apps has no user-authorization scope for the Clusters API (`clusters`, `compute.clusters` and similar are
rejected; only scopes such as `sql` are accepted), so the forwarded sign-in token cannot list clusters. The PAT:
- is checked against the signed-in user (someone else's token is refused);
- is sealed (HMAC-SHA256 encrypt-then-MAC, key derived from the app's own client secret) into an
  `HttpOnly; Secure; SameSite=Strict` cookie on the app's path (inside App-ception the path is the child's prefix),
  max 30 days; page scripts cannot read it and nothing is stored or logged server-side;
- is used only for clusters/list, clusters/get, clusters/events, list-node-types and SCIM Me;
- can be forgotten from the app; an expired or revoked token clears the cookie and asks again.

## Settings (`app/app.yaml`, all optional)

| Variable | Default | Meaning |
|---|---|---|
| `CRS_WAREHOUSE_ID` | auto | SQL warehouse; empty picks one the viewer can see (running, serverless first) |
| `CRS_WORKSPACE_IDS` | this workspace | extra workspace ids for the system-table panels (system tables are regional and account-wide; every query filters on workspace) |
| `CRS_LOOKBACK_DAYS` | 30 | default window (node_timeline keeps 90 days, the event log 60) |
| `CRS_TIMEZONE` | UTC | hour-of-day for the heatmap |
| `CRS_GENIE_SPACE_ID` | (DEPLOY) | Genie space for the Ask Genie panel; empty hides it |
| `CRS_DEMO` | allowed | `off` / `allowed` / `default` — the labelled **Synthetic demo** switch |
| `CRS_T_*` | 40/60/20/50/60/30 | rule thresholds (worker CPU/mem p95, driver CPU/mem p95, max and target auto-termination) |
| `CRS_SP_DIAGNOSTICS` | off | test only: `?as=app` views the app as its own service principal; `&simulate=no_system_tables` |

## The rules (starting points, not Databricks-official limits)
- Workers p95 CPU < 40% and p95 memory < 60%, no I/O-bound or swap signal → a node type with half the cores in the same
  family that still has 30% memory headroom; otherwise a lower worker ceiling.
- Driver p95 CPU < 20% and memory < 50% → a smaller driver.
- Peak workers well below the configured max over the window → lower max; at min 95% of the time → lower min.
- Auto-termination off → turn it on; above 60 min → 30 min, priced from the idle shutdowns in the event log.
- About 24 h on every active day → let it stop or move scheduled work to job compute; fixed size → autoscaling.
- Busy, I/O-bound or swapping clusters are marked "keep". Savings per cluster are combined, never added.

## Known limits
- Per-user CPU or memory on a shared cluster is not measured anywhere; the audit log says who ran what and for how long.
- `system.query.history` does not cover classic clusters; run times here are per notebook cell (`runCommand`).
- The Clusters API answers only for the workspace the app runs in; rows for other workspaces come from system tables.
- On a very large account the audit log scan is slow; the users panel loads in the background and is cached 10 minutes.

## Development
```bash
CRS_DEV_TOKEN=$(databricks auth token -p <profile> | jq -r .access_token) CRS_DEV_TOKEN_AS_PAT=1 \
DATABRICKS_HOST=https://<workspace> CRS_WORKSPACE_ID=<id> PORT=8731 python3 app/app.py
python3 tests/smoke_demo.py                    # rules + demo data
scripts/deploy_cli.sh <profile> <app> [warehouse] [workspace_ids] [tz]   # CLI deploy with a file-count check
```
