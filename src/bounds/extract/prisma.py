"""Prisma schema adapter (Phase 3): tables from ``model`` blocks.

Prisma's schema language is a small, well-defined block format, so this is a deterministic
line/brace scanner — zero-LLM, no new tree-sitter dependency. Each ``model`` block becomes a
``create_table`` symbol shaped exactly like the SQL adapter's output, so the schema fold,
``describe``, ``impact`` and drift checks treat a Prisma model identically to a SQL table.

  * table name = the model name, or ``@@map("snake_name")`` when present
  * columns    = scalar field names, or ``@map("col")`` when a field overrides its column

A model is the *current* declared state (Prisma owns its own migrations), so no fold ordering
is needed; symbols are non-exported because the fold is the authority on the table surface.
"""

from __future__ import annotations

import re

from ..models import ExtractResult, Symbol
from .base import LanguageAdapter, make_result

_MODEL_RE = re.compile(r"^\s*model\s+([A-Za-z_]\w*)\s*\{")
_MAP_RE = re.compile(r'@@map\(\s*"([^"]+)"\s*\)')
_FIELD_RE = re.compile(r'^\s*([A-Za-z_]\w*)\s+([A-Za-z_]\w*(?:\[\])?\??)')  # `name  Type[]/Type? ...`
_FIELD_MAP_RE = re.compile(r'@map\(\s*"([^"]+)"\s*\)')

# Prisma's built-in scalar types. Everything else with a PascalCase type name references
# another model or enum and is therefore a relation, not a database column.
_SCALAR_TYPES = frozenset({
    "String", "Boolean", "Int", "BigInt", "Float", "Decimal", "DateTime", "Json", "Bytes",
})


def _is_column_field(field_type: str) -> bool:
    """True when a Prisma field is a real (scalar) column, not a model/enum relation.

    Relation fields (``author User``, ``posts Post[]``, ``profile Profile?``) name another
    model or enum in PascalCase; Prisma materializes them as joins, never as columns, so they
    must stay out of the table's column surface (else ``describe`` reports phantom columns and a
    column-granular contract like ``User.posts`` passes check_contract incorrectly). Scalar
    fields use the built-in types (``String``, ``Int``, ``Json?`` ...), which are real columns.
    """
    base = field_type.rstrip("[]?")  # drop list `[]` / optional `?` modifiers
    if base in _SCALAR_TYPES:
        return True
    # A PascalCase non-scalar type names another model/enum: a relation, not a column.
    return not base[:1].isupper()


def _strip_comment(line: str) -> str:
    """Drop a trailing ``//`` line comment (not inside a string — Prisma maps use plain quotes)."""
    in_str = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_str = not in_str
        elif ch == "/" and not in_str and line[i : i + 2] == "//":
            return line[:i]
    return line


def _brace_delta(line: str) -> int:
    """Net ``{`` minus ``}`` count on ``line``, ignoring braces inside double-quoted strings.

    A field default like ``cfg String @default("{")`` must not desync the model-body brace
    tracking (which would swallow or split the next model).
    """
    in_str = False
    depth = 0
    for ch in line:
        if ch == '"':
            in_str = not in_str
        elif not in_str and ch == "{":
            depth += 1
        elif not in_str and ch == "}":
            depth -= 1
    return depth


def _parse_model_body(lines: list[str], start: int) -> tuple[str | None, list[str], int]:
    """Parse one model body starting just past its ``{``.

    Returns ``(table_name_override, columns, next_index)``. ``table_name_override`` is the
    ``@@map`` value when present, else None (caller falls back to the model name).
    """
    table: str | None = None
    columns: list[str] = []
    i, n, depth = start, len(lines), 1
    while i < n and depth > 0:
        raw = _strip_comment(lines[i])
        depth += _brace_delta(raw)
        stripped = raw.strip()
        if stripped.startswith("@@"):
            mp = _MAP_RE.search(raw)
            if mp:
                table = mp.group(1)
        elif stripped and not stripped.startswith("}"):
            fm = _FIELD_RE.match(raw)
            if fm and _is_column_field(fm.group(2)):
                col_map = _FIELD_MAP_RE.search(raw)
                columns.append(col_map.group(1) if col_map else fm.group(1))
        i += 1
    return table, columns, i


class PrismaAdapter(LanguageAdapter):
    language_name = "prisma"
    extensions = (".prisma",)

    def extract(self, rel_path: str, source: bytes) -> ExtractResult:
        try:
            lines = source.decode("utf-8", "replace").splitlines()
            symbols: list[Symbol] = []
            i, n = 0, len(lines)
            while i < n:
                m = _MODEL_RE.match(lines[i])
                if not m:
                    i += 1
                    continue
                model = m.group(1)
                start_line = i + 1
                mapped, columns, i = _parse_model_body(lines, i + 1)
                symbols.append(Symbol(
                    mapped or model, "table", start_line, exported=False,
                    metadata={"schema_op": "create_table", "columns": sorted(set(columns)), "model": model},
                ))
        except Exception as exc:  # fail soft
            return make_result(rel_path, self.language_name, [], [], source, error=str(exc))
        return make_result(rel_path, self.language_name, symbols, [], source)
