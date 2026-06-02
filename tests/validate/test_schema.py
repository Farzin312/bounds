"""Tests for manifest schema validation (root + subsystem) and its stable E_SCHEMA_INVALID code."""

from __future__ import annotations

from bounds import errors
from bounds.manifest import schema


# ===========================================================================
# Schema validation
# ===========================================================================
def test_schema_root_missing_required_keys():
    """A root manifest missing required version/project keys must each surface as E_SCHEMA_INVALID, not pass silently."""
    issues = schema.validate_root({})
    codes = {i.code for i in issues}
    assert errors.E_SCHEMA_INVALID in codes
    assert any("version" in i.message for i in issues)
    assert any("project" in i.message for i in issues)


def test_schema_subsystem_bad_role():
    """An unknown role value is rejected with E_SCHEMA_INVALID naming 'role' — bad roles must never reach a check site."""
    issues = schema.validate_subsystem("x", {"name": "x", "role": "bogus"})
    assert any(i.code == errors.E_SCHEMA_INVALID and "role" in i.message for i in issues)


def test_schema_root_entry_points_must_be_list():
    """entry_points must be a list: a bare string is a schema error so downstream iteration can't mis-parse a path char-by-char."""
    issues = schema.validate_root(
        {"version": "1", "project": "p", "entry_points": "main.py"}
    )
    assert any(i.code == errors.E_SCHEMA_INVALID and "entry_points" in i.message for i in issues)


def test_schema_root_entry_points_list_ok():
    """A well-formed entry_points list validates clean — the list-type check must not over-trigger on valid input."""
    issues = schema.validate_root(
        {"version": "1", "project": "p", "entry_points": ["main.py", "app.py"]}
    )
    assert issues == []


def test_schema_valid_subsystem_passes():
    """A fully-formed subsystem (name/role/criticality/paths) validates with zero issues — the happy path stays noise-free."""
    issues = schema.validate_subsystem(
        "auth",
        {"name": "auth", "role": "service", "criticality": "core", "paths": ["src/auth"]},
    )
    assert issues == []


def test_schema_root_sdd_config_validates_known_agent_and_phases():
    """The optional SDD block is accepted only for the fields Bounds acts on, so typoed agents/phases do not silently alter generated guidance."""
    ok = schema.validate_root(
        {
            "version": "1",
            "project": "p",
            "sdd": {
                "enabled": True,
                "agent": "codex",
                "phases": ["specify", "clarify", "plan", "tasks", "analyze", "implement", "verify"],
            },
        }
    )
    assert ok == []

    bad = schema.validate_root(
        {
            "version": "1",
            "project": "p",
            "sdd": {"enabled": "yes", "agent": "robot", "phases": ["specify", "ship"]},
        }
    )
    assert sum(i.code == errors.E_SCHEMA_INVALID for i in bad) == 3
