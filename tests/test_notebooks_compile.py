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
