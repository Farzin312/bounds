"""Opt-in, advisory raw-SQL-query table references (Phase 2).

This is the deliberately-fuzzy signal. It scans **string literals** in source for
``FROM``/``JOIN``/``INTO``/``UPDATE``/``DELETE FROM <table>`` and reports them as
*heuristic* table usages — useful for "who runs raw SQL against this table before I migrate
it?" but NEVER trustworthy enough to verify a boundary.

The moat guardrail is enforced structurally: nothing here ever feeds a validation check or an
``Issue``. Raw-query refs are surfaced only as an additive, clearly-labelled advisory block on
``bounds impact <table> --include-raw-queries``, so a string guess can never produce a blocking
``E_BOUNDARY_VIOLATION``. A wrong verified edge would falsify the "verified, not guessed"
positioning; a missing edge is fine — so this stays opt-in and out-of-band.

Only string literals are scanned (so a Python ``from x import y`` is not mistaken for a table
read), and only statically-knowable strings — an interpolated/templated query is skipped, not
guessed at.
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import tree_sitter as ts
    import tree_sitter_python as tspy
    import tree_sitter_typescript as tsts

    _OK = True
except Exception:  # pragma: no cover - only when a grammar wheel is missing
    _OK = False

# `FROM users`, `JOIN public.orders o`, `INTO users`, `UPDATE users`, `DELETE FROM users`.
# Group 1 = keyword, group 2 = (optionally schema-qualified) table identifier.
_SQL_REF = re.compile(
    r"\b(from|join|into|update|delete\s+from)\s+[\"'`\[]?([A-Za-z_][\w.]*)",
    re.IGNORECASE,
)
_WRITE_KEYWORDS = {"into", "update", "delete from"}

_LANGS: dict[str, object] = {}


def _language(name: str):
    if name not in _LANGS:
        if name == "python":
            _LANGS[name] = ts.Language(tspy.language())
        else:
            _LANGS[name] = ts.Language(tsts.language_typescript())
    return _LANGS[name]


def _string_texts(source: bytes, lang_name: str) -> list[str]:
    """Decoded contents of every static string literal in ``source`` (interpolated ones skipped)."""
    parser = ts.Parser(_language(lang_name))
    root = parser.parse(source).root_node
    out: list[str] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in ("string", "template_string"):
            # Skip strings carrying interpolation — a dynamic table name isn't knowable.
            if any(c.type in ("interpolation", "template_substitution") for c in node.named_children):
                continue
            out.append(source[node.start_byte : node.end_byte].decode("utf-8", "replace"))
            continue
        stack.extend(node.named_children)
    return out


def table_refs_in_source(source: bytes, lang_name: str) -> dict[str, set[str]]:
    """Map ``table -> {"reads"/"writes"}`` for raw-SQL refs found in ``source``'s strings."""
    refs: dict[str, set[str]] = {}
    for text in _string_texts(source, lang_name):
        for m in _SQL_REF.finditer(text):
            keyword = " ".join(m.group(1).lower().split())
            table = m.group(2).split(".")[-1].strip("\"'`[]")
            if not table:
                continue
            via = "writes" if keyword in _WRITE_KEYWORDS else "reads"
            refs.setdefault(table, set()).add(via)
    return refs


def scan_table_consumers(
    project_root: Path,
    file_owner: dict[str, str],
    table: str,
) -> list[dict]:
    """Advisory consumers of ``table`` via raw SQL strings: ``[{subsystem, via, files}]``.

    Best-effort and deterministic (sorted). Returns ``[]`` when grammars are unavailable or
    nothing matches. Never raises — a read error on one file just skips it.
    """
    if not _OK:
        return []
    by_subsystem: dict[str, dict[str, set[str]]] = {}  # subsystem -> {via -> files}
    for rel in sorted(file_owner):
        lang = "python" if rel.endswith(".py") else ("typescript" if _is_ts(rel) else None)
        if lang is None:
            continue
        try:
            source = (project_root / rel).read_bytes()
        except OSError:
            continue
        try:
            refs = table_refs_in_source(source, lang)
        except Exception:
            # Honour the "never raises" contract and the fail-soft invariant: a
            # tree-sitter parse failure on one advisory file is skipped, never
            # propagated. This block is an additive heuristic, not a validation check.
            continue
        vias = refs.get(table)
        if not vias:
            continue
        bucket = by_subsystem.setdefault(file_owner[rel], {})
        for via in vias:
            bucket.setdefault(via, set()).add(rel)
    out: list[dict] = []
    for subsystem in sorted(by_subsystem):
        for via in sorted(by_subsystem[subsystem]):
            out.append({
                "subsystem": subsystem,
                "via": via,
                "files": sorted(by_subsystem[subsystem][via]),
                "confidence": "heuristic",
            })
    return out


def _is_ts(rel: str) -> bool:
    return any(rel.endswith(ext) for ext in (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"))
