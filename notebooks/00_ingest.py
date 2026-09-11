# Databricks notebook source
# MAGIC %md
# MAGIC # Ingest — EIA-930 and archived weather forecasts
# MAGIC
# MAGIC The first task in the DAG, and until now the missing one: everything downstream
# MAGIC read a landing zone that a laptop had to refresh by hand, so the schedule would
# MAGIC have reprocessed the same files forever.
# MAGIC
# MAGIC Writes through a **UC external volume** rather than to `abfss://` directly. The
# MAGIC extractors use `pathlib` and `pyarrow`, which cannot address cloud storage; a
# MAGIC volume gives those APIs a real path (`/Volumes/...`) over the same bytes Auto
# MAGIC Loader then reads via `abfss://`. The alternative — writing to driver-local disk
# MAGIC and copying afterwards — needs a second step that can fail independently of the
# MAGIC one that fetched the data.
# MAGIC
# MAGIC The API key comes from a secret scope. `dbutils.secrets.get` redacts the value in
# MAGIC cell output and in logs, which a widget or an environment variable would not.

# COMMAND ----------

import os
import sys

_p = "/Workspace" + dbutils.notebook.entry_point.getDbutils().notebook() \
    .getContext().notebookPath().get()
for _ in range(5):
    _p = os.path.dirname(_p)
    if os.path.isdir(os.path.join(_p, "src")):
        sys.path.insert(0, _p)
        break
else:
    raise RuntimeError("could not locate src/ from the notebook path")

dbutils.widgets.text("catalog", "energy")
dbutils.widgets.dropdown("mode", "incremental", ["incremental", "backfill"])
dbutils.widgets.text("lookback_days", "14")
dbutils.widgets.text("secret_scope", "energy")

CATALOG = dbutils.widgets.get("catalog")
MODE = dbutils.widgets.get("mode")
LOOKBACK = int(dbutils.widgets.get("lookback_days"))
SCOPE = dbutils.widgets.get("secret_scope")

# One volume per catalog, each over its own path: Unity Catalog refuses two external
# volumes on one location, which is the right call — two names for one path is
# ambiguous ownership, and it forced dev and prod to stop sharing a landing zone.
LANDING = f"/Volumes/{CATALOG}/bronze/landing"
print(f"catalog={CATALOG}  mode={MODE}  landing={LANDING}")

# COMMAND ----------

from datetime import datetime, timedelta, timezone   # noqa: E402
from pathlib import Path                                    # noqa: E402

from src.ingestion import weather_client as wx              # noqa: E402
from src.ingestion.eia_client import (                      # noqa: E402
    DEFAULT_TYPES, ROUTE_START, EIAClient)
from src.ingestion.eia_client import run_extract as eia_extract   # noqa: E402

os.environ["EIA_API_KEY"] = dbutils.secrets.get(SCOPE, "eia_api_key")
root = Path(LANDING)
today = datetime.now(timezone.utc).date()
run_ts = datetime.now(timezone.utc).replace(microsecond=0)

if MODE == "backfill":
    eia_start, wx_start = ROUTE_START, wx.TEMP_START
else:
    # A lookback window rather than "since last run". Upstream revises recent hours, so
    # re-fetching them is the point: each pass lands a new vintage and Silver resolves
    # them. Asking only for new hours would silently miss every correction.
    eia_start = wx_start = today - timedelta(days=LOOKBACK)

eia_start = max(eia_start, ROUTE_START)
wx_start = max(wx_start, wx.TEMP_START)
# The weather archive lags real time; yesterday is the last complete day.
wx_end = today - timedelta(days=1)

# COMMAND ----------

print("### EIA-930")
eia_rows, eia_files = eia_extract(
    EIAClient(os.environ["EIA_API_KEY"], "PJM"), eia_start, today, root, DEFAULT_TYPES)

# COMMAND ----------

print("### Open-Meteo previous runs")
wx_rows, wx_files = wx.run_extract(wx_start, wx_end, root, run_ts)

# COMMAND ----------

import json  # noqa: E402

out = {
    "catalog": CATALOG, "mode": MODE, "landing": LANDING,
    "eia": {"rows": eia_rows, "files": eia_files,
            "from": str(eia_start), "to": str(today)},
    "weather": {"rows": wx_rows, "files": wx_files,
                "from": str(wx_start), "to": str(wx_end)},
}
print(json.dumps(out, indent=2))
# Landing more than zero files is the weakest useful assertion: a run that fetched
# nothing but reported success is how a silently broken feed survives for weeks.
assert eia_files > 0 and wx_files > 0, f"ingest landed no files: {out}"
dbutils.notebook.exit(json.dumps(out))
