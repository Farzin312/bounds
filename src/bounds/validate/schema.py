"""Subsystem-level SQL schema fold.

SQL files extract per-file DDL statements for cacheability; this module applies those
statements in deterministic migration order and returns the current table catalog (the
schema's *actual exposed surface*). Determinism is load-bearing: ordering uses the filename
prefix or an embedded ``revision``/``down_revision`` chain — never file mtime.

The fold is the single authority on which tables/columns a schema subsystem currently
exposes. ``checks`` and ``describe`` call it; both memoize per subsystem so a validate run
folds each schema at most once.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from .. import errors
from ..models import ExtractResult

# Languages whose extracted symbols carry DDL ``schema_op`` metadata and therefore fold into a
# table catalog: raw SQL migrations and Prisma ``model`` blocks (an external ``schema.sql``
# snapshot is just a .sql file, so it rides the SQL path with no special-casing).
SCHEMA_LANGUAGES = frozenset({"sql", "prisma"})

# Order-sensitive ops: if a subsystem has several unordered migrations carrying any of
# these, the fold result depends on order and we should warn (E_SCHEMA_NO_ORDER). Includes the
# non-table lifecycle ops (a DROP/ALTER POLICY or a DISABLE/NO FORCE RLS) now that policies and
# RLS fold — a lone CREATE/ENABLE is order-independent (like CREATE TABLE) and is excluded.
_ORDER_SENSITIVE = {
    "add_column", "drop_column", "drop_table", "rename_table", "rename_column",
    "drop_policy", "alter_policy", "disable_rls", "no_force_rls",
}


@dataclass
class _TableState:
    name: str
    columns: set[str] = field(default_factory=set)
    files: set[str] = field(default_factory=set)

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": "table", "columns": sorted(self.columns), "files": sorted(self.files)}


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------
def _schema_files(subsystem: str, extracts: dict[str, ExtractResult], file_owner: dict[str, str]) -> list[str]:
    return [
        rel
        for rel, result in extracts.items()
        if file_owner.get(rel) == subsystem and result.language in SCHEMA_LANGUAGES
    ]


def _file_meta(result: ExtractResult) -> dict:
    """Revision-chain / explicit-order hints captured from a migration's header comments."""
    for sym in result.symbols:
        if (sym.metadata or {}).get("schema_op") == "meta":
            return dict(sym.metadata)
    return {}


def _filename_key(rel: str) -> tuple[int, str, str]:
    """Numeric/timestamp filename prefix first (zero-padded), else lexical by name."""
    name = rel.rsplit("/", 1)[-1]
    match = re.match(r"^(\d+)", name)
    if match:
        return (0, match.group(1).zfill(32), rel)
    return (1, name, rel)


def _revision_chain(files: list[str], extracts: dict[str, ExtractResult]) -> list[str] | None:
    """Topologically order ``files`` by their ``revision``/``down_revision`` chain.

    Returns the ordered list for a clean single linear chain that covers every revisioned
    file, or ``None`` when there is no usable total order (no headers, branch, cycle, or
    multiple heads) — the caller then falls back to filename order and warns.
    """
    rev_to_file: dict[str, str] = {}
    down: dict[str, str | None] = {}
    revisioned: list[str] = []
    for rel in files:
        meta = _file_meta(extracts[rel])
        rev = meta.get("revision")
        if rev is None:
            continue
        if str(rev) in rev_to_file:  # duplicate revision id → ambiguous
            return None
        rev_to_file[str(rev)] = rel
        down[str(rev)] = str(meta["down_revision"]) if meta.get("down_revision") is not None else None
        revisioned.append(rel)
    if len(revisioned) < 2 or len(revisioned) != len(files):
        return None  # not a fully-revisioned set; use filename ordering instead

    heads = [r for r, d in down.items() if d is None or d not in rev_to_file]
    if len(heads) != 1:
        return None  # zero or multiple roots → not a single linear chain
    ordered: list[str] = []
    seen: set[str] = set()
    children: dict[str | None, list[str]] = {}
    for rev, d in down.items():
        children.setdefault(d, []).append(rev)
    rev: str | None = heads[0]
    while rev is not None:
        if rev in seen:  # cycle guard
            return None
        seen.add(rev)
        ordered.append(rev_to_file[rev])
        nxt = children.get(rev, [])
        if len(nxt) > 1:
            return None  # branch → no single order
        rev = nxt[0] if nxt else None
    if len(ordered) != len(revisioned):
        return None
    return ordered


