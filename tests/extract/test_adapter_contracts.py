"""Tests for LanguageAdapter.check_contract — self-consistency invariants.

Every adapter's contract is a pure function of its own ExtractResult: zero LLM,
zero filesystem, zero tree-sitter parse. These tests construct ExtractResult
directly to verify the contract logic in isolation.
"""

from __future__ import annotations

from bounds import errors
from bounds.extract.prisma import PrismaAdapter
from bounds.extract.sql import SqlAdapter
from bounds.models import ExtractResult, Issue, Symbol


# ===========================================================================
# PrismaAdapter contract
# ===========================================================================

def test_prisma_contract_passes_clean():
    """Normal Prisma output with only scalar columns produces no issues."""
    adapter = PrismaAdapter()
    result = ExtractResult(
        path="schema.prisma",
        language="prisma",
        symbols=[
            Symbol(name="User", kind="table", line=1, metadata={
                "model": "User",
                "columns": ["id", "email", "name", "createdAt"],
            }),
            Symbol(name="Post", kind="table", line=10, metadata={
                "model": "Post",
                "columns": ["id", "title", "content", "published"],
            }),
        ],
    )
    issues = adapter.check_contract(result)
    assert issues == [], f"Expected no issues, got {issues}"


def test_prisma_contract_catches_relation_leak():
    """A column matching a declared model name (PascalCase) fires E_ADAPTER_CONTRACT.

    Prisma relation fields often pluralize/lowercase the model name (``posts`` for
    ``Post``), which the output-level check can't distinguish from a real scalar.
    What it DOES catch: a column whose name *is* a PascalCase model name — the
    strongest signal of a regression where ``_is_column_field`` stopped filtering.
    """
    adapter = PrismaAdapter()
    result = ExtractResult(
        path="schema.prisma",
        language="prisma",
        symbols=[
            Symbol(name="User", kind="table", line=1, metadata={
                "model": "User",
                "columns": ["id", "email", "Post", "Profile"],  # relation field names == model names
            }),
            Symbol(name="Post", kind="table", line=10, metadata={
                "model": "Post",
                "columns": ["id", "title"],
            }),
            Symbol(name="Profile", kind="table", line=20, metadata={
                "model": "Profile",
                "columns": ["id", "bio"],
            }),
        ],
    )
    issues = adapter.check_contract(result)
    assert len(issues) == 1, f"Expected 1 issue, got {len(issues)}: {issues}"
    assert issues[0].code == errors.E_ADAPTER_CONTRACT
    assert "User" in issues[0].message
    assert "Post" in issues[0].message
    assert "Profile" in issues[0].message


def test_prisma_contract_no_metadata_columns_skipped():
    """Table symbols without metadata or columns are silently ignored."""
    adapter = PrismaAdapter()
    result = ExtractResult(
        path="schema.prisma",
        language="prisma",
        symbols=[
            Symbol(name="User", kind="table", line=1, metadata={}),
            Symbol(name="Post", kind="table", line=5, metadata={"model": "Post"}),
        ],
    )
    assert adapter.check_contract(result) == []


def test_prisma_contract_non_table_symbols_skipped():
    """Non-table symbols (enums, schema_meta) don't trigger column checks."""
    adapter = PrismaAdapter()
    result = ExtractResult(
        path="schema.prisma",
        language="prisma",
        symbols=[
            Symbol(name="User", kind="table", line=1, metadata={
                "model": "User",
                "columns": ["id", "email"],
            }),
            Symbol(name="Role", kind="enum", line=10, metadata={}),
        ],
    )
    assert adapter.check_contract(result) == []


# ===========================================================================
# SqlAdapter contract
# ===========================================================================

def test_sql_contract_passes_clean():
    """Normal SQL output with DDL symbols produces no issues."""
    adapter = SqlAdapter()
    result = ExtractResult(
        path="001_init.sql",
        language="sql",
        symbols=[
            Symbol(name="users", kind="table", line=1, exported=False),
            Symbol(name="users.id", kind="column", line=2, exported=False),
        ],
    )
    assert adapter.check_contract(result) == []


def test_sql_contract_catches_all_error_with_header():
    """All statements unparsable + revision header + no result.error → E_ADAPTER_CONTRACT."""
    adapter = SqlAdapter()
    result = ExtractResult(
        path="002_broken.sql",
        language="sql",
        symbols=[
            Symbol(name="revision", kind="schema_meta", line=1, exported=False),
            Symbol(name="something", kind="schema_error", line=3, exported=False),
            Symbol(name="other", kind="schema_error", line=5, exported=False),
        ],
        error=None,
    )
    issues = adapter.check_contract(result)
    assert len(issues) == 1, f"Expected 1 issue, got {len(issues)}: {issues}"
    assert issues[0].code == errors.E_ADAPTER_CONTRACT
    assert "every statement is unparsable" in issues[0].message.lower()


