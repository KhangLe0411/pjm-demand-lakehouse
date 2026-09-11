# Workspace setup (UI walkthrough)

One-time setup for the `energy` project inside the existing `dbw-lakeobs` workspace.
Nothing here touches the `lakeobs` observability project.

Reused as-is: subscription, `rg-lakeobs`, storage account `stlakeobs0803`, access
connector `ac-lakeobs`, storage credential `cred-lakeobs`, workspace, metastore.

Created here: one container, one external location, one catalog with four schemas,
one cluster policy, one budget alert.

---

## A1 — Container `energy` (Azure Portal)

1. <https://portal.azure.com> → search `stlakeobs0803` → open the storage account
2. Left menu → **Data storage** → **Containers**
3. **+ Container**
   - Name: `energy`
   - Anonymous access level: **Private (no anonymous access)**
4. **Create**

No RBAC step is needed. `ac-lakeobs` holds *Storage Blob Data Contributor* at
**storage-account scope**, so it reaches new containers automatically.

---

## A2 — External location `loc-energy` (Databricks)

<https://adb-7405610310266341.1.azuredatabricks.net>

1. **Catalog** in the left sidebar
2. **+ Add** (top right) → **Add an external location**
3. Pick **Manual** if offered a choice of method
   - External location name: `loc-energy`
   - URL: `abfss://energy@stlakeobs0803.dfs.core.windows.net/`
   - Storage credential: **`cred-lakeobs`** (existing — do not create a new one)
4. **Create**
5. Open it and press **Test connection**.

Expect Read / List / Write / Delete / Path Exists / Hierarchical Namespace to pass and
**File Events Resource Provision / Teardown to fail** with a 403. That is expected and
not a blocker — press **Force create**. See `docs/decisions.md` D-15 for why file
notification mode is deliberately not enabled.

---

## A3 — Catalog `energy` (Databricks)

1. **Catalog** → **+ Add** → **Add a catalog**
   - Catalog name: `energy`
   - Type: **Standard**
   - Storage location: **`loc-energy`**
2. **Create**

Setting the storage location matters. Left blank, managed tables land in the
Databricks-managed metastore root
(`abfss://unitycatalog@prdkrxefu6n5errfjwev0y4k...`) instead of your own container,
which gives up both cost visibility and control over the data.

---

## A4 — Schemas

In catalog `energy` → **Create schema**, once per name:

```
bronze    silver    gold    ml
```

Leave each storage location blank; they inherit the catalog's.

---

## B1 — Cluster policy

The Compute page's **Policies** tab is easy to miss: it sits at the end of the tab row
(`All-purpose compute | Job compute | SQL warehouses | Pools | Policies`) and collapses
into an overflow menu when the browser window is narrow or the sidebar is expanded.

This one was created from the CLI instead, which turned out to be the better route —
the definition is version-controlled in this repo rather than living only in the
workspace, and that is exactly the form Databricks Asset Bundles consumes in V2.

Definition: [`databricks/policy-energy-single-node.json`](../databricks/policy-energy-single-node.json)

```bash
python3 - <<'EOF'
import json, pathlib
d = json.loads(pathlib.Path("databricks/policy-energy-single-node.json").read_text())
pathlib.Path("/tmp/pol.json").write_text(json.dumps({
    "name": "energy-single-node",
    "definition": json.dumps(d),      # the API wants a JSON-encoded STRING here
}))
EOF

databricks cluster-policies create --json @/tmp/pol.json --profile lakeobs
```

The `definition` field being a JSON-*string* rather than a nested object is the one
gotcha; passing the object directly fails.

Created as `energy-single-node`, id `00094924DD2F31E7`. All nine keys were accepted.

What each line buys:

| Key | Effect |
|---|---|
| `num_workers` fixed 0 | Single node. Autoscale becomes unselectable, not merely discouraged. |
| `autotermination_minutes` fixed 10 | Cannot be raised. A forgotten cluster costs ~10 minutes of idle. |
| `runtime_engine` STANDARD | **Photon off.** Photon roughly doubles the DBU rate and buys nothing under 1M rows. The single largest saving here. |
| `cluster.profile` + `spark.master` + `ResourceClass` | The three settings Databricks actually requires for a genuine single-node cluster. `num_workers: 0` alone is not sufficient. |
| `custom_tags.project` | Makes per-project cost attribution queryable in `system.billing.usage`. |
| `node_type_id` allowlist | Caps VM size to what `eastasia` offers cheaply. |

To use it: **Compute** → **Create compute** → **Policy** dropdown → `energy-single-node`.
To view or edit later: **Compute** → **Policies** tab.

## B2 — Budget alert

1. Portal → **Cost Management + Billing** → **Cost Management** → **Budgets**
2. **+ Add**
   - Scope: the subscription
   - Name: `bg-student-credit`
   - Reset period: Monthly, Amount: `100`
3. Alert conditions: **50%**, **80%**, **95%** of budget → your email

The student credit already caps spend, so this exists to warn you *before* the cap
stops work mid-milestone.

---

## Verification

```bash
P="--profile lakeobs"
databricks external-locations get loc-energy $P
databricks catalogs get energy $P            # storage_root must be the energy container
databricks schemas list energy $P            # bronze, silver, gold, ml
databricks cluster-policies list $P
```

---

## Deliberately not done

**`lakeobs-wh` is left alone.** It belongs to the observability project. It is the only
object in the workspace that can consume the budget quickly (2X-Small serverless,
~$2.8/hour), but it is already at the 5-minute minimum auto-stop. The energy project
simply never points at it: Power BI reads Gold through Import mode against ADLS, at
zero Databricks compute (`docs/decisions.md` D-12).
