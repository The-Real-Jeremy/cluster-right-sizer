"""Smoke test on the synthetic data: every cluster evaluates, KPIs are consistent, the detail endpoint fills."""
import os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from lib import service
o = service.overview({}, 30, True)
print(o["kpis"])
for c in o["clusters"]:
    print(f'{c["name"]:22} score={c["score"]} peak={c["workers_peak"]}/{c["max_workers"]} saving={c["saving"]["fraction"]}',
          [r["title"] for r in c["recommendations"]], [n["title"] for n in c["notes"]])
    assert c["workers_peak"] is None or c["max_workers"] is None or c["workers_peak"] <= c["max_workers"], c["name"]
d = service.cluster_detail({}, o["clusters"][0]["key"], 30, True)
sizes = {k: len(v.get("rows", [])) for k, v in d["detail"].items() if isinstance(v, dict)}
print(sizes)
assert all(sizes.values()), sizes
print("OK")
