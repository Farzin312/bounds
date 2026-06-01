"""Tier-1 + Tier-2 ``describe`` assembly (extracted from cli.py).

``bounds describe`` merges the declared manifest (Tier 2, human YAML) with tree-sitter
facts (Tier 1, deterministic) so an agent can trust a subsystem's contract without reading
source. This module owns that merge; ``cli.py`` only wires arguments to it.

It deliberately reuses the shared owned-file walk (:func:`extract.scan.iter_subsystem_files`)
and single-file extractor (:func:`extract.scan.extract_file`) instead of carrying its own
copies, so describe and validate agree on which files a subsystem owns and how each is parsed.
Everything here is zero-LLM and deterministic: posix paths, sorted output.
"""

from __future__ import annotations

from pathlib import Path

from . import errors
from .extract import scan, supported_extensions
from .ignore import IgnoreMatcher
from .models import SubsystemCompact, ValidationReport
from .validate import engine as validate_engine
from .validate.schema import (
    _fold_subsystem_objects,
    hash_schema_catalog,
    schema_catalog,
    schema_diagnostics,
    schema_objects,
    schema_rls_posture,
)


def extract_owned(
    root: Path, sub: SubsystemCompact
) -> tuple[dict[str, str], list[str], list[str], list[dict], str, list[dict], list[dict], dict]:
    """Tier-1 extraction for one subsystem.

    Returns ``(exported_symbol_name -> owning_file, owned_files, unparsed_files, table_catalog,
    schema_hash, schema_objects, schema_diagnostics, rls_posture)``. Every file the
    subsystem owns is recorded in ``owned_files`` regardless of whether it parses (so ``files``
    reflects the declared surface); only cleanly-extracted exported symbols populate the symbol map
    used to mark ``exposes`` entries ``verified``. A supported owned file that fails to extract
    (unreadable / oversized / genuine parse error) is recorded in ``unparsed_files`` so describe
    reports it loudly instead of silently showing a real symbol as ``verified:false`` (fail-loud).
    A non-schema SQL file (seed/grant/cron with no DDL) extracts cleanly to *empty* symbols, so it
    is **not** counted here — the catalog-incompleteness signal lives in ``schema_diagnostics``,
    which reports only files that lost real DDL (``E_SCHEMA_UNPARSED``), never no-DDL files.

    File selection is the shared owned-file walk, so describe and validate own the same set.
    """
    exts = supported_extensions()
    extracted_symbols: dict[str, str] = {}  # symbol_name -> owning file (rel posix)
    extracts = {}
    file_owner = {}
    owned_files: list[str] = []
    unparsed_files: list[str] = []

    for abs_path in scan.iter_subsystem_files(root, sub, exts):
        rel = abs_path.relative_to(root).as_posix()
        if rel not in owned_files:
            owned_files.append(rel)
        result, _ = scan.extract_file(root, rel)
        if result is not None:
            extracts[rel] = result
            file_owner[rel] = sub.name
            for sym in result.symbols:
                if sym.exported:
                    extracted_symbols[sym.name] = rel
        else:  # supported source file that didn't yield a result → a genuine extraction failure
            unparsed_files.append(rel)

    catalog = schema_catalog(sub.name, extracts, file_owner)
    for table in catalog:
        files = table.get("files", [])
        if files:
            extracted_symbols[str(table["name"])] = str(files[0])
    schema_hash = hash_schema_catalog(catalog) if catalog else ""  # reuse the fold above; don't re-fold
    # Fold the non-table surface ONCE and feed both readers (objects + posture), instead of
    # each re-folding (which would also re-run order_migrations).
    objects_fold = _fold_subsystem_objects(sub.name, extracts, file_owner)
    objects = schema_objects(sub.name, extracts, file_owner, fold=objects_fold)
    diagnostics = [
        {"code": code, "message": message, "file": file}
        for code, message, file in schema_diagnostics(sub.name, extracts, file_owner)
    ]
    posture = (schema_rls_posture(sub.name, extracts, file_owner, catalog, fold=objects_fold)
               if catalog else {})
    return (extracted_symbols, owned_files, unparsed_files, catalog, schema_hash,
            objects, diagnostics, posture)


