#!/usr/bin/env python3
"""Generate the architecture diagram as both .drawio (editable) and .svg (previewable).

One layout definition drives both outputs, so the picture in the README can never drift
from the file someone opens to edit it.

Layout follows the column-band convention common to enterprise platform decks: stages
left to right, cross-cutting concerns as full-width bars underneath, a status legend
top-right, and a vendor mark in every box. The fourth legend slot in such decks is
usually "Convert"; here it is "Excluded", because on a portfolio diagram what was
deliberately left out carries more signal than another shade of done.
"""
from __future__ import annotations

import html
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diagram_icons import data_uri, svg_href   # noqa: E402

W, H = 1880, 1180

STATUS = {   # name: (fill, stroke, text, legend label)
    "done":     ("#00B050", "#00803A", "#FFFFFF", "Built + tested"),
    "wip":      ("#FFC000", "#BF8F00", "#1A1A1A", "Next (M1 / M5)"),
    "planned":  ("#CCC0DA", "#8064A2", "#1A1A1A", "V2"),
    "excluded": ("#F2F2F2", "#C00000", "#C00000", "Excluded, on purpose"),
    "cross":    ("#F4F8FC", "#6C8EBF", "#1A1A1A", ""),
}

COLUMNS = [
    ("Data Source",           40, 240),
    ("Ingestion",            300, 210),
    ("Storage & Processing", 530, 440),
    ("Curate & Serve",       990, 220),
    ("Analyze",             1230, 215),
    ("Product",             1465, 180),
    ("Consume",             1665, 195),
]

# (id, column, y, height, status, icon, title, subtitle)
NODES = [
    ("eia", 0, 150, 76, "done", "api", "EIA-930  ·  API v2",
     "D / DF / NG / TI\nhourly UTC, from 2019-01-01"),
    ("om", 0, 246, 76, "done", "weather", "Open-Meteo  ·  Previous Runs",
     "8 PJM load-zone points\nlead 1 + 2, from 2021-03-24"),
    ("omx", 0, 342, 68, "excluded", "weather", "Historical Forecast API",
     "stitched short-lead → leaks\nobserved weather only"),

    ("ext1", 1, 150, 76, "done", "python", "eia_client.py",
     "pagination · retry/backoff\nbackfill + incremental"),
    ("ext2", 1, 246, 76, "done", "python", "weather_client.py",
     "8 points per call\n6-month chunks"),
    ("sched", 1, 342, 56, "planned", "azure", "Azure Function  ·  hourly", ""),
    ("kafka", 1, 418, 56, "excluded", "stream", "Kafka / Event Hubs",
     "no continuous compute in V1"),

    ("landing", 2, 150, 76, "done", "adls", "ADLS Gen2  ·  landing/",
     "Parquet, date-partitioned\none file per (date, run)"),
    ("al", 2, 246, 76, "done", "databricks", "Auto Loader  ·  cloudFiles",
     "directory listing · availableNow\nidempotent, asserted"),
    ("bronze", 2, 342, 76, "done", "delta", "bronze.eia_region_data",
     "271,989 + 777,600 rows\nvalue stays STRING"),
    ("silver", 2, 438, 86, "done", "delta", "silver.electricity_hourly",
     "gap-free UTC spine · 67,375\nrevisions resolved · DF aligned"),
    ("silverw", 2, 538, 76, "done", "delta", "silver.weather_forecast_hourly",
     "765,696 rows · CDD/HDD\nboth vintages kept"),
    ("quar", 2, 628, 66, "done", "quarantine", "silver.…_quarantine",
     "7 rows · reason + raw text"),

    ("gdaily", 3, 150, 86, "done", "delta", "gold.daily_summary",
     "2,809 local operating days\nexpected_hours 23/24/25"),
    ("gfeat", 3, 250, 100, "done", "delta", "gold.demand_features",
     "47,832 rows · 33 features\ncutoff-anchored · 0 leakage"),

    ("naive", 4, 150, 56, "done", "model", "Seasonal naive  ·  lag-168",
     "MAPE 8.560%"),
    ("bench", 4, 220, 76, "done", "api", "EIA DF benchmark",
     "aligned · MAPE 2.435%\npeak-timing hit 62.5%"),
    ("xgba", 4, 310, 56, "done", "model", "XGBoost A  ·  no weather",
     "MAE 5,203 · MAPE 5.299%"),
    ("xgbb", 4, 380, 56, "done", "model", "XGBoost B  ·  + weather",
     "MAE 2,581 · +12.5% vs bench"),
    ("bt", 4, 450, 66, "done", "model", "Rolling-origin backtest",
     "monthly refit"),

    ("reg", 5, 150, 92, "done", "mlflow", "MLflow Registry · UC",
     "energy.ml.demand_forecaster\n@champion · promotion gated\nseparate monthly refit"),
    ("frun", 5, 262, 60, "done", "delta", "gold.forecast_run", "cutoff · model_version"),
    ("fpred", 5, 336, 60, "done", "delta", "gold.forecast_prediction", "predicted_p50"),
    ("facc", 5, 410, 76, "done", "delta", "gold.forecast_accuracy",
     "is_in_sample: ops =/= capability"),

    ("pbi", 6, 150, 86, "planned", "powerbi", "Power BI  ·  Import mode",
     "spec + DAX + preview\n.pbix authored in Desktop"),
    ("wh", 6, 250, 76, "excluded", "warehouse", "SQL Warehouse",
     "~$2.8/h serverless\nwould consume the credit"),
]

