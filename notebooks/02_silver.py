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

from src.silver.electricity import build_silver          # noqa: E402
from src.silver.weather import build_weather_silver      # noqa: E402

# Full rebuild from Bronze, not an incremental merge. Silver is a deterministic
# function of Bronze, Bronze is append-only, and the whole transform runs in seconds at
# this size — so a rebuild is both cheaper to reason about and immune to a merge
# predicate quietly going wrong. Revert to incremental only when the rebuild stops
# being cheap.
silver, quarantine = build_silver(spark.table(f"{CATALOG}.bronze.eia_region_data"), spark)
silver.write.mode("overwrite").option("overwriteSchema", "true") \
      .saveAsTable(f"{CATALOG}.silver.electricity_hourly")
quarantine.write.mode("overwrite").option("overwriteSchema", "true") \
      .saveAsTable(f"{CATALOG}.silver.electricity_quarantine")

weather, wq = build_weather_silver(spark.table(f"{CATALOG}.bronze.weather_forecast"))
weather.write.mode("overwrite").option("overwriteSchema", "true") \
      .saveAsTable(f"{CATALOG}.silver.weather_forecast_hourly")

# COMMAND ----------

import json  # noqa: E402
out = {t: spark.table(f"{CATALOG}.silver.{t}").count() for t in
       ("electricity_hourly", "electricity_quarantine", "weather_forecast_hourly")}
print(json.dumps(out, indent=2))
dbutils.notebook.exit(json.dumps(out))
