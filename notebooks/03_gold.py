# Databricks notebook source
import os
import sys

# The tested transforms live in src/. Notebooks call them rather than restating the
# logic: a copy would drift from the version the test suite covers, and the drift
# would not announce itself.
#
# The repo root is found by walking up until `src/` appears, so the same notebook works
# whether it was uploaded by hand (src as a sibling) or synced by a bundle (src beside
# notebooks/, one level further up).
_p = "/Workspace" + dbutils.notebook.entry_point.getDbutils().notebook() \
    .getContext().notebookPath().get()
for _ in range(5):
    _p = os.path.dirname(_p)
    if os.path.isdir(os.path.join(_p, "src")):
        sys.path.insert(0, _p)
        break
else:
    raise RuntimeError("could not locate src/ from the notebook path")

# A parameter, not a constant: the bundle target supplies it, so dev and prod differ in
# configuration rather than in code.
dbutils.widgets.text("catalog", "energy")
CATALOG = dbutils.widgets.get("catalog")
spark.sql(f"USE CATALOG {CATALOG}")
print(f"catalog={CATALOG}  root={sys.path[0]}")

# COMMAND ----------

from datetime import date                                    # noqa: E402
from src.gold.daily_summary import daily_summary, peak_history  # noqa: E402
from src.silver.calendar_utils import calendar_frame           # noqa: E402

silver = spark.table(f"{CATALOG}.silver.electricity_hourly")
# Starts a day early: the first UTC hour belongs to the previous local operating day.
cal = calendar_frame(spark, date(2018, 12, 31), date(2027, 12, 31))

gold = daily_summary(silver, cal)
gold.write.mode("overwrite").option("overwriteSchema", "true") \
    .saveAsTable(f"{CATALOG}.gold.daily_summary")
peak_history(gold).write.mode("overwrite").option("overwriteSchema", "true") \
    .saveAsTable(f"{CATALOG}.gold.peak_history")

# COMMAND ----------

import json  # noqa: E402
g = spark.table(f"{CATALOG}.gold.daily_summary")
out = {"daily_summary": g.count(),
       "complete_days": g.filter("is_complete").count(),
       "peak_history": spark.table(f"{CATALOG}.gold.peak_history").count()}
print(json.dumps(out, indent=2))
dbutils.notebook.exit(json.dumps(out))