def describe_one(
    root: Path,
    sub: SubsystemCompact,
    deep: bool,
    report: ValidationReport | None,
    entry_matcher: IgnoreMatcher,
    full: bool = False,
) -> dict:
    """Build the merged Tier-1 + Tier-2 describe payload for a single subsystem.

    ``report`` is the one shared read-only quick run (:func:`status_report`); status is derived
    from it twice: ``validation_status`` is **scoped to this subsystem** (so describing a
    clean subsystem reads fresh even when drift lives elsewhere), and ``project_status`` is the
    project-wide rollup kept additively alongside it.

    Owned files matching a ``root.entry_points`` glob are surfaced: each is listed under
    ``entry_points`` and any ``exposes`` entry backed by one is flagged ``entry_point: true``,
    so an agent sees a symbol lives in a bootstrap file.
    """
    payload = sub.to_dict()
    # ``SubsystemCompact.to_dict`` carries the (rarely-used, manifest-declared) ``files`` list;
    # describe's ``files`` means the *owned* file roster, which historically overwrote it. We now
    # gate that roster behind --full, so drop the model's key to avoid a stale empty ``files: []``
    # sitting next to ``file_count``.
    payload.pop("files", None)
    (extracted_symbols, owned_files, unparsed_files, catalog, schema_hash,
     objects, diagnostics, posture) = extract_owned(root, sub)
    for expose in payload.get("exposes", []):
        ename = expose.get("name", "")
        if ename in extracted_symbols:
            expose["file"] = extracted_symbols[ename]
            expose["verified"] = True
            if expose.get("kind") == "table":
                table = next((t for t in catalog if t["name"] == ename), None)
                if table is not None:
                    expose["columns"] = table["columns"]
            if entry_matcher and entry_matcher.matches(expose["file"]):
                expose["entry_point"] = True
        else:
            expose["verified"] = False
    # The verified CONTRACT (exposes/tables/consumes) is always emitted in full — that is what
    # an agent reads instead of source. The two non-contract bulk fields are gated behind
    # ``--full`` so a default describe stays token-lean: the flat file roster (non-actionable —
    # symbols already carry their own file) collapses to a count, and the potentially-huge
    # schema-objects list (RPCs/views/indexes/policies, hundreds on a real schema) collapses to
    # per-kind counts. ``--full`` restores both. This keeps `describe` cheaper than reading
    # source even on a 289-migration subsystem, honoring the token-first thesis.
    payload["file_count"] = len(owned_files)
    if full:
        payload["files"] = sorted(owned_files)
    # Always present for a stable shape; the human renderer hides it when empty.
    payload["entry_points"] = sorted(
        f for f in owned_files if entry_matcher and entry_matcher.matches(f)
    )
    # Additive (only when non-empty): owned source files Bounds could not extract — surfaced so an
    # agent never mistakes an unreadable/oversized file for "symbol absent from source".
    if unparsed_files:
        payload["unparsed_files"] = sorted(unparsed_files)
    if catalog:
        payload["tables"] = catalog
        payload["schema_hash"] = schema_hash
    # The non-table schema surface (functions/RPCs, views, indexes, triggers, types, RLS
    # policies). Counts by kind by default so an agent sees the shape cheaply; the full list
    # (which can be hundreds of entries) is restored by ``--full``.
    if objects:
        counts: dict[str, int] = {}
        for obj in objects:
            counts[obj["kind"]] = counts.get(obj["kind"], 0) + 1
        payload["schema_object_counts"] = dict(sorted(counts.items()))
        if full:
            payload["schema_objects"] = objects
    # Derived RLS security posture over the schema's own tables: protected (RLS + ≥1 policy),
    # rls_without_policy (locked, often unintended), unprotected (no RLS — the open door).
    # Counts always (token-lean, and the unprotected/no-policy counts ARE the risk signal);
    # the table-name lists + per-table policy counts are restored by ``--full``, mirroring the
    # schema_object_counts → --full pattern so the default stays scannable.
    if posture:
        summary = {k: posture[k] for k in
                   ("tables", "rls_enabled", "protected", "rls_without_policy", "unprotected")}
        if full:
            summary["unprotected_tables"] = posture["unprotected_tables"]
            summary["rls_without_policy_tables"] = posture["rls_without_policy_tables"]
            summary["protected_tables"] = posture["protected_tables"]
            summary["policy_count"] = posture["policy_count"]
        payload["rls_posture"] = summary
    # Why the catalog may be incomplete: files that lost real DDL to a parse error
    # (E_SCHEMA_UNPARSED) or have no deterministic migration order (E_SCHEMA_NO_ORDER). This is
    # the actionable counterpart to unparsed_files — it names files that DID carry schema and
    # could not be fully extracted, never no-DDL seed/grant/cron files.
    if diagnostics:
        payload["schema_diagnostics"] = diagnostics
    payload["validation_status"] = subsystem_status(report, sub.name)
    payload["project_status"] = project_status(report)
    if deep:
        payload["semantic"] = {"note": "LLM enrichment (Tier 3) not enabled in this build"}
    return payload


def status_report(root: Path) -> ValidationReport | None:
    """The shared read-only quick run backing describe's status (or ``None`` when fatal).

    Issues carry quick-downgraded severities (errors→warnings); callers re-derive the original
    status via :func:`_derive_status`, which reads each code's canonical severity instead.
    """
    try:
        return validate_engine.run(root, mode="quick", persist=False)
    except errors.BoundsError:
        return None


def _derive_status(issues: list) -> str:
    """fresh | stale | unresolved from an issue list, using CANONICAL severities.

    Quick mode downgrades errors→warnings (so it never blocks), which would otherwise make a
    per-subsystem status never read 'stale'. We therefore key off the code's canonical severity
    (``errors.SEVERITY``) rather than the live one — except an issue whose *live* severity is
    ``info`` (e.g. an undeclared-export drift) is never error-class.
    """
    if any(i.code == errors.E_UNRESOLVED_REFERENCE for i in issues):
        return "unresolved"
    if any(i.severity != "info" and errors.SEVERITY.get(i.code) == "error" for i in issues):
        return "stale"
    return "fresh"


def project_status(report: ValidationReport | None) -> str:
    """Project-wide validation status re-derived from the shared quick run."""
    return _derive_status(report.issues) if report is not None else "unresolved"


def subsystem_status(report: ValidationReport | None, name: str) -> str:
    """Validation status scoped to one subsystem's own issues."""
    if report is None:
        return "unresolved"
    return _derive_status([i for i in report.issues if i.subsystem == name])