EDGES = [
    ("eia", "ext1"), ("om", "ext2"), ("ext1", "landing"), ("ext2", "landing"),
    ("sched", "landing"), ("landing", "al"), ("al", "bronze"), ("bronze", "silver"),
    ("bronze", "quar"), ("silver", "gdaily"), ("silver", "gfeat"), ("silverw", "gfeat"),
    ("gdaily", "bench"), ("gfeat", "xgba"), ("gfeat", "xgbb"),
    ("naive", "bt"), ("bench", "bt"), ("xgba", "bt"), ("xgbb", "bt"),
    ("bt", "reg"), ("reg", "frun"), ("frun", "fpred"), ("fpred", "facc"),
    ("facc", "pbi"), ("gdaily", "pbi"),
]

BARS = [
    ("adls", "Data Quality — three layers, because relative and absolute tests fail differently",
     "hard bound → QUARANTINE (7 rows)   ·   same clock hour ±24h → FLAG (9)   ·"
     "   historical ceiling 165,563 MW → FLAG"),
    ("api", "Leakage control — mechanical, not a review step",
     "FeatureSpec declares provenance → audit() checks availability vs cutoff →"
     " 47,832 pairs, 0 violations   ·   5 known-leaky features asserted CAUGHT"),
    ("databricks", "Unity Catalog — catalog `energy` · schemas bronze / silver / gold / ml",
     "external location loc-energy → abfss://energy@stlakeobs0803   ·   credential cred-lakeobs"
     "   ·   Predictive Optimization DISABLED"),
    ("azure", "Cost guardrails — $100 student credit",
     "serverless-only workspace, so no idle billing (D-32)   ·   measured: ~35 s notebook + 40-60 s env floor (D-38)"
     "   ·   ~90% of the work ran local at $0   ·   Databricks: 3 job runs, 60-94 s"),
    ("python", "Verification",
     "149 pytest   ·   local Bronze == cloud Bronze, row for row (D-33)"
     "   ·   DF↔D offset corrected (D-28)   ·   Asset Bundles: dev/prod isolated (D-35)"),
]