def order_migrations(
    subsystem: str, extracts: dict[str, ExtractResult], file_owner: dict[str, str]
) -> tuple[list[str], bool]:
    """Return ``(ordered_files, no_total_order)`` for a schema subsystem's migrations.

    Order priority: explicit ``-- bounds:order N`` → ``revision``/``down_revision`` chain →
    filename numeric/timestamp prefix → lexical filename. ``no_total_order`` is True only
    when order actually matters (≥2 files, order-sensitive ops) yet no deterministic basis
    beyond raw lexical filename exists — surfaced as the advisory E_SCHEMA_NO_ORDER.
    """
    files = _schema_files(subsystem, extracts, file_owner)
    if len(files) <= 1:
        return sorted(files, key=_filename_key), False

    explicit = {rel: _file_meta(extracts[rel]).get("order") for rel in files}
    if any(v is not None for v in explicit.values()):
        ordered = sorted(files, key=lambda r: (explicit[r] is None, explicit[r] or 0, _filename_key(r)))
        return ordered, False

    chain = _revision_chain(files, extracts)
    if chain is not None:
        return chain, False

    ordered = sorted(files, key=_filename_key)
    # Warn only when ordering is genuinely undetermined: no file has a numeric prefix AND
    # the set contains order-sensitive ops whose outcome depends on the chosen order.
    has_prefix = any(re.match(r"^\d+", rel.rsplit("/", 1)[-1]) for rel in files)
    order_sensitive = any(
        (sym.metadata or {}).get("schema_op") in _ORDER_SENSITIVE
        for rel in files
        for sym in extracts[rel].symbols
    )
    return ordered, (not has_prefix and order_sensitive)


# ---------------------------------------------------------------------------
# Fold
# ---------------------------------------------------------------------------
def _fold_subsystem_schema(
    subsystem: str, extracts: dict[str, ExtractResult], file_owner: dict[str, str]
) -> dict[str, _TableState]:
    """Fold SQL DDL symbols owned by ``subsystem`` into surviving table state."""
    tables: dict[str, _TableState] = {}
    ordered, _ = order_migrations(subsystem, extracts, file_owner)
    for rel in ordered:
        for sym in extracts[rel].symbols:
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


# Non-table DDL objects surfaced alongside the table catalog. Two classes, by lifecycle:
#   * "dedup" kinds (functions/RPCs, views, indexes, triggers, types) — present if ever
#     created; deduped by (kind, name). DROP of these is rare and not tracked.
#   * "folded" kinds (policies, RLS toggles) — an ordered create/alter/drop fold, exactly like
#     tables, so a `DROP POLICY` (or `DISABLE ROW LEVEL SECURITY`) nets out and only the
#     *currently live* surface is reported (the same drift-proof contract the table fold gives).
_DEDUP_KINDS = ("function", "view", "index", "trigger", "type")


@dataclass
class _ObjectsFold:
    """Live non-table schema surface after the ordered fold."""
    dedup: dict[tuple[str, str], dict]  # (kind, name) -> {name, kind, [table], files:set}
    policies: dict[tuple[str, str], dict]  # (table, policy) -> {table, policy, present, files:set}
    rls: dict[str, dict]  # table -> {table, enabled, files:set}


def _fold_policy(policies: dict[tuple[str, str], dict], sym, rel: str) -> None:
    """Apply one policy op (create/alter/drop) to the running policy fold."""
    meta = sym.metadata or {}
    table, policy = str(meta.get("table") or ""), str(meta.get("policy") or "")
    entry = policies.setdefault((table, policy),
                                {"table": table, "policy": policy, "present": False, "files": set()})
    entry["files"].add(rel)
    op = meta.get("schema_op")
    if op == "drop_policy":
        entry["present"] = False
    elif op == "alter_policy":
        target = str(meta.get("to") or "").strip()
        if target:  # ALTER POLICY ... RENAME TO: move presence to the new name
            moved = policies.setdefault((table, target),
                                        {"table": table, "policy": target, "present": False, "files": set()})
            moved["files"].update(entry["files"])
            moved["present"] = entry["present"]
            entry["present"] = False
        # a non-rename ALTER POLICY leaves presence unchanged
    else:  # create_policy
        entry["present"] = True


