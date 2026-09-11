"""Assert a job run produced data, not just a green tick.

Every task exits a JSON summary, and until now nothing read it. A pipeline can
terminate SUCCESS while writing nothing: Auto Loader finds no new files, a join drops
every row, an upstream API returns an empty page. The run is green, the tables are
stale, and the first person to notice is whoever reads a forecast that never updated.

This turns each task's own summary into a post-condition. It is deliberately separate
from the assertions inside the notebooks: those check invariants the notebook can see,
this checks that the numbers crossing the task boundary are non-trivial.

Usage:  python scripts/check_run.py <run_id> [--profile NAME]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

# task_key -> list of (label, predicate over the task's exit JSON).
# Absent tasks are reported, not skipped: a task that vanished from the job is exactly
# the kind of change this should notice.
EXPECTED = {
    "ingest": [
        ("eia rows fetched", lambda d: d["eia"]["rows"] > 0),
        ("weather rows fetched", lambda d: d["weather"]["rows"] > 0),
    ],
    "bronze": [
        ("every bronze table non-empty", lambda d: all(v > 0 for v in d["tables"].values())),
        ("source files tracked", lambda d: all(v > 0 for v in d["distinct_source_files"].values())),
    ],
    "silver": [
        ("electricity_hourly non-empty", lambda d: d["electricity_hourly"] > 0),
        ("weather_forecast_hourly non-empty", lambda d: d["weather_forecast_hourly"] > 0),
        # Quarantine is allowed to be non-zero — it is a working DQ lane, not a fault.
        # What would be wrong is everything landing there.
        ("quarantine is a minority",
         lambda d: d["electricity_quarantine"] < d["electricity_hourly"]),
    ],
    "features": [
        ("feature rows built", lambda d: d["rows"] > 0),
        ("some rows are labelled", lambda d: d["labelled"] > 0),
    ],
    "leakage_audit": [
        ("no leakage violations", lambda d: d["violations"] == 0),
        # Zero violations over zero pairs is vacuous — the check that matters is that
        # the audit actually had something to audit.
        ("audit examined pairs", lambda d: d["pairs"] > 0),
        ("known-leaky set non-empty", lambda d: d["known_leaky_caught"] > 0),
    ],
}


def evaluate(outputs: dict, verbose: bool = False) -> list[str]:
    """Apply EXPECTED to the task exit values. Pure, so it is unit-tested without a
    workspace — a checker whose failure path never runs is not known to have one."""
    failures = []
    for task_key, checks in EXPECTED.items():
        data = outputs.get(task_key)
        if data is None:
            failures.append(f"{task_key}: no JSON exit value (task missing or changed?)")
            continue
        for label, predicate in checks:
            try:
                ok = predicate(data)
            except (KeyError, TypeError) as exc:
                failures.append(f"{task_key}: {label} — malformed exit value ({exc})")
                continue
            if verbose:
                print(f"  {'PASS' if ok else 'FAIL'}  {task_key:<14} {label}")
            if not ok:
                failures.append(f"{task_key}: {label} — got {json.dumps(data)}")
    return failures


def _cli(args: list[str], profile: str | None) -> dict:
    cmd = ["databricks", *args, "-o", "json"]
    if profile:
        cmd += ["--profile", profile]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"databricks {' '.join(args)} failed:\n{p.stderr.strip()[:500]}")
    return json.loads(p.stdout)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--profile", default=None)
    a = ap.parse_args()

    run = _cli(["jobs", "get-run", a.run_id], a.profile)
    state = (run.get("status") or {}).get("state")
    print(f"run {a.run_id}: {state}")

    outputs = {}
    for task in run.get("tasks", []):
        out = _cli(["jobs", "get-run-output", str(task["run_id"])], a.profile)
        result = (out.get("notebook_output") or {}).get("result")
        if result:
            try:
                outputs[task["task_key"]] = json.loads(result)
            except json.JSONDecodeError:
                print(f"  {task['task_key']}: exit value is not JSON — ignored")

    failures = evaluate(outputs, verbose=True)

    if failures:
        print("\nrun is green but its output is not:")
        for f in failures:
            print(f"  · {f}")
        return 1
    print("\nall post-conditions hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
