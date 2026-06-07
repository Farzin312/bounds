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

from collections import Counter
from pathlib import Path

from ..shared import errors, gitutil
from ..shared.ignore import IgnoreMatcher, load_matcher
from ..shared.models import Issue, RootManifest, SubsystemCompact, ValidationReport
from .extract import scan, supported_extensions
from .extract.scan import coverage_has_gap
from .validate.checks import CheckContext, check_cycles
from .manifest import loader as manifest_loader
from .validate import engine as validate_engine
from .validate.schema import (
    fold_subsystem_objects,
    hash_schema_catalog,
    schema_catalog,
    schema_diagnostics,
    schema_objects,
    schema_rls_posture,
)


def _ignore_ctx(root: Path) -> tuple[dict[str, SubsystemCompact], IgnoreMatcher, Path]:
    """The ignore-aware ``(subsystems, .boundsignore matcher, git repo root)`` describe shares with
    validate, so describe's file/doc/test selection matches exactly what ``validate`` scans.

    Loading the manifest set is genuinely fatal if it's missing/broken — it **propagates** rather than
    degrading to a single-subsystem view, which would silently re-introduce the cross-subsystem
    double-counting BOUNDS-006 fixed (and ``describe`` is only reached after the root already loaded).
    """
    _root, subs, _issues = manifest_loader.load_all(root)
    return subs, load_matcher(root), gitutil.repo_root(root) or root


def _owned_files(root: Path, sub: SubsystemCompact) -> list[str]:
    """Repo-relative posix paths this subsystem owns under most-specific-path-wins (BOUNDS-006).

    Reuses :func:`scan.resolve_owners` — the single project-wide ownership home shared with the
    validation engine — **ignore-aware** (``.boundsignore`` + ``.gitignore``), and filters to files
    whose winning owner is ``sub``, instead of walking the subsystem's own paths blindly (which
    double-counted any file a more-specific sibling owns AND counted files validate would skip). This
    makes describe's ``file_count``/``files`` AGREE with validate on exactly which files belong here.
    """
    subs, matcher, repo = _ignore_ctx(root)
    owners = scan.resolve_owners(root, subs, supported_extensions(), matcher, repo)
    return sorted(rel for rel, (owner, _abs) in owners.items() if owner == sub.name)


def _linked_docs_tests(root: Path, sub: SubsystemCompact) -> tuple[list[str], list[str]]:
    """The doc/test files (rel-posix, sorted) this subsystem links — explicit ``docs:``/``tests:``
    plus convention auto-detection (BOUNDS-010 hybrid model).

    Resolves against the full project's subsystems (most-specific declaration wins, same as validate),
    **ignore-aware** (``.boundsignore`` + ``.gitignore`` via the shared matcher/repo), so describe and
    validate/coverage agree on which docs/tests belong here. Returns ``(docs, tests)``.
    """
    subs, matcher, repo = _ignore_ctx(root)
    doc_owners = scan.resolve_doc_owners(root, subs, matcher, repo)
    test_owners = scan.resolve_test_owners(root, subs, matcher, repo)
    docs = sorted(rel for rel, owner in doc_owners.items() if owner == sub.name)
    tests = sorted(rel for rel, owner in test_owners.items() if owner == sub.name)
    return docs, tests


