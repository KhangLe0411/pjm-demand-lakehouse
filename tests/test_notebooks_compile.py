"""Notebook sources must at least parse.

Databricks notebooks are valid Python — the `# MAGIC` and `# COMMAND ----------` lines
are comments — so a syntax error is catchable without a workspace. Worth a test because
the feedback loop otherwise runs through `bundle deploy`, a job run, and a task failure,
which is minutes per typo; a broken f-string cost exactly that.

This checks syntax only. `dbutils` and `spark` are undefined here and that is fine:
undefined names are a runtime concern, not a parse one.
"""
import ast
import pathlib

import pytest

NOTEBOOKS = sorted(pathlib.Path("notebooks").glob("*.py"))


def test_there_are_notebooks_to_check():
    assert NOTEBOOKS, "no notebooks found — has the directory moved?"


@pytest.mark.parametrize("nb", NOTEBOOKS, ids=lambda p: p.name)
def test_notebook_parses(nb):
    ast.parse(nb.read_text(), filename=str(nb))


@pytest.mark.parametrize("nb", NOTEBOOKS, ids=lambda p: p.name)
def test_notebook_declares_the_databricks_header(nb):
    """Without the first line Databricks imports the file as a plain file, not a
    notebook, and the job task then fails to find its notebook at all."""
    assert nb.read_text().startswith("# Databricks notebook source"), nb


@pytest.mark.parametrize("nb", NOTEBOOKS, ids=lambda p: p.name)
def test_catalog_is_a_parameter_not_a_constant(nb):
    """dev and prod must differ in configuration, never in code. A hard-coded catalog
    would make a dev run write to prod tables."""
    src = nb.read_text()
    assert '"energy.' not in src, f"{nb}: hard-coded catalog reference"
    assert 'dbutils.widgets' in src, f"{nb}: no parameters declared"


def test_requirements_cover_every_third_party_import():
    """A module imported but not declared installs fine locally and fails in a clean
    environment. That is exactly how `mlflow` reached CI undeclared: it was pip-installed
    during development and never written down.

    Reads the source rather than the environment, so it fails on a machine that happens
    to have the package lying around — which is the case that matters.
    """
    import ast
    import pathlib
    import re
    import sys

    ALIAS = {"delta": "delta-spark", "sklearn": "scikit-learn",
             "dotenv": "python-dotenv", "yaml": "pyyaml"}
    LOCAL = {"src", "tests", "scripts", "diagram_icons"}

    imported: set[str] = set()
    for f in (list(pathlib.Path("src").rglob("*.py"))
              + list(pathlib.Path("tests").rglob("*.py"))
              + list(pathlib.Path("scripts").rglob("*.py"))):
        tree = ast.parse(f.read_text(), filename=str(f))
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imported |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                imported.add(n.module.split(".")[0])

    third_party = {ALIAS.get(m, m) for m in imported
                   if m not in sys.stdlib_module_names and m not in LOCAL}
    declared = {re.split(r"[=<>\[]", line.strip())[0].lower()
                for line in pathlib.Path("requirements.txt").read_text().splitlines()
                if line.strip() and not line.startswith("#")}

    missing = sorted(p for p in third_party if p.lower() not in declared)
    assert not missing, f"imported but not in requirements.txt: {missing}"

    unused = sorted(declared - {p.lower() for p in third_party})
    assert not unused, f"in requirements.txt but never imported: {unused}"


def test_every_widget_a_notebook_reads_is_passed_by_its_job():
    """A widget the job does not pass falls back to its declared default, silently.

    That is how the MLflow experiment ended up wherever the notebook happened to be
    deployed (D-40): nothing was wrong with the code, something was simply never
    supplied, and the default was plausible enough to run. The failure surfaced weeks
    later as a workspace ACL error naming an `aclPath`, which points nowhere near the
    cause. Checking the two files agree costs nothing and localises it to this line.
    """
    import re

    import yaml

    resources = pathlib.Path("resources")
    checked = 0
    for job_file in sorted(resources.glob("*.job.yml")):
        spec = yaml.safe_load(job_file.read_text())
        for job in (spec.get("resources", {}).get("jobs", {}) or {}).values():
            for task in job.get("tasks", []):
                nb_task = task.get("notebook_task")
                if not nb_task:
                    continue
                notebook = (job_file.parent / nb_task["notebook_path"]).resolve()
                assert notebook.exists(), f"{nb_task['notebook_path']} does not exist"
                reads = set(re.findall(
                    r'dbutils\.widgets\.get\(\s*["\']([^"\']+)["\']', notebook.read_text()))
                passed = set((nb_task.get("base_parameters") or {}).keys())
                assert not (reads - passed), (
                    f"{job_file.name}:{task['task_key']} does not pass "
                    f"{sorted(reads - passed)} to {notebook.name}, which reads them")
                checked += 1
    assert checked >= 8, f"only {checked} notebook tasks checked — did the glob break?"
