"""Regression guard for BOUNDS-007 — `bounds where` accepts a file path, not just a symbol.

`run_where` used to treat its argument purely as a symbol, so a path-shaped arg (e.g. `src/foo.py`)
returned zero results even though the file exists and is owned. It now detects a path-shaped query
(contains '/' or matches an existing repo-relative file by posix compare) and reports the owning
subsystem plus every symbol the file defines, while leaving symbol lookups unchanged.
"""
from __future__ import annotations

from bounds.core import locate


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


def test_where_miss_attaches_did_you_mean_substring(py_project):
    """A 0-result symbol lookup carries `suggestions.did_you_mean` for substring matches (miss recovery)."""
    result = locate.run_where(py_project, "hing")  # substring of the real symbol "Thing"
    assert result["count"] == 0
    sugg = result["suggestions"]
    names = {s["symbol"] for s in sugg.get("did_you_mean", [])}
    assert "Thing" in names
    hit = next(s for s in sugg["did_you_mean"] if s["symbol"] == "Thing")
    assert hit["owning_subsystem"] == "models"
    assert hit["try"] == "bounds where Thing"


def test_where_miss_suggests_subsystem_by_name(py_project):
    """A miss whose query matches a subsystem name surfaces a `bounds describe <name>` next step."""
    result = locate.run_where(py_project, "model")  # matches subsystem "models" by name
    assert result["count"] == 0
    subs = {s["subsystem"]: s for s in result["suggestions"].get("subsystems", [])}
    assert "models" in subs
    assert subs["models"]["try"] == "bounds describe models"


def test_where_total_miss_still_gives_a_next_step(py_project):
    """Even a name in nothing gets `note` + `broaden` + `fallback` — never a bare dead-end."""
    sugg = locate.run_where(py_project, "Zzzznotreal")["suggestions"]
    assert sugg["broaden"] == "bounds where Zzzznotreal --prefix"
    assert "did_you_mean" not in sugg and "subsystems" not in sugg
    assert "fallback" in sugg and sugg["note"]


def test_where_prefix_miss_omits_broaden(py_project):
    """In --prefix mode a miss has nothing wider to offer, so no `broaden` hint is emitted."""
    sugg = locate.run_where(py_project, "Zzzznotreal", prefix=True)["suggestions"]
    assert "broaden" not in sugg


def test_where_miss_human_render_includes_suggestions(py_project):
    """Parity: the --human view of a miss re-renders the suggestion block, not just '(no matches)'."""
    from bounds.shared import output

    result = locate.run_where(py_project, "hing")
    rendered = output._render_where_human(result)
    assert "did you mean" in rendered
    assert "bounds where Thing" in rendered


def test_impact_miss_fix_has_where_hint(py_project):
    """An unknown `impact` target raises with a fix that points at `bounds where` (symbol→owner chain)."""
    import pytest

    from bounds.shared import errors

    with pytest.raises(errors.BoundsError) as exc:
        locate.run_impact(py_project, "Zzzznotreal")  # neither a subsystem nor an interface
    assert exc.value.code == errors.E_SUBSYSTEM_NOT_FOUND
    assert "bounds where Zzzznotreal" in (exc.value.fix or "")


def test_where_path_human_render_is_distinct_from_symbol(py_project):
    """BOUNDS-007 JSON-first: the file-query payload renders cleanly in --human (file → owner + syms)."""
    from bounds.shared import output

    result = locate.run_where(py_project, "src/svc/main.py")
    # The dispatch table must route a file-query payload to the file renderer, not the generic dump.
    renderer = output._select_human_renderer(result)
    assert renderer is not None
    rendered = renderer(result)
    assert "src/svc/main.py" in rendered
    assert "svc" in rendered
    assert "run" in rendered
