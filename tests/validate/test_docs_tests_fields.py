"""Schema + model support for the subsystem `docs:`/`tests:` link fields.

These lock in the manifest-tier contract for the new hybrid linkage fields: the schema accepts and
type-validates them (same Issue/code as `paths`), and SubsystemCompact round-trips them through
to_dict/from_dict, emitting each only when non-empty (lean default). See models.py + manifest/schema.py.
"""
from __future__ import annotations

from bounds import errors
from bounds.manifest import schema
from bounds.models import SubsystemCompact


def test_schema_accepts_valid_docs_and_tests_lists():
    """A subsystem declaring `docs:`/`tests:` as lists of strings validates clean — the hybrid model
    lets a human authoritatively link coverage without tripping schema validation."""
    issues = schema.validate_subsystem(
        "auth",
        {"name": "auth", "role": "library",
         "docs": ["docs/auth.md"], "tests": ["tests/auth", "tests/test_auth.py"]},
    )
    assert issues == []


def test_schema_rejects_non_list_tests_with_schema_invalid():
    """A non-list `tests:` is a schema error (E_SCHEMA_INVALID), mirroring `paths` validation — bad
    manifest data degrades to a reported Issue, never a crash (fail-soft)."""
    issues = schema.validate_subsystem(
        "auth", {"name": "auth", "role": "library", "tests": "tests/auth"}
    )
    assert any(i.code == errors.E_SCHEMA_INVALID and "tests" in i.message for i in issues)


def test_schema_rejects_non_string_doc_entries():
    """A `docs:` list with a non-string entry is rejected — link fields must hold posix path
    strings, so a stray mapping/number can't silently become a bogus path."""
    issues = schema.validate_subsystem(
        "auth", {"name": "auth", "role": "library", "docs": ["docs/auth.md", 7]}
    )
    assert any(i.code == errors.E_SCHEMA_INVALID and "docs" in i.message for i in issues)


def test_subsystem_compact_roundtrips_docs_and_tests():
    """from_dict→to_dict preserves docs/tests exactly — the loader reads them and the JSON re-emits
    them, so a hand-linked manifest survives a parse/serialize cycle unchanged."""
    data = {"name": "auth", "role": "library", "criticality": "core",
            "paths": ["src/auth"], "docs": ["docs/auth.md"], "tests": ["tests/auth"]}
    sub = SubsystemCompact.from_dict(data)
    assert sub.docs == ["docs/auth.md"]
    assert sub.tests == ["tests/auth"]
    out = sub.to_dict()
    assert out["docs"] == ["docs/auth.md"]
    assert out["tests"] == ["tests/auth"]


def test_to_dict_omits_empty_docs_tests():
    """to_dict emits docs/tests only when non-empty (mirroring `namespace`), so a subsystem that
    links neither keeps the lean default JSON shape — no empty `docs: []`/`tests: []` noise."""
    sub = SubsystemCompact(name="x", role="library")
    out = sub.to_dict()
    assert "docs" not in out
    assert "tests" not in out


def test_from_dict_coerces_non_string_entries_safely():
    """from_dict coerces entries to str (same safe list coercion as `paths`/`files`), so a numeric
    YAML entry round-trips as a string instead of crashing the loader."""
    sub = SubsystemCompact.from_dict(
        {"name": "x", "role": "library", "tests": [123], "docs": ["a.md"]}
    )
    assert sub.tests == ["123"]
    assert sub.docs == ["a.md"]
