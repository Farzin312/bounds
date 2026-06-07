"""SQL adapter: deterministic DDL extraction via tree-sitter.

The adapter stays intentionally dumb and per-file cacheable. It records DDL operations
as (non-exported) symbols; the subsystem-level migration fold lives in ``validate.schema``
and is the single authority on a schema's *current* table surface. Per-statement
fail-soft: one unparsable statement is recorded as a ``schema_error`` signal symbol and the
remaining valid statements in the same file still extract — a typo in migration 030 never
erases the tables created in 001.

Two extraction layers, by what tree-sitter can and cannot parse:

* **Grammar-native** (tables/columns via ``CREATE``/``ALTER``/``DROP``/``RENAME``, plus
  functions/RPCs, views, indexes, triggers, types). These are read from the parse tree.
  The walk **descends into transaction containers** (``BEGIN; … COMMIT;``) so DDL wrapped
  in a transaction block — the common Supabase migration shape — is not silently dropped.
* **Postgres RLS dialect** (``CREATE``/``ALTER``/``DROP POLICY`` and
  ``ENABLE``/``DISABLE``/``FORCE`` ``ROW LEVEL SECURITY``). tree-sitter-sql has no grammar
  for these, so they are recovered with a regex pass over the source **after blanking every
  comment span** (comments are their own node type, so a commented-out ``create policy`` is
  masked out and can never produce a phantom symbol). Working on masked text — rather than
  scoping to a single ERROR node — recovers policies regardless of nesting (transactions),
  fragmentation (pg_dump output), or fusion with an adjacent broken statement.

A file with **no schema DDL at all** (a seed-data ``INSERT``, a ``grant``/``revoke``, a
``select cron.schedule(...)`` registration) is *not* a failure: it returns empty symbols and
no error, so describe/validate never mislabel it as "unparsed". A genuine parse failure that
loses real DDL is still reported hard — see :meth:`SqlAdapter.extract`.

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

from ...shared.models import ExtractResult, Issue, Symbol
from .base import LanguageAdapter, canonical_columns, make_result

__all__ = []

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

# Container node types whose children are themselves top-level-equivalent statements: the
# walk descends into these so DDL inside `BEGIN; … COMMIT;` is extracted, not dropped.
_CONTAINER_TYPES = {"transaction"}

# An ``id`` token in a policy/table position: a double/back/bracket-quoted identifier, or a
# bare word. A table reference may be schema-qualified (``"public"."t"`` / ``public.t``). These
# run over the comment/string-masked source as BYTES patterns, so a match's ``.start()`` is a
# byte offset directly usable for line numbers / span blanking (a char index would desync on
# any multibyte identifier).
_ID = rb'"[^"]+"|`[^`]+`|\[[^\]]+\]|\w+'
_QUALIFIED = rb"(?:" + _ID + rb")(?:\s*\.\s*(?:" + _ID + rb"))*"
# The shared `<name> ON <table>` tail of every policy statement (one home for the fragment).
_NAME_ON_TABLE = rb"(?P<name>" + _ID + rb")\s+on\s+(?P<table>" + _QUALIFIED + rb")"

# Postgres RLS dialect — recovered from comment-masked source (no grammar). All are anchored
# on the statement keyword so a substring inside an identifier can't match.
_POLICY_CREATE_RE = re.compile(
    rb"\bcreate\s+policy\s+(?:if\s+not\s+exists\s+)?" + _NAME_ON_TABLE,
    re.IGNORECASE,
)
_POLICY_DROP_RE = re.compile(
    rb"\bdrop\s+policy\s+(?:if\s+exists\s+)?" + _NAME_ON_TABLE,
    re.IGNORECASE,
)
_POLICY_ALTER_RE = re.compile(
    rb"\balter\s+policy\s+" + _NAME_ON_TABLE + rb"(?:\s+rename\s+to\s+(?P<to>" + _ID + rb"))?",
    re.IGNORECASE,
)
_RLS_RE = re.compile(
    rb"\balter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?(?P<table>" + _QUALIFIED + rb")\s+"
    rb"(?P<action>enable|disable|force|no\s+force)\s+row\s+level\s+security",
    re.IGNORECASE,
)

# Postgres dollar-quoted string / function body: ``$$ … $$`` or ``$tag$ … $tag$``. tree-sitter
# emits only the bare ``$$`` delimiters (the body is shredded into loose tokens), so the body is
# masked textually here instead — a ``CREATE POLICY`` inside an ``EXECUTE '…'`` / ``$$ … $$``
# body must never be mistaken for real DDL. The backreference requires a matching close tag.
_DOLLAR_QUOTE_RE = re.compile(rb"\$(\w*)\$[\s\S]*?\$\1\$")

# "Does this file intend any DDL *we model*?" — used to tell a non-schema file (seed/cron/grant)
# from a DDL-bearing parse failure (real loss, reported). Lists exactly the object kinds the
# fold materializes; a `CREATE SCHEMA` / `CREATE EXTENSION` (real DDL we don't model) parses
# fine and simply has no table surface, so it must NOT count as intent and hard-fail. Matched on
# the comment/string/body-masked source.
_DDL_INTENT_RE = re.compile(
    rb"\b(?:create|alter|drop)\s+(?:materialized\s+view|table|policy|index|"
    rb"unique\s+index|trigger|view|function|type)\b"
    rb"|\brow\s+level\s+security\b",
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


def _line_at(source: bytes, byte_off: int) -> int:
    """1-based line number of ``byte_off`` in ``source`` (newline count, deterministic)."""
    return source.count(b"\n", 0, byte_off) + 1


def _strip(name: str) -> str:
    """Strip SQL identifier quoting (``"x"`` / `` `x` `` / ``[x]``) and surrounding space."""
    return name.strip().strip("\"`[]")


def _last_ident(qualified: str) -> str:
    """Last component of a (possibly schema-qualified, possibly quoted) reference."""
    return _strip(qualified.split(".")[-1])


def _table_ref(name: str | None) -> str | None:
    """Canonical table key: the bare table name, schema qualifier dropped.

    A table is referenced inconsistently across a migration set — ``CREATE TABLE
    public.profiles`` here, ``ALTER TABLE profiles`` there, and policies/RLS always name it
    bare. Folding on the bare name is the single canonical form, so every op on a table lands
    on the same catalog entry instead of splitting ``public.profiles`` from ``profiles``.
    (Functions/views/types keep their qualified names — they are objects, not table refs.)
    """
    if name is None:
        return None
    return name.split(".")[-1]


# ---------------------------------------------------------------------------
# Grammar-native extraction (tables / columns / functions / views / indexes / …)
# ---------------------------------------------------------------------------
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
        on_table = _table_ref(_object_after(ddl, source, "keyword_on"))
    else:
        name = _object_name(ddl, source)
        on_table = _table_ref(_object_after(ddl, source, "keyword_on")) if ddl.type == "create_trigger" else None
    if on_table:
        meta["table"] = on_table
    if not name:
        return []
    return [Symbol(name, kind, line, exported=False, metadata=meta)]


def _add_column_symbols(child, table: str, source: bytes, line: int) -> list[Symbol]:
    """Symbols for an ``add_column`` node (real columns only).

    RLS toggles (``ENABLE/DISABLE ROW LEVEL SECURITY``) are recovered separately from
    comment-masked text and their spans blanked before this grammar walk, so the phantom
    ``column_definition`` the grammar would otherwise misparse them into never reaches here.
    """
    if not any(c.type == "keyword_add" for c in child.named_children):
        return []
    return [Symbol(f"{table}.{col}", "column", line, exported=False,
                   metadata={"schema_op": "add_column", "table": table, "column": col})
            for col in _columns(child, source)]  # paren-grouped multi-add yields several


def _alter_symbols(ddl, table: str, source: bytes, line: int) -> list[Symbol]:
    """Symbols for an ``alter_table``: add/drop/rename column or rename of the table."""
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
            target = _table_ref(_object_name(child, source))
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
    table = _table_ref(_object_name(ddl, source))
    if not table:
        return []
    if ddl.type == "create_table":
        return [Symbol(table, "table", line, exported=False,
                       metadata={"schema_op": "create_table", "columns": _columns(ddl, source)})]
    if ddl.type == "drop_table":
        return [Symbol(table, "drop", line, exported=False, metadata={"schema_op": "drop_table"})]
    return _alter_symbols(ddl, table, source, line)


def _iter_statements(node):
    """Yield ``statement``/``ERROR`` nodes, descending into transaction/block containers.

    A ``statement`` or ``ERROR`` is yielded as a unit (never descended into, so a nested
    subquery isn't double-counted). A container node (``transaction`` = ``BEGIN; … COMMIT;``)
    is descended so the DDL it wraps is reached. Everything else (comments, loose keyword
    tokens left by a fragmented parse) is skipped.
    """
    for child in node.named_children:
        if child.type in ("statement", "ERROR"):
            yield child
        elif child.type in _CONTAINER_TYPES:
            yield from _iter_statements(child)


def _grammar_symbols(root, source: bytes) -> tuple[list[Symbol], list[tuple[int, int]]]:
    """Grammar-native DDL symbols + the byte span of every error-bearing node, in one pass.

    Error spans (not just a count) let the caller classify each error as *lost DDL* (reported)
    vs benign non-schema noise — a ``cron.schedule`` call or ``grant`` the grammar trips on.
    """
    symbols: list[Symbol] = []
    error_spans: list[tuple[int, int]] = []
    for node in _iter_statements(root):
        if node.type == "statement":
            # Best-effort even when the statement has an error: tree-sitter-sql can't parse a
            # table-level `CONSTRAINT … UNIQUE (…)` / `CHECK (…)` clause, which errors the WHOLE
            # `CREATE TABLE` — yet the table name and its column_definitions are still parsed.
            # Recovering them keeps the table (an unmodeled constraint tail is not a catalog
            # loss). Only a statement that yields no DDL at all counts as a parse error.
            recovered = _symbols_from_statement(node, source)
            if recovered:
                symbols.extend(recovered)
            elif node.has_error:
                error_spans.append((node.start_byte, node.end_byte))
        else:  # ERROR node
            error_spans.append((node.start_byte, node.end_byte))
    return symbols, error_spans


# ---------------------------------------------------------------------------
# Postgres RLS dialect recovery (comment-masked, textual order)
# ---------------------------------------------------------------------------
def _mask_spans(root, source: bytes) -> list[tuple[int, int]]:
    """Byte spans to blank before the RLS regex + DDL-intent check, recursively.

    Masks **comments** (a commented-out ``create policy`` is not a policy), **single-quoted
    string literals**, and **function bodies / dollar-quoted strings** (so a ``CREATE TABLE``
    inside ``VALUES ('…')`` seed data, or a ``CREATE POLICY`` inside an ``EXECUTE '…'`` /
    ``$$ … $$`` function body, is never mistaken for real DDL). Double-quoted identifiers
    (``"public"``) are deliberately *not* masked — they're real table/object names.
    """
    spans: list[tuple[int, int]] = []
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type in ("comment", "marginalia"):
            spans.append((n.start_byte, n.end_byte))
            continue
        if n.type == "literal" and n.start_byte < n.end_byte and source[n.start_byte] == 0x27:
            spans.append((n.start_byte, n.end_byte))  # single-quoted string, not an identifier
            continue
        stack.extend(n.children)
    # Dollar-quoted bodies are masked textually: tree-sitter spans only the bare `$$` markers,
    # not the body, so a CREATE POLICY inside a `$$ … $$` / EXECUTE body would otherwise leak.
    spans.extend((m.start(), m.end()) for m in _DOLLAR_QUOTE_RE.finditer(source))
    return spans


def _blank_spans(source: bytes, spans: list[tuple[int, int]]) -> bytes:
    """Overwrite each ``[start, end)`` byte range with spaces, preserving newlines so line
    numbers and byte offsets are unchanged — used to mask comments and recovered RLS."""
    if not spans:
        return source
    buf = bytearray(source)
    for start, end in spans:
        for i in range(max(start, 0), min(end, len(buf))):
            if buf[i] != 0x0A:  # keep '\n' so line numbers stay aligned
                buf[i] = 0x20
    return bytes(buf)


def _stmt_span(masked: bytes, start: int) -> tuple[int, int]:
    """``[start, end)`` of the statement beginning at ``start`` — through its terminating
    ``;`` (a policy's USING/CHECK tail can run past the keyword), else end of source."""
    semi = masked.find(b";", start)
    return (start, semi + 1 if semi != -1 else len(masked))


def _recover_rls(masked: bytes) -> tuple[list[Symbol], list[tuple[int, int]]]:
    """Recover policy + RLS symbols from comment-masked source, in textual order.

    Returns ``(symbols, spans)``. ``symbols`` are ordered by source position so an
    in-file create-then-drop of the same policy folds correctly. ``spans`` are blanked
    before the grammar walk so a recovered statement can't also be counted as a parse error
    or misparsed into a phantom column.
    """
    found: list[tuple[int, Symbol]] = []
    spans: list[tuple[int, int]] = []

    def add(m, sym: Symbol) -> None:  # m.start() is a byte offset (bytes pattern over `masked`)
        found.append((m.start(), sym))
        spans.append(_stmt_span(masked, m.start()))

    def grp(m, key: str) -> str:
        return m.group(key).decode("utf-8", "replace")

    for m in _POLICY_CREATE_RE.finditer(masked):
        table, name = _last_ident(grp(m, "table")), _strip(grp(m, "name"))
        add(m, Symbol(f"{table}.{name}", "policy", _line_at(masked, m.start()), exported=False,
                      metadata={"schema_op": "create_policy", "table": table, "policy": name}))
    for m in _POLICY_DROP_RE.finditer(masked):
        table, name = _last_ident(grp(m, "table")), _strip(grp(m, "name"))
        add(m, Symbol(f"{table}.{name}", "policy", _line_at(masked, m.start()), exported=False,
                      metadata={"schema_op": "drop_policy", "table": table, "policy": name}))
    for m in _POLICY_ALTER_RE.finditer(masked):
        table, name = _last_ident(grp(m, "table")), _strip(grp(m, "name"))
        meta = {"schema_op": "alter_policy", "table": table, "policy": name}
        if m.group("to"):
            meta["to"] = _strip(grp(m, "to"))
        add(m, Symbol(f"{table}.{name}", "policy", _line_at(masked, m.start()), exported=False, metadata=meta))
    for m in _RLS_RE.finditer(masked):
        table = _last_ident(grp(m, "table"))
        action = re.sub(r"\s+", " ", grp(m, "action").strip().lower())
        op = {"enable": "enable_rls", "disable": "disable_rls",
              "force": "force_rls", "no force": "no_force_rls"}[action]
        add(m, Symbol(table, "rls", _line_at(masked, m.start()), exported=False,
                      metadata={"schema_op": op, "table": table}))

    found.sort(key=lambda t: t[0])
    return [sym for _, sym in found], spans


# ---------------------------------------------------------------------------
# Header (revision / order) recovery
# ---------------------------------------------------------------------------
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
        if child.type in ("statement", "transaction"):
            break  # headers live at the top of the file; stop once real DDL starts
        if child.type == "comment":
            _header_hints(_text(child, source).strip(), meta)
    if len(meta) == 1:  # only the schema_op marker → no header present
        return None
    return Symbol("<schema-meta>", "schema_meta", 1, exported=False, metadata=meta)


# ---------------------------------------------------------------------------
# Per-file scan
# ---------------------------------------------------------------------------
def _scan_file(source: bytes) -> tuple[list[Symbol], int, bool]:
    """Scan one file → ``(symbols, ddl_errors, has_ddl_intent)``.

    Grammar-native DDL is read after blanking recovered RLS spans (so policy ERROR nodes are
    un-fused from adjacent statements and the phantom RLS column is suppressed). Each remaining
    parse error is classified against the comment/string/body-masked text: ``ddl_errors`` are
    errors that actually carry DDL (lost schema → reported); the rest are benign non-schema
    noise (a ``cron.schedule`` call the grammar trips on, a complex ``insert``) and are ignored.
    ``has_ddl_intent`` is whole-file: does the masked source mention any DDL at all?
    """
    root = _parser().parse(source).root_node
    meta = _revision_meta(root, source)

    # Two masks, by consumer: the RLS regex + DDL-intent checks work on text with comments,
    # strings, and function bodies blanked (no false positives — a 'CREATE POLICY' in a seed
    # string or function body is not DDL); the grammar parse keeps function bodies intact (it
    # needs them) and only blanks recovered RLS spans.
    masked = _blank_spans(source, _mask_spans(root, source))
    rls_symbols, rls_spans = _recover_rls(masked)

    cleaned = _blank_spans(source, rls_spans)
    grammar, error_spans = _grammar_symbols(_parser().parse(cleaned).root_node, cleaned)

    symbols: list[Symbol] = []
    if meta is not None:
        symbols.append(meta)
    symbols.extend(grammar)
    symbols.extend(rls_symbols)

    # Classify each parse error on the masked slice (comment/string/body-free), so only a
    # genuinely-unparsable DDL statement counts as catalog loss.
    ddl_errors = sum(1 for s, e in error_spans if _DDL_INTENT_RE.search(masked[s:e]))
    has_intent = bool(_DDL_INTENT_RE.search(masked))
    return symbols, ddl_errors, has_intent


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
            symbols, ddl_errors, has_intent = _scan_file(source)
            # Report only errors that actually lost DDL (E_SCHEMA_UNPARSED, a warning). A
            # non-schema parse error (a cron registration, a complex insert) is never a schema
            # loss — never cry wolf on it, even in a file that also carries real DDL.
            if ddl_errors:
                # Fail soft, report hard: the file's parseable statements still folded.
                symbols.append(Symbol("<unparsed>", "schema_error", 1, exported=False,
                                      metadata={"schema_op": "unparsed", "count": ddl_errors}))
            valid = [s for s in symbols if s.kind not in ("schema_error", "schema_meta")]
            if not valid:
                # No DDL extracted. Hard-fail ONLY when the file *meant* to carry schema: it
                # has DDL intent the grammar couldn't parse, or a revision/order header
                # declaring it a migration. A file with no DDL intent (a seed INSERT, a
                # grant/revoke, a cron registration — parseable or not) is legitimately
                # schema-empty: return it clean (empty symbols, no error) so it is never
                # mislabeled "unparsed". This is the cry-wolf fix.
                has_header = any(s.kind == "schema_meta" for s in symbols)
                if has_intent or has_header:
                    return make_result(rel_path, self.language_name, [], [], source,
                                       error="SQL parse error (no schema statement could be parsed)")
                return make_result(rel_path, self.language_name, [], [], source)
        except Exception as exc:
            return make_result(rel_path, self.language_name, [], [], source, error=str(exc))
        return make_result(rel_path, self.language_name, symbols, [], source)
