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

from datetime import date, timedelta                      # noqa: E402
from src.features.build import build_features             # noqa: E402

WEATHER_FLOOR = date(2021, 3, 24)   # measured archive floor, D-21

silver = spark.table(f"{CATALOG}.silver.electricity_hourly")
weather = spark.table(f"{CATALOG}.silver.weather_forecast_hourly")

# The last forecast date whose whole target day is covered by weather.
wmax = weather.selectExpr("max(valid_timestamp_utc) AS m").first()["m"].date()
end = wmax - timedelta(days=1)

feats = build_features(silver, weather, spark, WEATHER_FLOOR, end,
                       weather_leads=(1, 2))
feats.write.mode("overwrite").option("overwriteSchema", "true") \
     .saveAsTable(f"{CATALOG}.gold.demand_features")

# COMMAND ----------

import json  # noqa: E402
f = spark.table(f"{CATALOG}.gold.demand_features")
out = {"rows": f.count(), "columns": len(f.columns),
       "labelled": f.filter("label_demand_mwh IS NOT NULL").count(),
       "first": str(f.selectExpr("min(forecast_date) AS d").first()["d"]),
       "last": str(f.selectExpr("max(forecast_date) AS d").first()["d"])}
print(json.dumps(out, indent=2))
dbutils.notebook.exit(json.dumps(out))
