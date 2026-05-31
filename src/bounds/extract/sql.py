"""SQL adapter: deterministic DDL extraction via tree-sitter.

The adapter stays intentionally dumb and per-file cacheable. It records DDL operations
as symbols; the subsystem-level migration fold lives in ``validate.schema``.
"""

from __future__ import annotations

import tree_sitter as ts
import tree_sitter_sql as tssql

from ..models import ExtractResult, ImportRef, Symbol
from .base import LanguageAdapter, make_result

_LANG: ts.Language | None = None


def _parser() -> ts.Parser:
    global _LANG
    if _LANG is None:
        _LANG = ts.Language(tssql.language())
    return ts.Parser(_LANG)


def _text(node: ts.Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _line(node: ts.Node) -> int:
    return node.start_point[0] + 1


def _object_name(node: ts.Node, source: bytes) -> str | None:
    obj = next((c for c in node.named_children if c.type == "object_reference"), None)
    if obj is None:
        return None
    parts = [c for c in obj.named_children if c.type == "identifier"]
    return ".".join(_text(p, source).strip('"`[]') for p in parts) if parts else _text(obj, source).strip('"`[]')


def _first_identifier(node: ts.Node, source: bytes) -> str | None:
    ident = next((c for c in node.named_children if c.type == "identifier"), None)
    return _text(ident, source).strip('"`[]') if ident is not None else None


def _columns(node: ts.Node, source: bytes) -> list[str]:
    cols: list[str] = []
    stack = list(node.named_children)
    while stack:
        cur = stack.pop(0)
        if cur.type == "column_definition":
            name = _first_identifier(cur, source)
            if name:
                cols.append(name)
            continue
        stack[0:0] = list(cur.named_children)
    return sorted(set(cols))


def _symbols_from_statement(stmt: ts.Node, source: bytes) -> list[Symbol]:
    ddl = next((c for c in stmt.named_children if c.type in {"create_table", "alter_table", "drop_table"}), None)
    if ddl is None:
        return []
    table = _object_name(ddl, source)
    if not table:
        return []
    line = _line(ddl)
    if ddl.type == "create_table":
        return [Symbol(table, "table", line, metadata={"schema_op": "create_table", "columns": _columns(ddl, source)})]
    if ddl.type == "drop_table":
        return [Symbol(table, "drop", line, metadata={"schema_op": "drop_table"})]

    out: list[Symbol] = []
    for child in ddl.named_children:
        if child.type == "add_column":
            col = _columns(child, source)
            if col:
                out.append(Symbol(f"{table}.{col[0]}", "column", line, metadata={"schema_op": "add_column", "table": table, "column": col[0]}))
        elif child.type == "drop_column":
            col = _first_identifier(child, source)
            if col:
                out.append(Symbol(f"{table}.{col}", "drop", line, metadata={"schema_op": "drop_column", "table": table, "column": col}))
        elif child.type == "rename_object":
            target = _object_name(child, source)
            if target:
                out.append(Symbol(table, "rename", line, metadata={"schema_op": "rename_table", "to": target}))
        elif child.type == "rename_column":
            ids = [c for c in child.named_children if c.type == "identifier"]
            if len(ids) >= 2:
                old = _text(ids[0], source).strip('"`[]')
                new = _text(ids[1], source).strip('"`[]')
                out.append(Symbol(f"{table}.{old}", "rename", line, metadata={"schema_op": "rename_column", "table": table, "column": old, "to": new}))
    return out


class SqlAdapter(LanguageAdapter):
    language_name = "sql"
    extensions = (".sql",)

    def extract(self, rel_path: str, source: bytes) -> ExtractResult:
        try:
            tree = _parser().parse(source)
            if tree.root_node.has_error:
                return make_result(rel_path, self.language_name, [], [], source, error="SQL parse error")
            symbols: list[Symbol] = []
            for node in tree.root_node.named_children:
                if node.type == "statement":
                    symbols.extend(_symbols_from_statement(node, source))
        except Exception as exc:
            return make_result(rel_path, self.language_name, [], [], source, error=str(exc))
        return make_result(rel_path, self.language_name, symbols, [], source)