HEAD_Y, BAR_Y0, BAR_H, BAR_GAP = 96, 826, 56, 10
COL_TOP, COL_BOT = 130, 806


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def drawio() -> str:
    mx = ET.Element("mxfile", host="app.diagrams.net", agent="energy-lakehouse")
    dia = ET.SubElement(mx, "diagram", name="Architecture", id="arch-1")
    model = ET.SubElement(dia, "mxGraphModel", dx="1600", dy="900", grid="1",
                          gridSize="10", guides="1", tooltips="1", connect="1",
                          arrows="1", fold="1", page="1", pageScale="1",
                          pageWidth=str(W), pageHeight=str(H), math="0", shadow="0")
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", id="0")
    ET.SubElement(root, "mxCell", id="1", parent="0")

    def cell(cid, value, style, x, y, w, h):
        c = ET.SubElement(root, "mxCell", id=cid, value=value, style=style,
                          vertex="1", parent="1")
        ET.SubElement(c, "mxGeometry", x=str(x), y=str(y), width=str(w),
                      height=str(h), attrib={"as": "geometry"})

    cell("title", "Hourly Incremental Electricity Demand Forecasting Lakehouse  —  PJM",
         "text;html=1;fontSize=24;fontStyle=1;fontColor=#C00000;", 40, 26, 1050, 40)
    cell("sub", "Azure Databricks · ADLS Gen2 · Unity Catalog · EIA-930 + archived weather forecasts",
         "text;html=1;fontSize=13;fontColor=#555555;", 42, 62, 950, 24)

    lx = 1250
    for i, (_, (fill, stroke, txt, label)) in enumerate(
            [(k, v) for k, v in STATUS.items() if v[3]]):
        cell(f"lg{i}", esc(label),
             f"rounded=1;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};"
             f"fontColor={txt};fontSize=10;fontStyle=1;", lx + i * 152, 32, 144, 26)

    for ci, (name, x, w) in enumerate(COLUMNS):
        cell(f"band{ci}", "",
             "rounded=0;whiteSpace=wrap;html=1;fillColor=none;strokeColor=#C00000;"
             "dashed=1;dashPattern=6 6;", x, COL_TOP - 44, w, COL_BOT - COL_TOP + 44)
        cell(f"hdr{ci}", esc(name),
             "text;html=1;fontSize=14;fontStyle=1;align=center;fontColor=#333333;",
             x, HEAD_Y, w, 26)

    for nid, ci, y, h, status, icon, title, sub in NODES:
        fill, stroke, txt, _ = STATUS[status]
        _, x, w = COLUMNS[ci]
        body = f"<b>{esc(title)}</b>"
        if sub:
            sc = "#FFFFFF" if status == "done" else "#444444"
            body += (f"<br/><font style='font-size:9px;color:{sc}'>"
                     + esc(sub).replace("\n", "<br/>") + "</font>")
        dash = "dashed=1;dashPattern=4 4;" if status == "excluded" else ""
        cell(nid, body,
             f"shape=label;whiteSpace=wrap;html=1;rounded=1;arcSize=12;"
             f"fillColor={fill};strokeColor={stroke};fontColor={txt};{dash}"
             f"image={data_uri(icon)};imageWidth=22;imageHeight=22;imageAlign=left;"
             "imageVerticalAlign=middle;spacingLeft=36;align=left;verticalAlign=middle;"
             "fontSize=11;", x + 12, y, w - 24, h)

    for i, (a, b) in enumerate(EDGES):
        e = ET.SubElement(root, "mxCell", id=f"e{i}",
                          style="edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;"
                                "strokeColor=#7F8C8D;strokeWidth=1.5;endArrow=block;"
                                "endFill=1;exitX=1;exitY=0.5;entryX=0;entryY=0.5;",
                          edge="1", parent="1", source=a, target=b)
        ET.SubElement(e, "mxGeometry", relative="1", attrib={"as": "geometry"})

    fill, stroke, txt, _ = STATUS["cross"]
    for i, (icon, head, detail) in enumerate(BARS):
        y = BAR_Y0 + i * (BAR_H + BAR_GAP)
        cell(f"bar{i}",
             f"<b>{esc(head)}</b><br/><font style='font-size:10px;color:#333333'>"
             f"{esc(detail)}</font>",
             f"shape=label;whiteSpace=wrap;html=1;rounded=1;arcSize=8;"
             f"fillColor={fill};strokeColor={stroke};fontColor={txt};"
             f"image={data_uri(icon)};imageWidth=20;imageHeight=20;imageAlign=left;"
             "imageVerticalAlign=middle;spacingLeft=40;align=left;verticalAlign=middle;"
             "fontSize=11;", 40, y, W - 80, BAR_H)

    ET.indent(mx, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(mx, encoding="unicode")


def svg() -> str:
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
         f'width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
         f'font-family="Segoe UI,Helvetica,Arial,sans-serif">',
         f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
         '<text x="40" y="54" font-size="24" font-weight="700" fill="#C00000">'
         'Hourly Incremental Electricity Demand Forecasting Lakehouse — PJM</text>',
         '<text x="42" y="79" font-size="13" fill="#555">Azure Databricks · ADLS Gen2 · '
         'Unity Catalog · EIA-930 + archived weather forecasts</text>']

    lx = 1250
    for i, (_, (fill, stroke, txt, label)) in enumerate(
            [(k, v) for k, v in STATUS.items() if v[3]]):
        x = lx + i * 152
        o.append(f'<rect x="{x}" y="32" width="144" height="26" rx="6" fill="{fill}" '
                 f'stroke="{stroke}"/>')
        o.append(f'<text x="{x+72}" y="49" font-size="10" font-weight="600" '
                 f'text-anchor="middle" fill="{txt}">{esc(label)}</text>')

    for ci, (name, x, w) in enumerate(COLUMNS):
        o.append(f'<rect x="{x}" y="{COL_TOP-44}" width="{w}" height="{COL_BOT-COL_TOP+44}" '
                 f'fill="none" stroke="#C00000" stroke-dasharray="6 6"/>')
        o.append(f'<text x="{x+w/2}" y="{HEAD_Y+18}" font-size="14" font-weight="600" '
                 f'text-anchor="middle" fill="#333">{esc(name)}</text>')

    pos = {}
    for nid, ci, y, h, status, icon, title, sub in NODES:
        fill, stroke, txt, _ = STATUS[status]
        _, cx, cw = COLUMNS[ci]
        x, w = cx + 12, cw - 24
        pos[nid] = (x, y, w, h)
        dash = ' stroke-dasharray="4 4"' if status == "excluded" else ""
        o.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{fill}" '
                 f'stroke="{stroke}" stroke-width="1.4"{dash}/>')
        o.append(f'<image xlink:href="{svg_href(icon)}" x="{x+9}" y="{y+h/2-11}" '
                 f'width="22" height="22"/>')
        lines = [l for l in sub.split("\n") if l] if sub else []
        tx, ty = x + 37, y + (19 if lines else h / 2 + 4)
        o.append(f'<text x="{tx}" y="{ty}" font-size="11" font-weight="700" '
                 f'fill="{txt}">{esc(title)}</text>')
        sc = "#FFFFFF" if status == "done" else "#444"
        for j, line in enumerate(lines):
            o.append(f'<text x="{tx}" y="{ty+14+j*11.5}" font-size="9" fill="{sc}">'
                     f'{esc(line)}</text>')

    for a, b in EDGES:
        ax, ay, aw, ah = pos[a]
        bx, by, bw, bh = pos[b]
        x1, y1, x2, y2 = ax + aw, ay + ah / 2, bx, by + bh / 2
        mid = (x1 + x2) / 2
        o.append(f'<path d="M {x1} {y1} H {mid} V {y2} H {x2-7}" fill="none" '
                 f'stroke="#7F8C8D" stroke-width="1.5"/>')
        o.append(f'<path d="M {x2} {y2} l -7 -4 l 0 8 z" fill="#7F8C8D"/>')

    fill, stroke, txt, _ = STATUS["cross"]
    for i, (icon, head, detail) in enumerate(BARS):
        y = BAR_Y0 + i * (BAR_H + BAR_GAP)
        o.append(f'<rect x="40" y="{y}" width="{W-80}" height="{BAR_H}" rx="6" '
                 f'fill="{fill}" stroke="{stroke}"/>')
        o.append(f'<image xlink:href="{svg_href(icon)}" x="52" y="{y+BAR_H/2-10}" '
                 f'width="20" height="20"/>')
        o.append(f'<text x="82" y="{y+23}" font-size="11" font-weight="700" fill="#1a1a1a">'
                 f'{esc(head)}</text>')
        o.append(f'<text x="82" y="{y+42}" font-size="10" fill="#333">{esc(detail)}</text>')

    o.append("</svg>")
    return "\n".join(o)


if __name__ == "__main__":
    d = Path("docs/diagrams")
    d.mkdir(parents=True, exist_ok=True)
    (d / "architecture.drawio").write_text(drawio(), encoding="utf-8")
    (d / "architecture.svg").write_text(svg(), encoding="utf-8")
    ET.parse(d / "architecture.drawio")
    ET.parse(d / "architecture.svg")
    ids = [n[0] for n in NODES]
    assert len(ids) == len(set(ids)), "duplicate node id"
    for a, b in EDGES:
        assert a in ids and b in ids, f"edge references unknown node: {a}->{b}"
    for ci in range(len(COLUMNS)):
        band = sorted([(n[2], n[2] + n[3], n[0]) for n in NODES if n[1] == ci])
        for (y0, y1, a), (y2, _, b) in zip(band, band[1:]):
            assert y1 <= y2, f"overlap in column {ci}: {a} and {b}"
        if band:
            assert band[-1][1] <= BAR_Y0 - 20, f"column {ci} runs into the bars"
    print(f"nodes {len(NODES)} · edges {len(EDGES)} · bars {len(BARS)} · icons embedded")
    for f in ("architecture.drawio", "architecture.svg"):
        print(f"  {d/f}  {(d/f).stat().st_size:,} bytes")
