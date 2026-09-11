# Local development

Everything except Auto Loader, Unity Catalog and Workflows runs here for free.
See `docs/decisions.md` D-12 for why.

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
export JAVA_HOME=/usr/lib/jvm/java-11-openjdk-amd64   # Spark 3.5 + Java 11
```

Verified working on this machine: Python 3.12.3, OpenJDK 11.0.32, PySpark 3.5.3,
delta-spark 3.2.1.

## Ingestion

```bash
# one-off full history (2019-01-01 -> today)
./.venv/bin/python src/ingestion/eia_client.py --mode backfill

# routine run; re-fetches a lookback window so upstream revisions are captured
./.venv/bin/python src/ingestion/eia_client.py --mode incremental --lookback-days 14

# dump raw API records without writing anything
./.venv/bin/python src/ingestion/eia_client.py --mode probe
```

Landing layout follows README section 10:

```
data/landing/eia/region-data/respondent=PJM/date=YYYY-MM-DD/part-<runTs>.parquet
```

One file per (period date, run). A second run over an already-ingested date writes a
*new* file rather than replacing the old one — that is what lets Bronze accumulate
vintages and Silver measure revisions.

## Spark

```python
from src.spark import local_session
spark = local_session()
```

## Established by measurement, not assumption

| Question | Answer | How |
|---|---|---|
| EIA `period` timezone | **UTC** | Mean summer load profile peaks 22:00 / troughs 09:00 in the returned index; PJM peaks 18:00 / troughs 05:00 EDT. Offset is exactly 4h. |
| EIA hourly history floor | 2019-01-01 | Route metadata `startPeriod`, confirmed by zero row counts for 2015-2018 |
| Weather forecast archive floor | 2021-03-24 | Binary search over the Previous Runs API |
| `value` type | string in the payload | `"value": "132084"` — cast in Silver, never at ingest |
| Units | `megawatthours` | `value-units` field |