def fold_subsystem_objects(
    subsystem: str, extracts: dict[str, ExtractResult], file_owner: dict[str, str]
) -> _ObjectsFold:
    """Fold non-table DDL into the live surface, in deterministic migration + textual order.

    Policies and RLS toggles fold (create/alter/drop, enable/disable/force) so a dropped
    policy or disabled table can't linger; functions/views/indexes/triggers/types dedup.
    Reuses :func:`order_migrations` so objects fold in the same order tables do.
    """
    dedup: dict[tuple[str, str], dict] = {}
    policies: dict[tuple[str, str], dict] = {}
    rls: dict[str, dict] = {}
    ordered, _ = order_migrations(subsystem, extracts, file_owner)
    for rel in ordered:
        for sym in extracts[rel].symbols:
            meta = sym.metadata or {}
            if sym.kind in _DEDUP_KINDS:
                entry = dedup.setdefault((sym.kind, sym.name),
                                         {"name": sym.name, "kind": sym.kind, "files": set()})
                entry["files"].add(rel)
                if meta.get("table"):
                    entry["table"] = meta["table"]
            elif sym.kind == "policy":
                _fold_policy(policies, sym, rel)
            elif sym.kind == "rls":
                entry = rls.setdefault(sym.name, {"table": sym.name, "enabled": False, "files": set()})
                entry["files"].add(rel)
                # RLS is enabled by ENABLE/FORCE and stays enabled under NO FORCE (which only
                # clears the owner-bypass exemption); only DISABLE turns it off.
                entry["enabled"] = meta.get("schema_op") != "disable_rls"
    return _ObjectsFold(dedup, policies, rls)


def schema_objects(
    subsystem: str,
    extracts: dict[str, ExtractResult],
    file_owner: dict[str, str],
    fold: _ObjectsFold | None = None,
) -> list[dict]:
    """The live non-table schema surface owned by ``subsystem``, sorted by (kind, name).

    Deterministic and byte-stable: the fold walks files in deterministic migration order and
    the output is sorted. Policies/RLS reflect the *current* surface (dropped/disabled ones
    are gone); functions/views/indexes/triggers/types are deduped. Each entry carries
    ``{name, kind, [table], files}``. A caller that already holds the fold (describe) passes it
    in to avoid re-folding.
    """
    if fold is None:
        fold = fold_subsystem_objects(subsystem, extracts, file_owner)
    merged: dict[tuple[str, str], dict] = dict(fold.dedup)
    for entry in fold.policies.values():
        if entry["present"]:
            name = f"{entry['table']}.{entry['policy']}"
            merged[("policy", name)] = {"name": name, "kind": "policy",
                                        "table": entry["table"], "files": entry["files"]}
    for entry in fold.rls.values():
        if entry["enabled"]:
            merged[("rls", entry["table"])] = {"name": entry["table"], "kind": "rls",
                                               "table": entry["table"], "files": entry["files"]}
    out: list[dict] = []
    for key in sorted(merged):
        entry = merged[key]
        record = {"name": entry["name"], "kind": entry["kind"]}
        if "table" in entry:
            record["table"] = entry["table"]
        record["files"] = sorted(entry["files"])
        out.append(record)
    return out


