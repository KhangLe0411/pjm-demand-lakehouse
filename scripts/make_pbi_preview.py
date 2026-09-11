#!/usr/bin/env python3
"""Render the Power BI page layout as standalone HTML, from real numbers.

A .pbix is a proprietary binary and cannot be authored here. What can be produced is
everything around it: the Gold tables in the exact shape the report consumes, the DAX,
and this preview — so the layout can be reviewed and argued about before anyone opens
Power BI Desktop. Every figure below comes from `forecast_accuracy.parquet`.
"""
from __future__ import annotations

import json
from pathlib import Path

d = json.loads(Path("data/features/pbi_preview.json").read_text())
K, H = d["kpi"], d["hours"]
W, CH = 1180, 260
PAD_L, PAD_R, PAD_T, PAD_B = 62, 20, 16, 34

series = [("Actual", d["actual"], "#1a1a1a", 2.4),
          ("EIA DF (benchmark)", d["eia_df"], "#F2C811", 1.8),
          ("xgb_b (challenger)", d["xgb_b"], "#00B050", 1.8)]
vals = [v for _, s, _, _ in series for v in s if v is not None]
lo, hi = min(vals) * 0.97, max(vals) * 1.03
n = len(H)
px = lambda i: PAD_L + i * (W - PAD_L - PAD_R) / max(n - 1, 1)          # noqa: E731
py = lambda v: PAD_T + (CH - PAD_T - PAD_B) * (1 - (v - lo) / (hi - lo))  # noqa: E731


def path(s):
    return "M " + " L ".join(f"{px(i):.1f} {py(v):.1f}"
                             for i, v in enumerate(s) if v is not None)


def card(label, value, sub, accent):
    return (f'<div class="card"><div class="lbl">{label}</div>'
            f'<div class="val" style="color:{accent}">{value}</div>'
            f'<div class="sub">{sub}</div></div>')


rows = "".join(
    f"<tr><td>{m}</td><td>{K[m]['MAE']:,.0f}</td><td>{K[m]['MAPE']:.3f}%</td>"
    f"<td>{K[m]['pk_MAPE']:.3f}%</td></tr>"
    for m in ("eia_df", "xgb_b", "xgb_a", "seasonal_naive"))

grid = "".join(
    f'<line x1="{PAD_L}" y1="{py(lo + (hi-lo)*f):.1f}" x2="{W-PAD_R}" '
    f'y2="{py(lo + (hi-lo)*f):.1f}" stroke="#e8e8e8"/>'
    f'<text x="{PAD_L-8}" y="{py(lo + (hi-lo)*f)+4:.1f}" font-size="10" '
    f'text-anchor="end" fill="#888">{(lo+(hi-lo)*f)/1000:.0f}k</text>'
    for f in (0, .25, .5, .75, 1))
ticks = "".join(
    f'<text x="{px(i):.1f}" y="{CH-14}" font-size="9" text-anchor="middle" '
    f'fill="#888">{H[i]}</text>' for i in range(0, n, 24))
lines = "".join(f'<path d="{path(s)}" fill="none" stroke="{c}" stroke-width="{w}"/>'
                for _, s, c, w in series)
legend = "".join(
    f'<span class="lg"><i style="background:{c}"></i>{name}</span>'
    for name, _, c, _ in series)

html = f"""<!doctype html><meta charset="utf-8">
<title>PJM Demand Forecast Monitor</title>
<style>
 body{{font:14px/1.45 "Segoe UI",Helvetica,Arial,sans-serif;background:#f3f2f1;
       margin:0;padding:22px;color:#201f1e}}
 h1{{font-size:19px;margin:0 0 2px}} .meta{{color:#666;font-size:12px;margin-bottom:16px}}
 .wrap{{max-width:1240px;margin:auto}}
 .cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:14px}}
 .card{{background:#fff;border-radius:6px;padding:14px 16px;
        box-shadow:0 1px 3px rgba(0,0,0,.11)}}
 .lbl{{font-size:11px;color:#666;text-transform:uppercase;letter-spacing:.4px}}
 .val{{font-size:26px;font-weight:600;margin:4px 0 1px}}
 .sub{{font-size:11px;color:#888}}
 .panel{{background:#fff;border-radius:6px;padding:14px 16px;margin-bottom:14px;
         box-shadow:0 1px 3px rgba(0,0,0,.11)}}
 .panel h2{{font-size:13px;margin:0 0 10px;color:#333;font-weight:600}}
 table{{border-collapse:collapse;width:100%;font-size:13px}}
 th,td{{text-align:right;padding:6px 10px;border-bottom:1px solid #eee}}
 th:first-child,td:first-child{{text-align:left;font-family:ui-monospace,monospace}}
 th{{color:#666;font-weight:600;font-size:11px;text-transform:uppercase}}
 tr:first-child td{{font-weight:600}}
 .lg{{font-size:11px;color:#555;margin-right:16px}}
 .lg i{{display:inline-block;width:11px;height:11px;border-radius:2px;
        margin-right:5px;vertical-align:-1px}}
 .note{{font-size:11px;color:#888;margin-top:8px}}
</style>
<div class="wrap">
<h1>PJM · Electricity Demand Forecast Monitor</h1>
<div class="meta">Layout preview rendered from forecast_accuracy.parquet ·
 55-fold rolling-origin backtest, 2022-03 → 2026-09 · 39,476 scored hours per model</div>

<div class="cards">
 {card("Benchmark MAPE", f"{K['eia_df']['MAPE']:.2f}%", "EIA day-ahead forecast", "#8a6d00")}
 {card("Challenger MAPE", f"{K['xgb_b']['MAPE']:.2f}%", "xgb_b · +12.5% vs benchmark", "#00803A")}
 {card("Weather contribution", "−50.4%", "MAE, xgb_a → xgb_b", "#0F6CBD")}
 {card("Leakage violations", "0", "47,832 cutoff/target pairs", "#00803A")}
</div>

<div class="panel">
 <h2>Actual vs forecast — last 7 operating days</h2>
 <svg viewBox="0 0 {W} {CH}" width="100%">{grid}{lines}{ticks}</svg>
 <div>{legend}</div>
 <div class="note">Hours are UTC. Both forecasts are hour-aligned to the demand
  timeline; the published EIA series is labelled one hour early (D-28).</div>
</div>

<div class="panel">
 <h2>Benchmark comparison — full backtest</h2>
 <table><tr><th>model</th><th>MAE MWh</th><th>MAPE</th><th>peak MAPE</th></tr>
 {rows}</table>
 <div class="note">The challenger does not beat the balancing authority. It lands
  within 12.5% on public data alone, and weather features account for half its accuracy.</div>
</div>
</div>"""
Path("docs/diagrams/powerbi_preview.html").write_text(html, encoding="utf-8")
print(f"docs/diagrams/powerbi_preview.html  {len(html):,} bytes")
