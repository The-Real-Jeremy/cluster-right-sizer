#!/bin/bash
# CLI deploy for development (the browser-only path is DEPLOY.py). Stages app/ with per-target settings,
# uploads it, deploys, and checks the file count. Usage:
#   scripts/deploy_cli.sh <profile> <app-name> [warehouse_id] [extra_workspace_ids] [timezone]
set -euo pipefail
P=$1; APP=$2; WH=${3:-}; WS=${4:-}; TZ=${5:-UTC}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
ME=$(databricks current-user me -p "$P" -o json | python3 -c 'import json,sys;print(json.load(sys.stdin)["userName"])')
DEST="/Workspace/Users/$ME/apps/$APP"
STAGE=$(mktemp -d)/app
rsync -a --exclude __pycache__ "$ROOT/app/" "$STAGE/"
BUILD="$(git -C "$ROOT" rev-parse --short HEAD)$(git -C "$ROOT" diff --quiet || echo -dirty)"
python3 - "$STAGE/app.yaml" "$WH" "$WS" "$TZ" "$BUILD" <<'PY'
import sys, re, json
p, wh, ws, tz, build = sys.argv[1:]
vals = {"CRS_WAREHOUSE_ID": wh, "CRS_WORKSPACE_IDS": ws, "CRS_TIMEZONE": tz, "BUILD_ID": build}
lines, cur = open(p).read().split("\n"), None
for i, ln in enumerate(lines):
    m = re.match(r"\s*- name: (\w+)", ln)
    if m: cur = m.group(1)
    elif cur in vals and re.match(r"\s*value:", ln):
        lines[i] = re.sub(r"value:.*", "value: " + json.dumps(vals[cur]), ln); cur = None
open(p, "w").write("\n".join(lines))
PY
# extra env for test deployments only, e.g. CRS_EXTRA_ENV="CRS_SP_DIAGNOSTICS=1"
for kv in ${CRS_EXTRA_ENV:-}; do printf '  - name: %s\n    value: "%s"\n' "${kv%%=*}" "${kv#*=}" >> "$STAGE/app.yaml"; done
N_LOCAL=$(find "$STAGE" -type f | wc -l | tr -d ' ')
databricks workspace mkdirs "$DEST" -p "$P"
databricks workspace import-dir "$STAGE" "$DEST" --overwrite -p "$P" > /dev/null
N_REMOTE=$(databricks workspace list "$DEST" -p "$P" -o json | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')
python3 - "$P" "$DEST" "$N_LOCAL" <<'PY'
import json, subprocess, sys
p, dest, n_local = sys.argv[1], sys.argv[2], int(sys.argv[3])
def walk(path):
    out = json.loads(subprocess.check_output(["databricks", "workspace", "list", path, "-p", p, "-o", "json"]))
    n = 0
    for o in out:
        n += walk(o["path"]) if o["object_type"] == "DIRECTORY" else 1
    return n
n = walk(dest)
print(f"files: local {n_local}, workspace {n}")
assert n == n_local, "file count mismatch: upload incomplete"
PY
echo "deploying $DEST -> $APP (build $BUILD)"
databricks apps deploy "$APP" --source-code-path "$DEST" -p "$P" -o json | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("status"))'
