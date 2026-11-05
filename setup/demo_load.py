# Databricks notebook source
# Synthetic load for the Cluster Right-Sizer demo clusters. Intensity comes from the "profile" widget.
import time, random, json, urllib.request
# Self-limiting: after DEADLINE (UTC epoch) the job pauses its own schedule and exits, so it cannot run forever.
DEADLINE = 1793890800  # 2026-11-06 02:00 AEDT
if time.time() > DEADLINE:
    ctx = json.loads(dbutils.notebook.entry_point.getDbutils().notebook().getContext().safeToJson())["attributes"]
    job_id = ctx.get("jobId")
    if job_id:
        host = ctx["api_url"]; tok = ctx["api_token"]
        body = json.dumps({"job_id": int(job_id), "new_settings": {"schedule": {"quartz_cron_expression": "0 0/20 * * * ?", "timezone_id": "UTC", "pause_status": "PAUSED"}}}).encode()
        urllib.request.urlopen(urllib.request.Request(host + "/api/2.1/jobs/update", data=body, method="POST", headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"}), timeout=30)
    dbutils.notebook.exit("past deadline: schedule paused")
dbutils.widgets.text("profile", "light")
profile = dbutils.widgets.get("profile")
minutes = {"light": 4, "busy": 9, "idle": 0.5}.get(profile, 3)
parts = {"light": 8, "busy": 64, "idle": 2}.get(profile, 8)
t_end = time.time() + minutes * 60
n = 0
while time.time() < t_end:
    df = spark.range(0, 40_000_000 if profile == "busy" else 4_000_000, numPartitions=parts)
    df.selectExpr("id % 1000 as k", "sha2(cast(id as string), 256) as h").groupBy("k").count().collect()
    n += 1
    if profile == "light":
        time.sleep(random.uniform(10, 30))
print(profile, "iterations:", n)

# COMMAND ----------

spark.range(0, 1000).selectExpr("sum(id)").collect()
