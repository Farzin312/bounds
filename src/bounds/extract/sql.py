"""SQL adapter: deterministic DDL extraction via tree-sitter.

The adapter stays intentionally dumb and per-file cacheable. It records DDL operations
as (non-exported) symbols; the subsystem-level migration fold lives in ``validate.schema``
and is the single authority on a schema's *current* table surface. Per-statement
fail-soft: one unparsable statement is recorded as a ``schema_error`` signal symbol and the
remaining valid statements in the same file still extract — a typo in migration 030 never
erases the tables created in 001.

Covered statements: tables/columns (``CREATE``/``ALTER``/``DROP``/``RENAME``), plus
functions/RPCs, views, indexes, triggers, and types (grammar-native). Postgres RLS
(``CREATE POLICY``, ``ENABLE/DISABLE ROW LEVEL SECURITY``) has no tree-sitter grammar yet,
so it is recovered with a deterministic text scan instead of being dropped as a parse error.

A leading comment header (``-- revision: <id>`` / ``-- down_revision: <id>``, or an explicit
``-- bounds:order <n>``) is captured as a ``schema_meta`` signal so the fold can order a
revision-chained migration set deterministically without reading file mtimes.
"""

from __future__ import annotations

import re

try:  # graceful degradation: a missing grammar disables SQL, never crashes extraction
    import tree_sitter as ts
    import tree_sitter_sql as tssql

    _IMPORT_ERROR: str | None = None
except Exception as exc:  # pragma: no cover - exercised only when the wheel is absent
    ts = None  # type: ignore[assignment]
    tssql = None  # type: ignore[assignment]
    _IMPORT_ERROR = str(exc)

from ..models import ExtractResult, Issue, Symbol
from .base import LanguageAdapter, make_result

_LANG = None

# `-- revision: abc` / `-- down_revision: xyz` / `-- bounds:order 5` header lines.
_REV_RE = re.compile(r"^--\s*revision\s*[:=]\s*([\w.\-]+)", re.IGNORECASE)
_DOWN_RE = re.compile(r"^--\s*down[_-]?revision\s*[:=]\s*([\w.\-]+)", re.IGNORECASE)
_ORDER_RE = re.compile(r"^--\s*bounds\s*:\s*order\s*[:=]?\s*(\d+)", re.IGNORECASE)

# `CREATE TABLE` family the grammar resolves to a typed node, mapped to (kind, schema_op).
# The object name is the first `object_reference`; index names are an `identifier` instead.
_NAMED_CREATE = {
    "create_function": ("function", "create_function"),
    "create_view": ("view", "create_view"),
    "create_index": ("index", "create_index"),
    "create_trigger": ("trigger", "create_trigger"),
    "create_type": ("type", "create_type"),
}
_DDL_TYPES = {"create_table", "alter_table", "drop_table"} | set(_NAMED_CREATE)

# tree-sitter-sql (0.3.11) has no grammar for Postgres RLS, so these statements surface as
# ERROR nodes. We recover their identity with a deterministic text scan and discount them
# from the unparsed tally so a policy/RLS file isn't perpetually flagged E_SCHEMA_UNPARSED.
_POLICY_RE = re.compile(
    r"\bcreate\s+policy\s+(?P<name>\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|[\w.]+)\s+on\s+"
    r"(?P<table>\"[^\"]+\"|[\w.]+)",
    re.IGNORECASE,
)
_RLS_RE = re.compile(
    r"\balter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?(?P<table>\"[^\"]+\"|[\w.]+)\s+"
    r"(?P<action>enable|disable)\s+row\s+level\s+security",
    re.IGNORECASE,
)


def _parser():
    global _LANG
    if _LANG is None:
        _LANG = ts.Language(tssql.language())
    return ts.Parser(_LANG)


def _text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _line(node) -> int:
    return node.start_point[0] + 1


def _strip(name: str) -> str:
    """Strip SQL identifier quoting (``"x"`` / `` `x` `` / ``[x]``)."""
    return name.strip("\"`[]")


def _object_name(node, source: bytes) -> str | None:
    """Dotted object name from the first ``object_reference`` under ``node`` (quotes stripped)."""
    obj = next((c for c in node.named_children if c.type == "object_reference"), None)
    if obj is None:
        return None
    parts = [c for c in obj.named_children if c.type in ("identifier", "literal")]
    if parts:
        return ".".join(_strip(_text(p, source)) for p in parts)
    return _strip(_text(obj, source))


def _first_name(node, source: bytes) -> str | None:
    """First identifier/literal directly under ``node`` (handles quoted columns)."""
    ident = next((c for c in node.named_children if c.type in ("identifier", "literal")), None)
    return _strip(_text(ident, source)) if ident is not None else None


def _object_after(node, source: bytes, keyword_type: str) -> str | None:
    """First ``object_reference`` appearing after a given keyword child (e.g. the table
    after ``ON`` for ``CREATE INDEX ... ON t`` / ``CREATE TRIGGER ... ON t``)."""
    seen = False
    for c in node.named_children:
        if c.type == keyword_type:
            seen = True
        elif seen and c.type == "object_reference":
            parts = [x for x in c.named_children if x.type in ("identifier", "literal")]
            if parts:
                return ".".join(_strip(_text(p, source)) for p in parts)
            return _strip(_text(c, source))
    return None


