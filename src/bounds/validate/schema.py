"""Subsystem-level SQL schema fold.

SQL files extract per-file DDL statements for cacheability. This module applies those
statements in deterministic migration order and returns the current table catalog.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import ExtractResult


@dataclass
class _TableState:
    name: str
    columns: set[str] = field(default_factory=set)
    files: set[str] = field(default_factory=set)

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": "table", "columns": sorted(self.columns), "files": sorted(self.files)}


def _fold_subsystem_schema(subsystem: str, extracts: dict[str, ExtractResult], file_owner: dict[str, str]) -> dict[str, _TableState]:
    """Fold SQL DDL symbols owned by ``subsystem`` into surviving table state."""
    tables: dict[str, _TableState] = {}
    for rel in _ordered_sql_files(subsystem, extracts, file_owner):
        result = extracts[rel]
        for sym in result.symbols:
            meta = sym.metadata or {}
            op = meta.get("schema_op")
            if op == "create_table":
                table = tables.setdefault(sym.name, _TableState(sym.name))
                table.columns = set(str(c) for c in (meta.get("columns") or []))
                table.files.add(rel)
            elif op == "add_column":
                table_name = str(meta.get("table") or "").strip()
                column = str(meta.get("column") or "").strip()
                if table_name and column:
                    table = tables.setdefault(table_name, _TableState(table_name))
                    table.columns.add(column)
                    table.files.add(rel)
            elif op == "drop_column":
                table_name = str(meta.get("table") or "").strip()
                column = str(meta.get("column") or "").strip()
                if table_name in tables and column:
                    tables[table_name].columns.discard(column)
                    tables[table_name].files.add(rel)
            elif op == "drop_table":
                tables.pop(sym.name, None)
            elif op == "rename_table":
                target = str(meta.get("to") or "").strip()
                if target:
                    table = tables.pop(sym.name, _TableState(sym.name))
                    table.name = target
                    table.files.add(rel)
                    tables[target] = table
            elif op == "rename_column":
                table_name = str(meta.get("table") or "").strip()
                column = str(meta.get("column") or "").strip()
                target = str(meta.get("to") or "").strip()
                if table_name in tables and column and target:
                    tables[table_name].columns.discard(column)
                    tables[table_name].columns.add(target)
                    tables[table_name].files.add(rel)
    return dict(sorted(tables.items()))


def schema_catalog(subsystem: str, extracts: dict[str, ExtractResult], file_owner: dict[str, str]) -> list[dict]:
    return [table.to_dict() for table in _fold_subsystem_schema(subsystem, extracts, file_owner).values()]


def _ordered_sql_files(subsystem: str, extracts: dict[str, ExtractResult], file_owner: dict[str, str]) -> list[str]:
    return sorted(
        (rel for rel, result in extracts.items() if file_owner.get(rel) == subsystem and result.language == "sql"),
        key=_migration_key,
    )


def _migration_key(rel: str) -> tuple[int, str, str]:
    name = rel.rsplit("/", 1)[-1]
    match = re.match(r"^(\d+)", name)
    if match:
        return (0, match.group(1).zfill(32), rel)
    return (1, name, rel)