def schema_rls_posture(
    subsystem: str,
    extracts: dict[str, ExtractResult],
    file_owner: dict[str, str],
    catalog: list[dict] | None = None,
    fold: _ObjectsFold | None = None,
) -> dict:
    """Derived row-level-security posture over a schema's own (catalog) tables.

    Answers the highest-value security question deterministically from the fold: which tables
    have RLS enabled with at least one live policy (``protected``), which have RLS on but no
    policy (``rls_without_policy`` — effectively locked, often unintended), and which exposed
    tables have no RLS at all (``unprotected`` — the open door). The universe is the live table
    catalog (the schema's own tables); RLS toggles on tables outside it (e.g. ``storage.objects``)
    are not counted as the schema's exposure. ``catalog`` and ``fold`` are passed in to reuse the
    already-built table fold + objects fold (describe holds both).
    """
    if fold is None:
        fold = fold_subsystem_objects(subsystem, extracts, file_owner)
    # Posture is a Postgres-RLS concept. A schema that declares no RLS and no policies (a
    # Prisma schema, or plain SQL that never uses row-level security) has no posture to report
    # — emitting "every table unprotected" there would be a false alarm, so return nothing.
    if not fold.rls and not fold.policies:
        return {}
    if catalog is None:
        catalog = schema_catalog(subsystem, extracts, file_owner)
    catalog_tables = {str(t["name"]) for t in catalog}
    enabled = {t for t, e in fold.rls.items() if e["enabled"]}
    policy_count: dict[str, int] = {}
    for entry in fold.policies.values():
        if entry["present"]:
            policy_count[entry["table"]] = policy_count.get(entry["table"], 0) + 1

    protected = sorted(t for t in catalog_tables if t in enabled and policy_count.get(t, 0) > 0)
    rls_without_policy = sorted(t for t in catalog_tables if t in enabled and policy_count.get(t, 0) == 0)
    unprotected = sorted(t for t in catalog_tables if t not in enabled)
    return {
        "tables": len(catalog_tables),
        "rls_enabled": len(catalog_tables & enabled),
        "protected": len(protected),
        "rls_without_policy": len(rls_without_policy),
        "unprotected": len(unprotected),
        # name lists + per-table policy counts are gated behind --full by the caller.
        "protected_tables": protected,
        "rls_without_policy_tables": rls_without_policy,
        "unprotected_tables": unprotected,
        "policy_count": {t: policy_count[t] for t in sorted(policy_count) if t in catalog_tables},
    }


def hash_schema_catalog(catalog: list[dict]) -> str:
    """Deterministic sha256 over an already-built table catalog (single home for the hash).

    Insensitive to migration *count* and to non-structural fields — only ``{name, columns}``
    feed the digest, so two migration sets that fold to the same surviving surface hash
    identically. A caller that already holds a catalog (``describe``) passes it here instead
    of re-folding via :func:`schema_structure_hash`.
    """
    surface = [{"name": t["name"], "columns": t["columns"]} for t in catalog]
    blob = json.dumps(surface, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def schema_structure_hash(subsystem: str, extracts: dict[str, ExtractResult], file_owner: dict[str, str]) -> str:
    """Fold ``subsystem``'s migrations and hash the resulting table surface (standalone path).

    The schema subsystem's structure hash, surfaced in ``describe`` so an agent (or a freshness
    gate) can detect a schema-surface change without re-reading DDL. Callers that already hold
    the catalog should call :func:`hash_schema_catalog` directly to avoid a second fold.
    """
    return hash_schema_catalog(schema_catalog(subsystem, extracts, file_owner))


def schema_diagnostics(
    subsystem: str, extracts: dict[str, ExtractResult], file_owner: dict[str, str]
) -> list[tuple[str, str, str | None]]:
    """``(code, message, file)`` advisories for a schema subsystem (both warnings, never block).

    ``E_SCHEMA_UNPARSED`` — a migration had a statement tree-sitter couldn't parse (the rest
    of the file still folded). ``E_SCHEMA_NO_ORDER`` — order matters but no deterministic
    basis (filename prefix / revision chain / explicit order) was available.
    """
    out: list[tuple[str, str, str | None]] = []
    for rel in sorted(_schema_files(subsystem, extracts, file_owner)):
        for sym in extracts[rel].symbols:
            if (sym.metadata or {}).get("schema_op") == "unparsed":
                count = (sym.metadata or {}).get("count", 1)
                out.append((
                    errors.E_SCHEMA_UNPARSED,
                    f"{count} unparsable SQL statement(s) skipped in '{rel}' "
                    "(the file's parseable statements still folded into the schema)",
                    rel,
                ))
    _, no_order = order_migrations(subsystem, extracts, file_owner)
    if no_order:
        out.append((
            errors.E_SCHEMA_NO_ORDER,
            f"schema subsystem '{subsystem}' has order-dependent migrations with no "
            "deterministic order (no numeric filename prefix, revision chain, or "
            "'-- bounds:order N' header); folded in lexical filename order",
            None,
        ))
    return out