def _column_defs(node):
    """Yield every ``column_definition`` for ``node``, ignoring table constraints.

    ``CREATE TABLE`` wraps columns in a ``column_definitions`` node alongside a separate
    ``constraints`` node (PRIMARY KEY / FOREIGN KEY clauses); ``ADD COLUMN`` holds the
    ``column_definition`` directly. We never descend into ``constraints`` so a
    ``REFERENCES other(oid)`` clause can't leak ``oid`` in as a phantom column.
    """
    for child in node.named_children:
        if child.type == "column_definition":
            yield child
        elif child.type == "column_definitions":
            for c in child.named_children:
                if c.type == "column_definition":
                    yield c


def _columns(node, source: bytes) -> list[str]:
    cols: list[str] = []
    for coldef in _column_defs(node):
        name = _first_name(coldef, source)
        if name:
            cols.append(name)
    return sorted(set(cols))


def _symbols_from_statement(stmt, source: bytes) -> list[Symbol]:
    ddl = next((c for c in stmt.named_children if c.type in _DDL_TYPES), None)
    if ddl is None:
        return []
    line = _line(ddl)
    # SQL symbols are never "exported": the materialized table surface is the fold's job
    # (validate.schema), so a dropped table can't survive in exported_names via its CREATE.
    if ddl.type in _NAMED_CREATE:
        # CREATE FUNCTION/VIEW/TRIGGER/TYPE name the object via the first object_reference;
        # CREATE INDEX names it via a bare identifier (the object_reference is the table).
        kind, op = _NAMED_CREATE[ddl.type]
        if ddl.type == "create_index":
            name = _first_name(ddl, source)
            meta = {"schema_op": op}
            on_table = _object_after(ddl, source, "keyword_on")
            if on_table:
                meta["table"] = on_table
        else:
            name = _object_name(ddl, source)
            meta = {"schema_op": op}
            if ddl.type == "create_trigger":
                on_table = _object_after(ddl, source, "keyword_on")
                if on_table:
                    meta["table"] = on_table
        if not name:
            return []
        return [Symbol(name, kind, line, exported=False, metadata=meta)]

    table = _object_name(ddl, source)
    if not table:
        return []
    if ddl.type == "create_table":
        return [
            Symbol(table, "table", line, exported=False,
                   metadata={"schema_op": "create_table", "columns": _columns(ddl, source)})
        ]
    if ddl.type == "drop_table":
        return [Symbol(table, "drop", line, exported=False, metadata={"schema_op": "drop_table"})]

    out: list[Symbol] = []
    for child in ddl.named_children:
        if child.type == "add_column":
            # A real ADD COLUMN carries a `keyword_add`; without it this is the grammar
            # misparsing `... ENABLE ROW LEVEL SECURITY` into a phantom column "enable".
            # The regex pass owns RLS, so drop the phantom here.
            if not any(c.type == "keyword_add" for c in child.named_children):
                continue
            for col in _columns(child, source):  # paren-grouped multi-add yields several
                out.append(Symbol(f"{table}.{col}", "column", line, exported=False,
                                  metadata={"schema_op": "add_column", "table": table, "column": col}))
        elif child.type == "drop_column":
            col = _first_name(child, source)
            if col:
                out.append(Symbol(f"{table}.{col}", "drop", line, exported=False,
                                  metadata={"schema_op": "drop_column", "table": table, "column": col}))
        elif child.type == "rename_object":
            target = _object_name(child, source)
            if target:
                out.append(Symbol(table, "rename", line, exported=False,
                                  metadata={"schema_op": "rename_table", "to": target}))
        elif child.type == "rename_column":
            ids = [c for c in child.named_children if c.type in ("identifier", "literal")]
            if len(ids) >= 2:
                old = _strip(_text(ids[0], source))
                new = _strip(_text(ids[1], source))
                out.append(Symbol(f"{table}.{old}", "rename", line, exported=False,
                                  metadata={"schema_op": "rename_column", "table": table,
                                            "column": old, "to": new}))
    return out


def _policy_rls_symbols(source: bytes) -> list[Symbol]:
    """Recover ``CREATE POLICY`` and ``ENABLE/DISABLE ROW LEVEL SECURITY`` via a text scan.

    The grammar errors on these Postgres-specific statements, so they would otherwise be
    swallowed as ``schema_error``. Deterministic: ``finditer`` yields in source order and
    line numbers come from a byte count, never wall-clock or set ordering.
    """
    text = source.decode("utf-8", "replace")
    out: list[Symbol] = []
    for m in _POLICY_RE.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        name = _strip(m.group("name"))
        table = _strip(m.group("table"))
        out.append(Symbol(f"{table}.{name}", "policy", line, exported=False,
                          metadata={"schema_op": "create_policy", "table": table, "policy": name}))
    for m in _RLS_RE.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        table = _strip(m.group("table"))
        action = m.group("action").lower()
        out.append(Symbol(table, "rls", line, exported=False,
                          metadata={"schema_op": f"{action}_rls", "table": table}))
    return out


