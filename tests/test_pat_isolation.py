"""Proves a sibling App-ception child cannot unseal this app's PAT cookie, using real separate processes with the
environment App-ception gives its children (same DATABRICKS_CLIENT_SECRET and DATABRICKS_APP_NAME for all of them).
Also keeps the control that shows WHY: with a derived key (standalone mode) a second process with the same secret can."""
import json, os, subprocess, sys

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
SHARED = {"DATABRICKS_CLIENT_SECRET": "same-secret-every-child-inherits", "DATABRICKS_APP_NAME": "app-ception",
          "DATABRICKS_HOST": "https://example.invalid"}
CHILD = dict(SHARED, APPCEPTION_CHILD="cluster_right_sizer", APPCEPTION_BASE_PATH="/apps/cluster_right_sizer", APPCEPTION_MOUNT="prefix")
SIBLING = dict(SHARED, APPCEPTION_CHILD="some_other_child", APPCEPTION_BASE_PATH="/apps/some_other_child", APPCEPTION_MOUNT="root")
STANDALONE = dict(SHARED, DATABRICKS_APP_NAME="cluster-right-sizer")

SEAL = "import json,sys; sys.path.insert(0,'.'); from lib import pat; v=pat.seal('dapiFAKE0000000000000000','a@b.c'); print(json.dumps({'v':v,'same_process':bool(pat.unseal(v)),'hdr':pat.cookie_header(v,60),'mode':pat.key_mode()}))"
UNSEAL = "import json,sys; sys.path.insert(0,'.'); from lib import pat; print(json.dumps({'ok':bool(pat.unseal(sys.argv[1])),'mode':pat.key_mode()}))"


def run(code, env, *args):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("APPCEPTION_", "CATALOG_", "DATABRICKS_", "CRS_"))}
    e.update(env)
    return json.loads(subprocess.check_output([sys.executable, "-W", "ignore", "-c", code, *args], cwd=APP, env=e))


a = run(SEAL, CHILD)
path = [x.strip() for x in a["hdr"].split(";") if x.strip().startswith("Path=")][0]
checks = [
    ("child: key mode is memory", a["mode"] == "memory"),
    ("child: same process can unseal its own cookie", a["same_process"]),
    ("child: cookie Path is its own prefix", path == "Path=/apps/cluster_right_sizer/"),
    ("child: a SIBLING child (same secret + app name) cannot unseal it", run(UNSEAL, SIBLING, a["v"])["ok"] is False),
    ("child: the same child after a restart cannot unseal it (re-enter by design)", run(UNSEAL, CHILD, a["v"])["ok"] is False),
]
s = run(SEAL, STANDALONE)
checks += [
    ("standalone: key mode is derived, cookie Path=/", s["mode"] == "derived" and "Path=/;" in s["hdr"]),
    ("control: with a DERIVED key another process with the same secret CAN unseal (the risk being closed)",
     run(UNSEAL, STANDALONE, s["v"])["ok"] is True),
    ("standalone + CRS_PAT_KEY=memory: another process cannot unseal", run(UNSEAL, dict(STANDALONE, CRS_PAT_KEY="memory"),
     run(SEAL, dict(STANDALONE, CRS_PAT_KEY="memory"))["v"])["ok"] is False),
]
for name, ok in checks:
    print(("PASS " if ok else "FAIL ") + name)
sys.exit(0 if all(ok for _, ok in checks) else 1)
