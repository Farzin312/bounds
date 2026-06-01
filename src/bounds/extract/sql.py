"""SQL adapter: deterministic DDL extraction via tree-sitter.

The adapter stays intentionally dumb and per-file cacheable. It records DDL operations
as (non-exported) symbols; the subsystem-level migration fold lives in ``validate.schema``
and is the single authority on a schema's *current* table surface. Per-statement
fail-soft: one unparsable statement is recorded as a ``schema_error`` signal symbol and the
remaining valid statements in the same file still extract — a typo in migration 030 never
erases the tables created in 001.

Covered statements: tables/columns (``CREATE``/``ALTER``/``DROP``/``RENAME``), plus
functions/RPCs, views, indexes, triggers, and types (grammar-native). Postgres RLS has no
tree-sitter grammar yet, so it is recovered without whole-file regex (which would match
inside comments/strings): ``CREATE POLICY`` from the text of the ERROR node it produces, and
``ENABLE/DISABLE ROW LEVEL SECURITY`` structurally from the phantom column the grammar
misparses it into. A genuine parse error beside recovered RLS is never discounted.

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
from .base import LanguageAdapter, canonical_columns, make_result

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

# tree-sitter-sql (0.3.11) has no grammar for Postgres `CREATE POLICY`, so it surfaces as an
# ERROR node. We recover the policy's identity by matching ONLY that ERROR node's text (never
# whole-file text) — comments and string literals are their own node types, never ERRORs, so
# a commented-out `create policy` can't produce a phantom symbol. `ENABLE/DISABLE ROW LEVEL
# SECURITY` is recovered structurally instead (see the add_column phantom in
# _symbols_from_statement), so it needs no regex.
_POLICY_RE = re.compile(
    r"\bcreate\s+policy\s+(?P<name>\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|[\w.]+)\s+on\s+"
    r"(?P<table>\"[^\"]+\"|[\w.]+)",
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
    return canonical_columns(cols)


def _named_create_symbol(ddl, source: bytes, line: int) -> list[Symbol]:
    """A function/view/index/trigger/type ``CREATE`` as one typed symbol.

    The object is named by the first ``object_reference``, except ``CREATE INDEX`` whose
    name is a bare ``identifier`` (its ``object_reference`` is the target table). Index and
    trigger also record the table they target via the ``ON`` clause.
    """
    kind, op = _NAMED_CREATE[ddl.type]
    meta: dict = {"schema_op": op}
    if ddl.type == "create_index":
        name = _first_name(ddl, source)
        on_table = _object_after(ddl, source, "keyword_on")
    else:
        name = _object_name(ddl, source)
        on_table = _object_after(ddl, source, "keyword_on") if ddl.type == "create_trigger" else None
    if on_table:
        meta["table"] = on_table
    if not name:
        return []
    return [Symbol(name, kind, line, exported=False, metadata=meta)]


def _add_column_symbols(child, table: str, source: bytes, line: int) -> list[Symbol]:
    """Symbols for an ``add_column`` node — real columns, or an RLS recovery.

    A real ADD COLUMN carries a ``keyword_add``. Without it, the grammar has misparsed
    ``... ENABLE/DISABLE ROW LEVEL SECURITY`` into a phantom column named "enable"/"disable"
    (the name sits inside a ``column_definition``, so it's read via ``_columns``). We recover
    that structurally as an ``rls`` symbol — no regex, so immune to comment/string false
    positives — instead of leaking a phantom column.
    """
    if not any(c.type == "keyword_add" for c in child.named_children):
        action = next((c.lower() for c in _columns(child, source)
                       if c.lower() in ("enable", "disable")), None)
        if action:
            return [Symbol(table, "rls", line, exported=False,
                           metadata={"schema_op": f"{action}_rls", "table": table})]
        return []
    return [Symbol(f"{table}.{col}", "column", line, exported=False,
                   metadata={"schema_op": "add_column", "table": table, "column": col})
            for col in _columns(child, source)]  # paren-grouped multi-add yields several


def _alter_symbols(ddl, table: str, source: bytes, line: int) -> list[Symbol]:
    """Symbols for an ``alter_table``: add/drop/rename column or rename/RLS of the table."""
    out: list[Symbol] = []
    for child in ddl.named_children:
        if child.type == "add_column":
            out.extend(_add_column_symbols(child, table, source, line))
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
                old, new = _strip(_text(ids[0], source)), _strip(_text(ids[1], source))
                out.append(Symbol(f"{table}.{old}", "rename", line, exported=False,
                                  metadata={"schema_op": "rename_column", "table": table,
                                            "column": old, "to": new}))
    return out


def _symbols_from_statement(stmt, source: bytes) -> list[Symbol]:
    # SQL symbols are never "exported": the materialized table surface is the fold's job
    # (validate.schema), so a dropped table can't survive in exported_names via its CREATE.
    ddl = next((c for c in stmt.named_children if c.type in _DDL_TYPES), None)
    if ddl is None:
        return []
    line = _line(ddl)
    if ddl.type in _NAMED_CREATE:
        return _named_create_symbol(ddl, source, line)
    table = _object_name(ddl, source)
    if not table:
        return []
    if ddl.type == "create_table":
        return [Symbol(table, "table", line, exported=False,
                       metadata={"schema_op": "create_table", "columns": _columns(ddl, source)})]
    if ddl.type == "drop_table":
        return [Symbol(table, "drop", line, exported=False, metadata={"schema_op": "drop_table"})]
    return _alter_symbols(ddl, table, source, line)


def _policies_from_error(node, source: bytes) -> tuple[list[Symbol], list[tuple[int, int]]]:
    """Recover ``CREATE POLICY`` symbols + their source spans from one ERROR node.

    Scoped to the ERROR node only (never whole-file text): tree-sitter classifies comments
    and string literals as their own node types, so a commented-out or string-embedded
    ``create policy`` is never an ERROR and can't produce a phantom symbol. The returned spans
    (each statement from its ``create`` to the next ``;``) let the caller blank policies and
    re-parse, so a genuinely broken statement fused into the same ERROR node still reports.
    """
    out: list[Symbol] = []
    spans: list[tuple[int, int]] = []
    for m in _POLICY_RE.finditer(_text(node, source)):
        name = _strip(m.group("name"))
        table = _strip(m.group("table"))
        out.append(Symbol(f"{table}.{name}", "policy", _line(node), exported=False,
                          metadata={"schema_op": "create_policy", "table": table, "policy": name}))
        # Span = the policy statement, create → its terminating ';'. The ';' legitimately
        # sits past this ERROR node (a policy's USING/CHECK tail parses into a sibling node),
        # so we search the whole source. Assumes the policy is terminated — well-formed SQL
        # always is; an unterminated policy is invalid input Postgres rejects too.
        start = node.start_byte + m.start()
        semi = source.find(b";", start)
        spans.append((start, semi + 1 if semi != -1 else node.end_byte))
    return out, spans


_HEADER_RULES = ((_REV_RE, "revision"), (_DOWN_RE, "down_revision"), (_ORDER_RE, "order"))


def _header_hints(line: str, meta: dict) -> None:
    """Fold any revision/down_revision/order hint on a comment ``line`` into ``meta``."""
    for rx, key in _HEADER_RULES:
        m = rx.match(line)
        if m:
            meta[key] = int(m.group(1)) if key == "order" else m.group(1)


def _revision_meta(root, source: bytes) -> Symbol | None:
    """A ``schema_meta`` symbol carrying revision-chain / explicit-order hints, or None.

    Reads leading ``comment`` nodes only (deterministic; no eval). Lets the fold order an
    Alembic-style offline-SQL migration set by its ``down_revision`` chain, or honour an
    explicit ``-- bounds:order N`` when filenames carry no usable prefix.
    """
    meta: dict[str, object] = {"schema_op": "meta"}
    for child in root.named_children:
        if child.type == "statement":
            break  # headers live at the top of the file; stop once real DDL starts
        if child.type == "comment":
            _header_hints(_text(child, source).strip(), meta)
    if len(meta) == 1:  # only the schema_op marker → no header present
        return None
    return Symbol("<schema-meta>", "schema_meta", 1, exported=False, metadata=meta)


def _residual_unparsed(error_lines: list[int], rls_lines: set[int]) -> int:
    """Genuine error count after discounting one RLS remnant per recovered-RLS line.

    `ENABLE/DISABLE ROW LEVEL SECURITY` parses as a valid alter_table (recovered as an
    `rls` symbol) plus a trailing "level security" ERROR on the SAME line. Drop one error
    per RLS line — matched by line, so a genuine error elsewhere still reports (no masking).
    """
    seen: set[int] = set()
    residual = 0
    for ln in error_lines:
        if ln in rls_lines and ln not in seen:
            seen.add(ln)
            continue
        residual += 1
    return residual


def _blank_spans(source: bytes, spans: list[tuple[int, int]]) -> bytes:
    """Overwrite each ``[start, end)`` byte range with spaces, preserving newlines (so line
    numbers and offsets are unchanged) — used to erase recovered policies before re-parsing."""
    buf = bytearray(source)
    for start, end in spans:
        for i in range(start, min(end, len(buf))):
            if buf[i] != 0x0A:  # keep '\n' so line numbers stay aligned
                buf[i] = 0x20
    return bytes(buf)


def _walk(root, source: bytes) -> tuple[list[Symbol], list[int], list[tuple[int, int]]]:
    """One pass over top-level nodes → (symbols, error_lines, recovered-policy spans)."""
    symbols: list[Symbol] = []
    error_lines: list[int] = []
    policy_spans: list[tuple[int, int]] = []
    meta = _revision_meta(root, source)
    if meta is not None:
        symbols.append(meta)
    for node in root.named_children:
        if node.type == "statement":
            if node.has_error:  # one bad statement: skip it, keep its siblings
                error_lines.append(_line(node))
            else:
                symbols.extend(_symbols_from_statement(node, source))
        elif node.type == "ERROR":
            # A `CREATE POLICY` lands here (no grammar). Recover it and record its span; a
            # genuinely broken statement doesn't match and still counts as an error.
            policies, spans = _policies_from_error(node, source)
            if policies:
                symbols.extend(policies)
                policy_spans.extend(spans)
            else:
                error_lines.append(_line(node))
    return symbols, error_lines, policy_spans


def _scan_file(root, source: bytes) -> tuple[list[Symbol], int]:
    """Walk top-level nodes into (symbols, genuine-unparsed-count). Per-statement fail-soft.

    Policies have no grammar, so they surface as ERROR nodes that tree-sitter may *fuse* with
    an adjacent broken statement. To avoid masking that real error, when policies were
    recovered we blank their spans and re-parse: the residual error count then reflects only
    genuinely-unparsable SQL (RLS remnants still discounted by line).
    """
    symbols, error_lines, policy_spans = _walk(root, source)
    if policy_spans:
        blanked = _blank_spans(source, policy_spans)
        _, error_lines, _ = _walk(_parser().parse(blanked).root_node, blanked)
    rls_lines = {s.line for s in symbols if s.kind == "rls"}
    return symbols, _residual_unparsed(error_lines, rls_lines)


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
            symbols, unparsed = _scan_file(tree.root_node, source)
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