def _revision_meta(root, source: bytes) -> Symbol | None:
    """A ``schema_meta`` symbol carrying revision-chain / explicit-order hints, or None.

    Reads leading ``comment`` nodes only (deterministic; no eval). Lets the fold order an
    Alembic-style offline-SQL migration set by its ``down_revision`` chain, or honour an
    explicit ``-- bounds:order N`` when filenames carry no usable prefix.
    """
    meta: dict[str, object] = {"schema_op": "meta"}
    for child in root.named_children:
        if child.type != "comment":
            # Stop scanning once real DDL starts; headers live at the top of the file.
            if child.type == "statement":
                break
            continue
        line = _text(child, source).strip()
        for rx, key in ((_REV_RE, "revision"), (_DOWN_RE, "down_revision"), (_ORDER_RE, "order")):
            m = rx.match(line)
            if m:
                meta[key] = int(m.group(1)) if key == "order" else m.group(1)
    if len(meta) == 1:  # only the schema_op marker → no header present
        return None
    return Symbol("<schema-meta>", "schema_meta", 1, exported=False, metadata=meta)


class SqlAdapter(LanguageAdapter):
    language_name = "sql"
    extensions = (".sql",)
    contract_description = (
        "A file whose every statement is unparsable is a hard parse failure "
        "(empty symbols + result.error), even when a leading '-- revision:' header "
        "is present — the header is a signal, not a parsed statement, so it must not "
        "mask an all-error migration into a partial result."
    )

    def check_contract(self, result: ExtractResult) -> list[Issue]:
        """Catch an all-unparsable migration whose revision header masked the failure.

        A leading ``-- revision:`` / ``-- bounds:order N`` header is captured as a
        ``schema_meta`` signal, not a parsed statement; it must not let an otherwise
        wholly-unparsable file fold to a partial (empty) result. :meth:`extract` already
        converts that case to a hard ``result.error`` (clearing symbols); this *output*
        check fires only if that guard regresses — a result that still carries the header
        (``schema_meta``) and the unparsed marker (``schema_error``) with no real DDL
        symbol and no ``error`` set.
        """
        kinds = {s.kind for s in result.symbols}
        has_meta = "schema_meta" in kinds
        has_error = "schema_error" in kinds
        has_ddl = any(s.kind not in ("schema_meta", "schema_error") for s in result.symbols)
        if has_meta and has_error and not has_ddl and not result.error:
            return [self._contract_issue(
                "SqlAdapter: every statement is unparsable but a revision header is "
                "present and result.error is empty — the header masked an all-error file "
                "that should be a hard parse failure",
                file=result.path,
                fix="an all-unparsable migration must hard-fail (empty symbols + error); "
                    "a schema_meta header must not count as a parsed statement",
            )]
        return []

    def extract(self, rel_path: str, source: bytes) -> ExtractResult:
        if ts is None:  # grammar wheel absent: report soft, never crash the whole run
            return make_result(rel_path, self.language_name, [], [], source,
                               error=f"tree-sitter-sql unavailable: {_IMPORT_ERROR}")
        try:
            tree = _parser().parse(source)
            symbols: list[Symbol] = []
            meta = _revision_meta(tree.root_node, source)
            if meta is not None:
                symbols.append(meta)
            unparsed = 0
            for node in tree.root_node.named_children:
                if node.type == "statement":
                    if node.has_error:  # one bad statement: skip it, keep its siblings
                        unparsed += 1
                        continue
                    symbols.extend(_symbols_from_statement(node, source))
                elif node.type == "ERROR":
                    unparsed += 1
            # Recover grammar-unsupported RLS statements (each leaves ~one ERROR node, so
            # discount them from the tally rather than double-reporting as schema_error).
            recovered = _policy_rls_symbols(source)
            symbols.extend(recovered)
            unparsed = max(0, unparsed - len(recovered))
            if unparsed:
                # Fail soft, report hard: surfaced as E_SCHEMA_UNPARSED (warning), not a
                # silent drop and not a whole-file failure - the valid statements still folded.
                symbols.append(Symbol("<unparsed>", "schema_error", 1, exported=False,
                                      metadata={"schema_op": "unparsed", "count": unparsed}))
            # A file that is ALL error (no statement parsed at all) is a genuine hard failure.
            # A leading `schema_meta` header is not a parsed statement, so it must not mask an
            # otherwise wholly-unparsable migration (e.g. `-- revision: 1` over a broken body).
            valid = [s for s in symbols if s.kind not in ("schema_error", "schema_meta")]
            if not valid and unparsed:
                return make_result(rel_path, self.language_name, [], [], source,
                                   error="SQL parse error (no statement could be parsed)")
        except Exception as exc:
            return make_result(rel_path, self.language_name, [], [], source, error=str(exc))
        return make_result(rel_path, self.language_name, symbols, [], source)