def subsystem_overlaps(root: Path, sub: SubsystemCompact) -> list[Issue]:
    """Genuine same-path conflicts touching ``sub``: files another subsystem declares identically.

    A true ambiguity is two subsystems declaring the *identical* path/file for a source file at
    EQUAL specificity, where :func:`scan.resolve_owners` can only break the tie by sorted-first
    subsystem name (so ownership rides on alphabetical luck, not intent). For each such file this
    emits one non-fatal ``E_SUBSYSTEM_OVERLAP`` :class:`Issue` (warning) naming the colliding
    subsystems and the file, so an agent can narrow a path or move it to ``files:`` rather than
    depend on the tie-break. Nested paths (different specificity) are NOT overlaps — the deepest path
    legitimately wins (BOUNDS-001), so they never surface here. Sorted for deterministic output.
    """
    try:
        subs, matcher, repo = _ignore_ctx(root)
    except errors.BoundsError:
        return []
    return scan._ownership_overlap_issues(
        root,
        subs,
        supported_extensions(),
        matcher,
        repo,
        subsystem=sub.name,
        aggregate=False,
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

    File selection is most-specific-path-wins ownership (:func:`scan.resolve_owners` via
    :func:`_owned_files`), the SAME map validate uses, so describe and validate own the identical
    set — a file a more-specific sibling subsystem owns is no longer double-counted here (BOUNDS-006).
    """
    extracted_symbols: dict[str, str] = {}  # symbol_name -> owning file (rel posix)
    extracts = {}
    file_owner = {}
    owned_files: list[str] = _owned_files(root, sub)
    unparsed_files: list[str] = []

    for rel in owned_files:
        result, _ = scan.extract_file(root, rel)
        if result is None:  # supported source file that didn't yield a result → genuine failure
            unparsed_files.append(rel)
            continue
        extracts[rel] = result
        file_owner[rel] = sub.name
        extracted_symbols.update((s.name, rel) for s in result.symbols if s.exported)

    catalog = schema_catalog(sub.name, extracts, file_owner)
    for table in catalog:
        files = table.get("files", [])
        if files:
            extracted_symbols[str(table["name"])] = str(files[0])
    schema_hash = hash_schema_catalog(catalog) if catalog else ""  # reuse the fold above; don't re-fold
    # Fold the non-table surface ONCE and feed both readers (objects + posture), instead of
    # each re-folding (which would also re-run order_migrations).
    objects_fold = fold_subsystem_objects(sub.name, extracts, file_owner)
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
    # docs/tests are surfaced as the RESOLVED set (explicit + convention) under --full, mirroring
    # the file roster — drop the model's raw declared lists so they're never shown un-gated and a
    # default describe stays token-lean (BOUNDS-010 parity).
    payload.pop("docs", None)
    payload.pop("tests", None)
    (extracted_symbols, owned_files, unparsed_files, catalog, schema_hash,
     objects, diagnostics, posture) = extract_owned(root, sub)
    for expose in payload.get("exposes", []):
        _verify_expose(expose, extracted_symbols, catalog, entry_matcher)
    # The verified CONTRACT (exposes/tables/consumes) is always emitted in full — that is what
    # an agent reads instead of source. The two non-contract bulk fields are gated behind
    # ``--full`` so a default describe stays token-lean: the flat file roster (non-actionable —
    # symbols already carry their own file) collapses to a count, and the potentially-huge
    # schema-objects list (RPCs/views/indexes/policies, hundreds on a real schema) collapses to
    # per-kind counts. ``--full`` restores both. This keeps `describe` cheaper than reading
    # source even on a 289-migration subsystem, honoring the token-first thesis.
    payload["file_count"] = len(owned_files)
    # JSON-first parity (BOUNDS-010): the human renderer (output._render_subsystem_human) lists the
    # file roster exactly when ``payload["files"]`` is present and non-empty, so the JSON MUST carry
    # the same ``files`` under the same gate — never render a list the JSON omitted. ``--full`` is
    # that gate for both: default stays token-lean (count only), ``--full`` populates the roster in
    # the JSON and the human view alike. owned_files is already sorted by _owned_files.
    if full:
        payload["files"] = owned_files
        # Linked docs/tests (explicit `docs:`/`tests:` + convention auto-detection) — gated under
        # --full like the file roster for JSON/human parity (BOUNDS-010): a default describe stays
        # token-lean, --full surfaces exactly which docs/tests cover this subsystem in BOTH views.
        # Resolved here (one tree walk) only when --full is set, so the default path stays cheap.
        docs, tests = _linked_docs_tests(root, sub)
        if docs:
            payload["docs"] = docs
        if tests:
            payload["tests"] = tests
    # Always present for a stable shape; the human renderer hides it when empty.
    payload["entry_points"] = sorted(
        f for f in owned_files if entry_matcher and entry_matcher.matches(f)
    )
    # Additive (only when non-empty): owned source files Bounds could not extract — surfaced so an
    # agent never mistakes an unreadable/oversized file for "symbol absent from source".
    if unparsed_files:
        payload["unparsed_files"] = sorted(unparsed_files)
    # Additive (only when non-empty), and gated behind --full like the file/doc/test roster: a genuine
    # same-path ownership ambiguity touching this subsystem — two manifests declaring the identical
    # path/file at equal specificity, where the winner rides only on sorted-first name (BOUNDS-006).
    # Non-fatal warning Issues so an agent can disambiguate; the file is still deterministically owned.
    # --full-gated because the check walks every subsystem's files: the default describe stays token-
    # AND compute-lean (the contract is what the default returns; diagnostics live under --full).
    if full:
        overlaps = subsystem_overlaps(root, sub)
        if overlaps:
            payload["overlaps"] = [i.to_dict() for i in overlaps]
    _attach_schema_payload(payload, catalog, schema_hash, objects, posture, diagnostics,
                           unparsed_files, full)
    payload["validation_status"] = subsystem_status(report, sub.name)
    payload["project_status"] = project_status(report)
    if deep:
        payload["semantic"] = {"note": "LLM enrichment (Tier 3) not enabled in this build"}
    return payload


def _verify_expose(
    expose: dict, extracted_symbols: dict[str, str], catalog: list[dict], entry_matcher: IgnoreMatcher
) -> None:
    """Annotate one declared ``exposes`` entry with tree-sitter facts (verified/file/columns), in place."""
    ename = expose.get("name", "")
    if ename not in extracted_symbols:
        expose["verified"] = False
        return
    expose["file"] = extracted_symbols[ename]
    expose["verified"] = True
    if expose.get("kind") == "table":
        table = next((t for t in catalog if t["name"] == ename), None)
        if table is not None:
            expose["columns"] = table["columns"]
    if entry_matcher and entry_matcher.matches(expose["file"]):
        expose["entry_point"] = True


def _attach_schema_payload(
    payload: dict,
    catalog: list[dict],
    schema_hash: str,
    objects: list[dict],
    posture: dict,
    diagnostics: list[dict],
    unparsed_files: list[str],
    full: bool,
) -> None:
    """Add the (token-lean) schema fields to a describe payload, in place.

    Token-first by default: the table contract is always full, but the (often hundreds-long)
    schema-object list, the RLS posture table-name lists, and the per-file diagnostics collapse
    to counts unless ``full``. The always-present ``schema_coverage`` flag tells an AI whether
    absence is authoritative (see :func:`_schema_coverage`).
    """
    if catalog:
        payload["tables"] = catalog
        payload["schema_hash"] = schema_hash
        payload["schema_coverage"] = _schema_coverage(diagnostics, unparsed_files)
    if objects:
        counts: dict[str, int] = {}
        for obj in objects:
            counts[obj["kind"]] = counts.get(obj["kind"], 0) + 1
        payload["schema_object_counts"] = dict(sorted(counts.items()))
        if full:
            payload["schema_objects"] = objects
    if posture:
        summary = {k: posture[k] for k in
                   ("tables", "rls_enabled", "protected", "rls_without_policy", "unprotected")}
        if full:
            for k in ("unprotected_tables", "rls_without_policy_tables", "protected_tables", "policy_count"):
                summary[k] = posture[k]
        payload["rls_posture"] = summary
    # The actionable per-file detail behind schema_coverage — names files that carried schema yet
    # could not be fully extracted (E_SCHEMA_UNPARSED / E_SCHEMA_NO_ORDER), never no-DDL files.
    # Gated behind --full: by default schema_coverage carries the count + the trust note, so the
    # token-lean default stays small while still telling an AI the catalog may be incomplete.
    if diagnostics and full:
        payload["schema_diagnostics"] = diagnostics


def _schema_coverage(diagnostics: list[dict], unparsed_files: list[str]) -> dict:
    """Trust signal: is this schema catalog COMPLETE, or might a table/policy be missing?

    An AI reads the catalog as authoritative ("these are the tables"). When some owned DDL
    couldn't be parsed, absence is NOT authoritative — so we say so explicitly and compactly,
    rather than letting a silent gap mislead. ``complete: true`` is the positive case: every
    owned file extracted, so a table/policy *not* listed genuinely isn't in the schema.
    """
    lost = sorted(set(unparsed_files) | {d["file"] for d in diagnostics
                                         if d.get("code") == errors.E_SCHEMA_UNPARSED and d.get("file")})
    if not lost:
        return {"complete": True}
    return {
        "complete": False,
        "unextracted_files": len(lost),
        "note": ("Some owned files contain DDL Bounds could not fully parse, so this catalog "
                 "may be incomplete: a table, column, or policy not listed here may still exist. "
                 "Do not treat absence as authoritative. Re-run with --full for the file list "
                 "(schema_diagnostics)."),
    }


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
    # Errors outrank warning-level unresolved references: when both apply the actionable "stale"
    # (drift to fix) must win over the benign "unresolved" (forward refs, fine mid-adoption), or a
    # consumer branching on status does nothing about real drift. Mirrors engine._status.
    if any(i.severity != "info" and errors.SEVERITY.get(i.code) == "error" for i in issues):
        return "stale"
    if any(i.code == errors.E_UNRESOLVED_REFERENCE for i in issues):
        return "unresolved"
    return "fresh"


def project_status(report: ValidationReport | None) -> str:
    """Project-wide validation status re-derived from the shared quick run."""
    return _derive_status(report.issues) if report is not None else "unresolved"


def subsystem_status(report: ValidationReport | None, name: str) -> str:
    """Validation status scoped to one subsystem's own issues."""
    if report is None:
        return "unresolved"
    return _derive_status([i for i in report.issues if i.subsystem == name])


def run_list(subs: dict, rootm: RootManifest, namespace: str | None = None) -> dict:
    """Return the subsystem map for the project. Read-only."""
    entries: list[dict] = []
    for n in sorted(subs):
        sub = subs[n]
        if namespace is not None and sub.namespace != namespace:
            continue
        entry: dict = {
            "name": sub.name,
            "role": sub.role,
            "criticality": sub.criticality,
        }
        if sub.namespace:
            entry["namespace"] = sub.namespace
        entry.update({
            "description": sub.description,
            "exposes": len(sub.exposes),
            "consumes": len(sub.consumes),
            "consumed_by": sorted(sub.consumed_by),
        })
        entries.append(entry)
    return {"mode": "list", "project": rootm.project, "subsystems": entries}


def run_describe(subs: dict, name: str, root: Path, rootm: RootManifest, full: bool = False, deep: bool = False) -> dict | None:
    """Describe one subsystem. Read-only."""
    if name not in subs:
        return None
    report = status_report(root)
    entry_matcher = IgnoreMatcher(rootm.entry_points)
    return describe_one(root, subs[name], deep=deep, report=report, 
                        entry_matcher=entry_matcher, full=full)


def run_describe_namespace(subs: dict, namespace: str, root: Path, rootm: RootManifest, full: bool = False, deep: bool = False) -> dict | None:
    """Describe all subsystems in a namespace. Read-only."""
    targets = [s for s in subs.values() if s.namespace == namespace]
    if not targets:
        return None
    report = status_report(root)
    entry_matcher = IgnoreMatcher(rootm.entry_points)
    payloads = [describe_one(root, s, deep=deep, report=report, 
                             entry_matcher=entry_matcher, full=full)
                for s in sorted(targets, key=lambda x: x.name)]
    return {
        "mode": "describe",
        "namespace": namespace,
        "subsystems": payloads,
    }


def run_overview(root: Path, rootm: RootManifest, subs: dict, schema_issues: list) -> dict:
    """Return project health, coverage, and trust signals. Read-only."""
    roles = Counter(s.role for s in subs.values())
    criticality = Counter(s.criticality for s in subs.values())
    edges = [
        {"from": n, "to": c.subsystem, "interfaces": sorted(c.interfaces)}
        for n in sorted(subs)
        for c in subs[n].consumes
    ]
    edges.sort(key=lambda e: (e["from"], e["to"], e["interfaces"]))
    
    ctx = CheckContext(root, rootm, subs, {}, {}, set(), set())
    cycle_issues = check_cycles(ctx)
    schema_errors = sum(1 for i in schema_issues if i.severity == "error")

    # Fold a real validation pass into health (BOUNDS-009).
    report = validate_engine.run(root, mode="full", persist=False)
    
    counts: Counter = Counter()
    for i in report.issues:
        counts[i.code] += i.count
        
    cov = report.stats.get("coverage", {})
    mapping = cov.get("mapping") or {}
    validation_errors = report.errors()
    
    validation = {
        "ok": not validation_errors and not schema_errors,
        "errors": len(validation_errors) + schema_errors,
        "warnings": len(report.warnings()),
        "structural_drift": counts.get(errors.E_STRUCTURAL_DRIFT, 0),
        "boundary_violations": counts.get(errors.E_BOUNDARY_VIOLATION, 0),
        "ownership_overlaps": counts.get(errors.E_SUBSYSTEM_OVERLAP, 0),
        "contract_gaps": counts.get(errors.E_CONTRACT_MISSING_EXPORT, 0),
        "stale_interfaces": counts.get(errors.E_STALE_INTERFACE, 0),
        "mapped_pct": mapping.get("mapped_pct", 0.0),
    }
    
    described_n = sum(1 for s in subs.values() if (s.description or "").strip())
    total_subs = len(subs)
    validation["described"] = {
        "with_description": described_n,
        "total": total_subs,
        "pct": round(100.0 * described_n / total_subs, 1) if total_subs else 100.0,
    }

    next_steps = list(report.next_steps)
    if total_subs and described_n * 2 < total_subs:
        missing = total_subs - described_n
        next_steps.append(
            f"Add subsystem descriptions ({missing} of {total_subs} are empty). Without prose, "
            "concept lookups can't map a concept to a subsystem."
        )
    if cycle_issues:
        next_steps.append("Break dependency cycles before treating impact results as complete.")
    if schema_errors:
        next_steps.append("Fix schema manifest errors before trusting schema/table answers.")
    if validation.get("ownership_overlaps"):
        next_steps.append(
            "Resolve duplicate ownership: run `bounds validate -H` for the overlapping "
            "subsystems and narrow their `paths:` so ownership is deterministic."
        )
    if not next_steps:
        next_steps.append(
            "Use `bounds list` → `bounds describe <name>` → `bounds impact <name>` to scope "
            "changes, then `bounds validate --quick` after edits."
        )
    
    validation["trust_note"] = (
        "Bounds is authoritative for tree-sitter-verified symbols in mapped source. "
        "If mapped_pct is below 100, unmapped library source is outside the architecture map; "
        "use Bounds to scope first, then inspect source where the map is incomplete."
    )
    validation["next_steps"] = next_steps

    # Informational doc/test linkage (JSON-first parity).
    for label in ("tests", "docs"):
        bucket = mapping.get(label) or {}
        if bucket.get("total"):
            validation[label] = {"linked": bucket.get("linked", 0),
                                 "unlinked": bucket.get("unlinked", 0)}

    return {
        "mode": "overview",
        "project": rootm.project,
        "subsystems": total_subs,
        "roles": dict(sorted(roles.items())),
        "criticality": dict(sorted(criticality.items())),
        "edges": edges,
        "cycles": [i.message for i in cycle_issues],
        "schema_issues": [i.to_dict() for i in schema_issues],
        "health": {
            "ok": not validation_errors and not cycle_issues and schema_errors == 0,
            "schema_errors": schema_errors,
            "cycles": len(cycle_issues),
            "validation": validation,
        },
        "stats": report.stats,
    }