def test_sql_contract_all_error_with_header_and_error_ok():
    """All-error + revision header but result.error is set → no contract issue (already handled)."""
    adapter = SqlAdapter()
    result = ExtractResult(
        path="002_broken.sql",
        language="sql",
        symbols=[
            Symbol(name="revision", kind="schema_meta", line=1, exported=False),
            Symbol(name="something", kind="schema_error", line=3, exported=False),
        ],
        error="all statements failed to parse",
    )
    assert adapter.check_contract(result) == []


def test_sql_contract_all_error_no_header_passes():
    """All-error without revision header → no contract issue (normal failure path)."""
    adapter = SqlAdapter()
    result = ExtractResult(
        path="003_clean_fail.sql",
        language="sql",
        symbols=[
            Symbol(name="stmt1", kind="schema_error", line=1, exported=False),
        ],
        error="all statements failed to parse",
    )
    assert adapter.check_contract(result) == []


def test_sql_contract_mixed_parse_no_issue():
    """Some DDL symbols survive despite schema_meta + schema_error → no contract issue."""
    adapter = SqlAdapter()
    result = ExtractResult(
        path="004_mixed.sql",
        language="sql",
        symbols=[
            Symbol(name="revision", kind="schema_meta", line=1, exported=False),
            Symbol(name="users", kind="table", line=3, exported=False),  # valid DDL
            Symbol(name="garbage", kind="schema_error", line=10, exported=False),
        ],
        error=None,
    )
    assert adapter.check_contract(result) == []


# ===========================================================================
# Wiring — the contract reaches `bounds validate`, in every mode that checks
# ===========================================================================

def test_adapter_contracts_dispatched_in_every_checking_mode():
    """check_adapter_contracts is in the dispatch table for every mode that runs checks
    (quick/full/preflight/audit) — never silently dropped. ``hotfix`` runs none by design.
    Guards the regression where ``quick`` omitted it while ARCHITECTURE §7 claimed it ran there.
    """
    from bounds.validate.checks import CHECKS_BY_MODE, check_adapter_contracts

    for mode in ("quick", "full", "preflight", "audit"):
        assert check_adapter_contracts in CHECKS_BY_MODE[mode], f"missing in {mode!r}"
    assert check_adapter_contracts not in CHECKS_BY_MODE["hotfix"]


def test_adapter_contract_surfaces_through_validate(sample_project, monkeypatch):
    """End-to-end: an E_ADAPTER_CONTRACT raised by an adapter's check_contract reaches the
    ``bounds validate`` JSON (the full dispatch path, not just the in-memory unit tests above),
    and stays advisory — warning severity, exit code 0.

    The clean extractors never emit the masked state (it's a backstop for a regression), so we
    monkeypatch ``SqlAdapter.extract`` for one file to reproduce the masked-failure state: a
    revision header + all-unparsable statements with ``result.error`` wrongly left empty.
    """
    import json

    from click.testing import CliRunner

    from bounds.cli import main
    from bounds.extract.base import make_result
    from bounds.extract.sql import SqlAdapter

    masked_rel = "src/database/9999_masked.sql"
    masked = sample_project / "src" / "database" / "9999_masked.sql"
    masked.write_text("-- revision: deadbeef\nTHIS IS NOT VALID SQL;\n", encoding="utf-8")

    orig_extract = SqlAdapter.extract

    def fake_extract(self, rel_path, source):
        if rel_path.endswith("9999_masked.sql"):
            return make_result(
                rel_path,
                "sql",
                [
                    Symbol("revision", "schema_meta", 1, exported=False),
                    Symbol("stmt", "schema_error", 2, exported=False),
                ],
                [],
                source,
                error=None,  # the regression the contract guards against
            )
        return orig_extract(self, rel_path, source)

    monkeypatch.setattr(SqlAdapter, "extract", fake_extract)
    monkeypatch.chdir(sample_project)

    result = CliRunner().invoke(main, ["validate"])
    assert result.exit_code == 0, result.output  # advisory must never block
    data = json.loads(result.output)
    hits = [i for i in data["issues"] if i["code"] == errors.E_ADAPTER_CONTRACT]
    assert hits, f"E_ADAPTER_CONTRACT did not surface; got {[i['code'] for i in data['issues']]}"
    assert all(i["severity"] == "warning" for i in hits)
    assert any(i["file"] == masked_rel for i in hits)
