"""Assert what is deployed matches what the bundle declares.

`bundle deploy` reports success per resource, not per property. Several things can be
true afterwards that nobody intended: a job deployed without `run_as` silently runs as
whoever deployed it, a schedule can be live when the repository says `PAUSED`, and task
notebooks can still point at a path the bundle no longer owns — which is exactly how
prod carried an orphaned user path from D-35 to D-40 without a single red build.

So this compares the two sides rather than checking absolutes: the declared config from
`bundle validate`, the live config from the jobs API. Absolutes go stale — the day
someone deliberately unpauses the schedule, a hard-coded "must be PAUSED" becomes a
false alarm, and a gate that cries wolf gets switched off.

Usage:  python scripts/check_deploy.py --target prod [--profile NAME]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys


def _cli(args: list[str], profile: str | None) -> dict:
    cmd = ["databricks", *args, "--output", "json"]
    if profile:
        cmd += ["--profile", profile]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"databricks {' '.join(args)} failed:\n{p.stderr.strip()[:500]}")
    return json.loads(p.stdout)


def _notebook_paths(job: dict) -> dict[str, str]:
    return {t["task_key"]: t["notebook_task"]["notebook_path"]
            for t in job.get("tasks", []) if t.get("notebook_task")}


def evaluate(root_path: str, declared: dict, ids: dict, live: dict) -> list[str]:
    """declared/live: {job name -> settings}. ids: {job name -> deployed id or None}."""
    failures = []
    for name, want in declared.items():
        if not ids.get(name):
            failures.append(f"{name}: declared but not deployed")
            continue
        got = live.get(name)
        if got is None:
            failures.append(f"{name}: deployed id {ids[name]} but the jobs API returned nothing")
            continue

        # run_as is the D-40 invariant. A job deployed without it runs as the deployer,
        # which is a human on a laptop and an expiring account.
        if got.get("run_as") != want.get("run_as"):
            failures.append(
                f"{name}: run_as is {json.dumps(got.get('run_as'))}, "
                f"declared {json.dumps(want.get('run_as'))}")

        # Includes pause_status, so an unpaused schedule the repository does not ask
        # for is caught — and so is the reverse, a deploy that quietly re-paused one.
        if got.get("schedule") != want.get("schedule"):
            failures.append(
                f"{name}: schedule is {json.dumps(got.get('schedule'))}, "
                f"declared {json.dumps(want.get('schedule'))}")

        want_tasks, got_tasks = _notebook_paths(want), _notebook_paths(got)
        if set(want_tasks) != set(got_tasks):
            failures.append(
                f"{name}: tasks are {sorted(got_tasks)}, declared {sorted(want_tasks)}")
        for task_key, path in sorted(got_tasks.items()):
            if not path.startswith(root_path):
                failures.append(
                    f"{name}.{task_key}: notebook {path} is outside this "
                    f"deployment's root {root_path}")
            elif want_tasks.get(task_key) != path:
                failures.append(
                    f"{name}.{task_key}: notebook is {path}, "
                    f"declared {want_tasks.get(task_key)}")
    return failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--profile", default=None)
    a = ap.parse_args()

    cfg = _cli(["bundle", "validate", "-t", a.target], a.profile)
    root_path = cfg["workspace"]["root_path"]
    declared = cfg.get("resources", {}).get("jobs", {}) or {}

    summary = _cli(["bundle", "summary", "-t", a.target], a.profile)
    ids = {k: v.get("id") for k, v in
           ((summary.get("resources", {}).get("jobs", {}) or {}).items())}

    live = {}
    for name, job_id in ids.items():
        if job_id:
            live[name] = _cli(["jobs", "get", str(job_id)], a.profile)["settings"]

    print(f"target {a.target}  root {root_path}")
    for name in sorted(declared):
        print(f"  {name:<24} id={ids.get(name)}")

    failures = evaluate(root_path, declared, ids, live)
    if failures:
        print("\ndeployed state does not match the bundle:")
        for f in failures:
            print(f"  · {f}")
        return 1
    print("\ndeployed state matches the bundle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
