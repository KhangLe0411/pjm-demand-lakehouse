#!/usr/bin/env python3
"""Apply the fact-check review to the hand-built flow diagram.

Written as a re-runnable script rather than a one-off edit because the file is edited
in diagrams.net, and an editor holding the old state in memory will overwrite whatever
was changed on disk. Re-run this after closing the editor.

Idempotent: applying it twice changes nothing.

Every edit below corresponds to a numbered entry in docs/decisions.md — the diagram is
the first thing a reader sees, so a claim there that the project disproved is worse
than the same claim buried in code.
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

S11 = "<font style='font-size:11px'>"
V2_STYLE = ("rounded=1;whiteSpace=wrap;html=1;fillColor=#EEFBF7;strokeColor=#74CEB6;"
            "dashed=1;fontColor=#087C60;fontSize=12;align=left;spacing=12;"
            "verticalAlign=top;")

# id -> new value.  Each is a correction, not a rewording.
VALUES = {
    # D-03: a local operating day is 23, 24 or 25 hours. The repo has a test guarding
    # against reverting to 24; the diagram was reinstating the bug.
    "contract":
        "<b>Forecast Contract</b><br>Region: <b>PJM</b><br>Cutoff: <b>10:00 local D</b>"
        "<br>Target: <b>23 / 24 / 25</b> hours of D+1<br>"
        f"{S11}DST-aware; never hard-coded 24</font>",

    # D-01: Previous Runs is the source. Historical Forecast is the API that leaks.
    # D-21: only temperature reaches back far enough to be a primary feature.
    "om_card":
        "<b>Previous Runs API</b> — explicit vintage<br>"
        f"{S11}lead-2 = provably pre-cutoff · lead-1 = sensitivity</font><br><br>"
        "🌡 Temperature → CDD / HDD <b>(primary)</b><br>"
        f"{S11}💧 Humidity · 〰 Wind · ☁ Precipitation — 2024-01-19+ only, "
        "ablation not primary</font><br><br>"
        "<font style='font-size:11px;color:#B03A2E'>✕ Historical Forecast API — leaks "
        "at 14-38 h; used as observed weather only</font>",

    # D-18: quarantine is one layer of three. The two flag layers are the interesting
    # ones — they are why quarantine holds 7 rows yet 176,085 was still caught.
    "dqsmall":
        "DQ 3 layers — ① hard bound → <b>quarantine</b> · "
        "② same clock hour ±24 h → <b>flag</b> · ③ historical ceiling → <b>flag</b>",

    # Not built. README lists both as gaps; showing them as present invites a request
    # to open something that does not exist.
    "gh2":
        "<b>CI/CD — planned</b><br><br>PR → Ruff → Pytest → PySpark / DQ tests<br>"
        "Leakage tests → Bundle validate<br>Deploy DEV → integration test → approval<br>"
        f"{S11}needs a service principal to own prod</font>",
    "kv2":
        "<b>Secrets &amp; Identity — planned</b><br><br>"
        "Key Vault / Databricks secret scope<br>"
        "Service Principal / OIDC for automation<br>"
        f"{S11}today: gitignored .env; prod bundle on a user path</font>",
    "obs2":
        "<b>Observability</b><br><br>✓ Forecast error monitoring<br>"
        f"{S11}gold.forecast_accuracy · is_in_sample split</font><br>"
        "<font style='font-size:11px;color:#087C60'>planned: freshness · job status · "
        "rollback</font>",

    # D-29: repo naming is Model A = no weather, Model B = + weather. The ablation is
    # the project's cleanest result and was missing from the diagram entirely.
    "modelcard":
        "<b>Modeling &amp; Benchmarking</b><br>"
        f"{S11}55-fold rolling origin · 39,476 h per model · MAPE</font><br><br>"
        "Baseline 0 — Seasonal Naive (lag-168) &nbsp;<b>8.560%</b><br>"
        "Benchmark 1 — EIA Day-ahead (DF) &nbsp;<b>2.475%</b><br>"
        "Model A — XGBoost, <i>no weather</i> &nbsp;<b>5.299%</b><br>"
        "Model B — XGBoost, <i>+ weather</i> &nbsp;<b>2.678%</b><br><br>"
        f"{S11}weather ablation: <b>MAE −50.4%</b> · Model B is <b>+12.5%</b> vs the "
        "benchmark, not ahead of it</font>",

    "unc": "V2 &nbsp;P10 / P50 / P90 &nbsp;·&nbsp; residual correction",
}

STYLES = {"gh2": V2_STYLE, "kv2": V2_STYLE}

# id -> (y, height). Column 5 is reflowed to make room for the two additions.
GEOM = {
    "om_card":   (427, 210),
    "leakgate":  (160, 54),
    "modelcard": (222, 198),
    "bt":        (428, 52),
    "pk":        (488, 52),
    "ex":        (548, 52),
    "mlreg":     (608, 66),
    "unc":       (682, 32),
}

# The two things the project is most distinctive for, absent from the original.
NEW = [
    ("leakgate", "c5", 1251, 160, 319, 54,
     "🛑 <b>Leakage Audit — gate</b><br>"
     f"{S11}47,832 pairs · 0 violations · 5 known-leaky asserted caught</font>",
     "rounded=1;whiteSpace=wrap;html=1;fillColor=#F3EFFF;strokeColor=#7C63D8;"
     "strokeWidth=2;fontColor=#3B2E7E;fontSize=12;align=center;"),
    ("mlreg", "c5", 1251, 608, 319, 66,
     "◈ <b>MLflow Registry (Unity Catalog)</b><br>"
     f"{S11}energy.ml.demand_forecaster @champion<br>"
     "monthly refit · promotion gated, registering ≠ serving</font>",
     "rounded=1;whiteSpace=wrap;html=1;fillColor=#FAF8FF;strokeColor=#A89AE7;"
     "fontColor=#4C3D99;fontSize=12;align=center;"),
]

EDGE_STYLE = ("edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;strokeWidth=2;"
              "endArrow=block;endFill=1;strokeColor=#7C63D8;")
# The gate sits between features and the model so the picture shows it blocking,
# rather than running alongside as a report nobody has to pass.
DROP_EDGES = {("goldcard", "modelcard")}
ADD_EDGES = [("goldcard", "leakgate"), ("leakgate", "modelcard"), ("ex", "mlreg")]


def apply(path: Path) -> int:
    tree = ET.parse(path)
    root = tree.getroot()
    parent = root.find(".//root")
    cells = {c.get("id"): c for c in root.iter("mxCell")}
    changed = 0

    for cid, cparent, x, y, w, h, value, style in NEW:
        if cid in cells:
            continue
        c = ET.SubElement(parent, "mxCell", id=cid, value=value, style=style,
                          vertex="1", parent=cparent)
        ET.SubElement(c, "mxGeometry", x=str(x), y=str(y), width=str(w),
                      height=str(h), attrib={"as": "geometry"})
        cells[cid] = c
        changed += 1

    for cid, value in VALUES.items():
        c = cells.get(cid)
        if c is not None and c.get("value") != value:
            c.set("value", value)
            changed += 1
    for cid, style in STYLES.items():
        c = cells.get(cid)
        if c is not None and c.get("style") != style:
            c.set("style", style)
            changed += 1
    for cid, (y, h) in GEOM.items():
        c = cells.get(cid)
        if c is None:
            continue
        g = c.find("mxGeometry")
        if (g.get("y"), g.get("height")) != (str(y), str(h)):
            g.set("y", str(y))
            g.set("height", str(h))
            changed += 1

    for c in list(parent):
        if c.get("edge") == "1" and (c.get("source"), c.get("target")) in DROP_EDGES:
            parent.remove(c)
            changed += 1
    have = {(c.get("source"), c.get("target")) for c in parent if c.get("edge") == "1"}
    for i, (s, t) in enumerate(ADD_EDGES):
        if (s, t) in have:
            continue
        e = ET.SubElement(parent, "mxCell", id=f"e_review_{i}", style=EDGE_STYLE,
                          edge="1", parent="1", source=s, target=t)
        ET.SubElement(e, "mxGeometry", relative="1", attrib={"as": "geometry"})
        changed += 1

    if changed:
        tree.write(path, encoding="utf-8", xml_declaration=True)
        ET.parse(path)          # fail loudly rather than leave malformed XML
    return changed


COL5_X = (1240, 1600)
COL5_BOTTOM = 733          # container is y=88 h=645
BAND_TOP = 740             # the cross-cutting strip below the columns


def check_overlaps(path: Path) -> list[str]:
    """Column 5 only — the column this script reflows.

    Bounded above by BAND_TOP so the cross-cutting cards, which share the x range, are
    not reported as overlaps. A check that cries wolf gets ignored, and then so do its
    real findings.
    """
    root = ET.parse(path).getroot()
    items = []
    for c in root.iter("mxCell"):
        g = c.find("mxGeometry")
        if c.get("vertex") != "1" or g is None:
            continue
        x, y, h = (float(g.get(k, 0)) for k in ("x", "y", "height"))
        if COL5_X[0] < x < COL5_X[1] and 150 <= y < BAND_TOP and h < 300:
            items.append((y, y + h, c.get("id")))
    items.sort()
    out = []
    for (y0, y1, a), (y2, _, b) in zip(items, items[1:]):
        if y1 > y2:
            out.append(f"{a} overlaps {b}")
    if items and items[-1][1] > COL5_BOTTOM:
        out.append(f"{items[-1][2]} runs past the column at {items[-1][1]:.0f}")
    return out


if __name__ == "__main__":
    p = Path(sys.argv[1] if len(sys.argv) > 1 else
             Path.home() / "Downloads" / "energy-demand-lakehouse-style-matched.drawio")
    if not p.exists():
        sys.exit(f"not found: {p}")
    n = apply(p)
    print(f"{p.name}: {n} change(s)" if n else f"{p.name}: already up to date")
    problems = check_overlaps(p)
    print("  layout:", "; ".join(problems) if problems else "no overlap, within column")
