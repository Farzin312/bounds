"""Regression guard for BOUNDS-007 — `bounds where` accepts a file path, not just a symbol.

`run_where` used to treat its argument purely as a symbol, so a path-shaped arg (e.g. `src/foo.py`)
returned zero results even though the file exists and is owned. It now detects a path-shaped query
(contains '/' or matches an existing repo-relative file by posix compare) and reports the owning
subsystem plus every symbol the file defines, while leaving symbol lookups unchanged.
"""
from __future__ import annotations

from bounds import locate


def test_where_symbol_query_unchanged(py_project):
    """A plain symbol arg still returns the per-definition rows with match/symbol keys (no regression)."""
    result = locate.run_where(py_project, "Thing")
    assert result["symbol"] == "Thing"
    assert result["match"] == "exact"
    assert result["count"] == 1
    hit = result["results"][0]
    assert hit["file"] == "src/models/thing.py"
    assert hit["owning_subsystem"] == "models"
    assert hit["exposed"] is True


def test_where_path_arg_reports_owner_and_symbols(py_project):
    """A path-shaped arg (contains '/') reports the file's owning subsystem and its symbols."""
    result = locate.run_where(py_project, "src/svc/main.py")
    assert result["query_kind"] == "file"
    assert result["file"] == "src/svc/main.py"
    assert result["owning_subsystem"] == "svc"
    names = {r["symbol"] for r in result["results"]}
    assert "run" in names
    # Every result is tagged with the same owner (the file's owner), and `run` is not in svc.exposes.
    assert all(r["owning_subsystem"] == "svc" for r in result["results"])
    assert next(r for r in result["results"] if r["symbol"] == "run")["exposed"] is False


def test_where_bare_existing_filename_is_treated_as_path(py_project):
    """A bare filename with no '/' that matches an existing source file resolves as a file query."""
    result = locate.run_where(py_project, "src/models/thing.py")
    assert result["query_kind"] == "file"
    assert result["owning_subsystem"] == "models"
    assert result["results"][0]["symbol"] == "Thing"
    assert result["results"][0]["exposed"] is True  # models declares Thing in exposes


def test_where_nonexistent_symbol_stays_a_symbol_query(py_project):
    """A bare name with no '/' that is neither a file nor a symbol stays a (zero-result) symbol query."""
    result = locate.run_where(py_project, "Nonexistent")
    assert result["match"] == "exact"  # symbol-shaped payload, not a file payload
    assert "query_kind" not in result
    assert result["count"] == 0


def test_where_path_human_render_is_distinct_from_symbol(py_project):
    """BOUNDS-007 JSON-first: the file-query payload renders cleanly in --human (file → owner + syms)."""
    from bounds import output

    result = locate.run_where(py_project, "src/svc/main.py")
    # The dispatch table must route a file-query payload to the file renderer, not the generic dump.
    renderer = output._select_human_renderer(result)
    assert renderer is not None
    rendered = renderer(result)
    assert "src/svc/main.py" in rendered
    assert "svc" in rendered
    assert "run" in rendered
