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
# MAGIC %md
# MAGIC # Leakage audit — a gate, not a report
# MAGIC
# MAGIC Placed upstream of the forecast task so a leaking feature set stops the pipeline
# MAGIC instead of producing a number. A forecast built on information that was not
# MAGIC available is worse than no forecast: it looks usable and is not.
# MAGIC
# MAGIC The audit runs against the **actual (cutoff, target) pairs in the built table**,
# MAGIC not against a sample, so it cannot pass by testing a friendly subset.

# COMMAND ----------

from src.features.spec import FEATURES_MODEL_B, KNOWN_LEAKY, audit   # noqa: E402

pairs = (spark.table(f"{CATALOG}.gold.demand_features")
         .select("cutoff_utc", "target_timestamp_utc").distinct().collect())
print(f"auditing {len(pairs):,} (cutoff, target) pairs")

violations = []
for r in pairs:
    v = audit(FEATURES_MODEL_B, r["cutoff_utc"], r["target_timestamp_utc"])
    if v:
        violations.extend(v[:1])

# COMMAND ----------

# An audit that only ever passes is indistinguishable from no audit. Each of these
# looks defensible and leaks; if the audit stops catching one, the audit is broken.
#
# Checked across ALL pairs, not one. Two of them leak only past a horizon threshold —
# `demand_same_hour_minus_1d` beyond ~21h, `net_generation_same_hour_minus_2d` beyond
# ~18h — so at an early target hour they are genuinely safe. Asserting on a single
# arbitrary pair failed this task against a correct pipeline; a gate that raises false
# alarms gets switched off, which costs more than the gate was worth.
caught = {s.name: False for s in KNOWN_LEAKY}
for r in pairs:
    for spec in KNOWN_LEAKY:
        if not caught[spec.name] and audit([spec], r["cutoff_utc"],
                                           r["target_timestamp_utc"]):
            caught[spec.name] = True
    if all(caught.values()):
        break
uncaught = [n for n, ok in caught.items() if not ok]
assert not uncaught, f"audit no longer catches known-leaky features: {uncaught}"
print(f"self-check: all {len(KNOWN_LEAKY)} known-leaky features caught somewhere")

# COMMAND ----------

import json  # noqa: E402
out = {"pairs": len(pairs), "violations": len(violations),
       "known_leaky_caught": len(KNOWN_LEAKY)}
for v in violations[:5]:
    print(v)
assert not violations, f"LEAKAGE: {len(violations)} features knowable after the cutof"
print(json.dumps(out, indent=2))
dbutils.notebook.exit(json.dumps(out))
