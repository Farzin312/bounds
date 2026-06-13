"""Calibration: reconcile declared manifests against tree-sitter reality.

``bounds calibrate`` extracts each subsystem's actual interface surface and proposes a diff
against what its manifest declares — symbols to **add** (found but undeclared), to **remove**
(declared but gone), and ``consumes`` edges to reconcile against real cross-boundary imports.

Calibration is *not* auto-fix. By default it only prints the proposed diff; ``--apply`` writes
it. The reconciliation rules encode developer intent:

* A declared expose that tree-sitter no longer finds is proposed for **removal** — UNLESS
  another subsystem consumes it (then it's a real contract → flagged ``needs_review``, never
  auto-removed) or it is marked ``internal: true`` (deliberately private → exempt entirely).
* A symbol tree-sitter exports but the manifest omits is proposed for **addition** — UNLESS its
  file is generated (detected by a header marker; see :mod:`bounds.ignore`) or the symbol is
  private (the adapter already drops ``_``-prefixed Python names from the exported set; we
  double-guard). The marker token is intentionally not spelled out in this header — a source file
  that literally contains it would otherwise be self-misdetected as generated.
* A real cross-subsystem import not in ``consumes`` is proposed as a new ``consumes`` edge
  (direct imports only — never invent transitive deps). A declared ``consumes`` interface the
  provider doesn't expose is proposed for removal.
* ``role`` and ``criticality`` are NEVER changed (semantic developer decisions). ``consumed_by``
  is auto-derived by the loader, so it's never written here.

Zero LLM. Deterministic: sorted output, POSIX paths, no timestamps.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from ..shared import config, errors, gitutil, surface
from ..shared import tsconfig
from ..shared.ignore import load_matcher
from .extract.scan import extract_project, subsystems_with_unsupported_source, unsupported_surface_files
from .manifest import loader as manifest_loader
from .validate.checks import current_cycle_keys, index_extracts, resolve_import

__all__ = ["run_calibrate"]


def run_calibrate(
    project_root: Path, *, subsystem: str | None = None, apply: bool = False,
    prune_unknown: bool = False,
    prune_missing_exports: bool = False,
    track_interfaces: bool = False,
    coarsen_interfaces: bool = False,
) -> dict:
    """Reconcile manifests against source; return the proposed diff (and apply it if asked).

    ``prune_unknown`` (only meaningful with ``apply``) additionally removes ``consumes`` edges
    that point at a subsystem which doesn't exist. Off by default so a genuine forward reference
    survives an apply; on, it clears the stale/typo'd edges that otherwise keep ``validate``
    reporting ``unresolved`` forever.

    ``prune_missing_exports`` is an explicit review acceptance path: a supported-language expose that
    source no longer exports is removed even when another manifest still consumes it. Unsupported-
    language exposes remain protected because Bounds has no extractor evidence that they are gone.

    ``track_interfaces`` opts into upgrading bare ``consumes`` edges to interface-level contracts.
    Without it, calibration keeps discovered/draft dependency edges at subsystem granularity so
    orphan-export checks do not suddenly judge every public export as curated contract data.

    ``coarsen_interfaces`` is the explicit recovery path for manifests that already contain
    accidental interface-level ``consumes`` data: it keeps the provider edges but removes the
    interface lists so validation returns to subsystem-level dependency checks."""
    _root, subs, _ = manifest_loader.load_all(project_root)
    if subsystem is not None and subsystem not in subs:
        raise errors.BoundsError(
            errors.E_SUBSYSTEM_NOT_FOUND,
            f"subsystem '{subsystem}' not found",
            fix=f"known subsystems: {sorted(subs)}",
        )
    targets = [subsystem] if subsystem else sorted(subs)

    matcher = load_matcher(project_root)
    file_owner, extracts, generated = extract_project(project_root, subs, matcher)
    # Subsystems that own an UNSUPPORTED-language source file (Go/Rust/Java/…): for those Bounds has
    # no adapter, so it can't extract symbols and has zero evidence a hand-authored expose is gone.
    # Shared with the validation engine (scan.subsystems_with_unsupported_source) so calibrate and
    # validate never disagree about such a manifest.
    unsupported_owners = subsystems_with_unsupported_source(project_root, subs, matcher)
    known_noext, suffix_index = index_extracts(extracts)  # one shared projection (built once)
    aliases = tsconfig.load(project_root)  # TS path aliases, loaded once for the whole run

    # What every subsystem consumes (provider -> set of interface names) and consumed providers.
    consumed_ifaces: dict[tuple[str, str], set[str]] = {}
    for cname in sorted(subs):
        for c in subs[cname].consumes:
            consumed_ifaces.setdefault((c.subsystem, cname), set()).update(c.interfaces)
    consumed_providers_ifaces: dict[str, set[str]] = {}
    for (provider, _consumer), ifaces in consumed_ifaces.items():
        consumed_providers_ifaces.setdefault(provider, set()).update(ifaces)

    proposals: dict[str, dict] = {}
    for name in targets:
        proposal = _calibrate_one(
            name, subs, file_owner, extracts, generated, known_noext, suffix_index,
            consumed_providers_ifaces, aliases, unsupported_owners,
            track_interfaces=track_interfaces, coarsen_interfaces=coarsen_interfaces,
        )
        if prune_missing_exports:
            _accept_supported_review_removals(proposal)
        if _has_changes(proposal):
            proposals[name] = proposal

    applied = False
    if apply and proposals:
        for name, proposal in proposals.items():
            if _has_applicable_changes(proposal, prune_unknown):
                _apply_proposal(subs[name], proposal, prune_unknown=prune_unknown)
                applied = True

    return {
        "mode": "calibrate",
        "applied": applied,
        "track_interfaces": track_interfaces,
        "coarsen_interfaces": coarsen_interfaces,
        "subsystems": proposals,
        "summary": _summarize(proposals),
        "next_steps": _next_steps(proposals, applied=applied, track_interfaces=track_interfaces),
    }


# ---------------------------------------------------------------------------
# Per-subsystem reconciliation
# ---------------------------------------------------------------------------
def _calibrate_one(
    name: str,
    subs: dict,
    file_owner: dict[str, str],
    extracts: dict,
    generated: set[str],
    known_noext: dict[str, str],
    suffix_index: dict[str, str],
    consumed_providers_ifaces: dict[str, set[str]],
    aliases: "tsconfig.TsAliases | None" = None,
    unsupported_owners: set[str] | None = None,
    track_interfaces: bool = False,
    coarsen_interfaces: bool = False,
) -> dict:
    sub = subs[name]
    # When the subsystem owns an unsupported-language file, its exposes may have been hand-authored
    # for symbols Bounds cannot extract — so a "declared but not found" expose is UNVERIFIABLE, never
    # proven stale. Such exposes are routed to needs_review (surfaced, never auto-stripped) instead of
    # remove_exposes. A pure supported-language subsystem is unaffected: a genuinely stale expose is
    # still proposed for removal.
    owns_unsupported = name in (unsupported_owners or set())
    own_files = sorted(p for p, owner in file_owner.items() if owner == name and p in extracts)

    # Actual exported symbols: name -> (kind, file). First sorted file wins for stability.
    actual: dict[str, tuple[str, str]] = {}
    for rel in own_files:
        for sym in extracts[rel].symbols:
            if sym.exported and not sym.name.startswith("_") and sym.name not in actual:
                actual[sym.name] = (sym.kind, rel)

    declared = {i.name: i for i in sub.exposes}
    consumed_here = consumed_providers_ifaces.get(name, set())

    # ADD: exported but undeclared, skipping generated files.
    add_exposes = [
        {"name": s, "kind": actual[s][0], "file": actual[s][1]}
        for s in sorted(set(actual) - set(declared))
        if actual[s][1] not in generated
    ]

    # REMOVE / NEEDS_REVIEW: declared but no longer exported.
    remove_exposes: list[str] = []
    needs_review: list[str] = []
    for s in sorted(set(declared) - set(actual)):
        iface = declared[s]
        if iface.internal:
            continue  # deliberately private — exempt from calibration
        if s in consumed_here or owns_unsupported:
            # consumed → real contract; owns_unsupported → can't verify an unparseable-language
            # symbol is gone. Either way: surface for a human, never auto-remove.
            needs_review.append(s)
        else:
            remove_exposes.append(s)
    review_reasons = {
        s: "unsupported_language" if owns_unsupported else "consumed_contract"
        for s in needs_review
    }

    # CONSUMES reconciliation. Always reconcile provider edges. Interface-level consumes are more
    # precise but also activate orphan-export checks for that provider. That is only trustworthy for
    # curated contracts, so default calibration preserves bare discover-generated edges as bare edges;
    # it enriches interfaces only when the edge already had interface data or the user explicitly
    # opts into --track-interfaces.
    actual_consumes: dict[str, set[str]] = {}
    declared_provider_names = {c.subsystem for c in sub.consumes}
    interface_tracked_providers = {c.subsystem for c in sub.consumes if c.interfaces}
    for rel in own_files:
        for imp in extracts[rel].imports:
            for target, is_member in _resolve_import_targets(rel, imp, known_noext, suffix_index, aliases):
                owner = file_owner.get(target) if target else None
                if not (owner and owner != name and owner in subs):
                    continue
                if is_member and owner not in declared_provider_names:
                    continue
                provider_exposes = subs[owner].expose_names()
                actual_consumes.setdefault(owner, set()).update(
                    nm for nm in imp.names if nm in provider_exposes
                )
    declared_consumes = {c.subsystem: set(c.interfaces) for c in sub.consumes}
    coarsen_consume_interfaces = [
        {"subsystem": c.subsystem, "interfaces": sorted(c.interfaces)}
        for c in sub.consumes
        if coarsen_interfaces and c.interfaces
    ]
    add_consumes = []
    for provider in sorted(set(actual_consumes) - set(declared_consumes)):
        entry = {"subsystem": provider, "interfaces": []}
        if track_interfaces:
            entry["interfaces"] = sorted(actual_consumes[provider])
        add_consumes.append(entry)
    add_consume_interfaces = [
        {"subsystem": provider, "interfaces": sorted(ifaces - declared_consumes[provider])}
        for provider, ifaces in sorted(actual_consumes.items())
        if provider in declared_consumes and ifaces - declared_consumes[provider]
        and not coarsen_interfaces
        and (track_interfaces or provider in interface_tracked_providers)
    ]

    remove_consumes: list[dict] = []
    remove_consume_edges: list[str] = []
    unknown_consumes: list[str] = []
    for c in sub.consumes:
        provider = subs.get(c.subsystem)
        if provider is None:
            # Consumes a subsystem that doesn't exist. It could be a genuine forward reference
            # (incremental adoption) or stale cruft / a typo — Bounds can't read intent, so it
            # SURFACES the edge here (never silently dropped, the old behaviour) and removes it
            # only under an explicit --prune-unknown. validate reports the same edge as
            # E_UNRESOLVED_REFERENCE; this is the calibrate-side fix path for it.
            unknown_consumes.append(c.subsystem)
            continue
        if c.subsystem not in actual_consumes:
            remove_consume_edges.append(c.subsystem)
            continue
        stale = sorted(i for i in c.interfaces if i not in provider.expose_names())
        if stale:
            remove_consumes.append({"subsystem": c.subsystem, "interfaces": stale})

    return {
        "add_exposes": add_exposes,
        "remove_exposes": remove_exposes,
        "needs_review": needs_review,
        "review_reasons": review_reasons,
        "add_consumes": add_consumes,
        "add_consume_interfaces": add_consume_interfaces,
        "coarsen_consume_interfaces": coarsen_consume_interfaces,
        "remove_consumes": remove_consumes,
        "remove_consume_edges": sorted(set(remove_consume_edges)),
        "unknown_consumes": sorted(set(unknown_consumes)),
    }


def _resolve_import_targets(
    importer_rel: str,
    imp,
    known_noext: dict[str, str],
    suffix_index: dict[str, str],
    aliases: "tsconfig.TsAliases | None",
) -> list[tuple[str, bool]]:
    """Resolve the imported module plus Python package-member imports.

    Python commonly writes ``from . import output`` or ``from .. import gitutil``. The raw module
    specifier resolves to the package, but the dependency is the imported member module when a
    sibling file/package exists. Without checking those member targets, stale-edge pruning can delete
    real dependencies that happen to use package import syntax."""
    targets: list[tuple[str, bool]] = []

    def add(module: str, *, is_member: bool) -> None:
        target = resolve_import(importer_rel, module, known_noext, suffix_index, aliases)
        item = (target, is_member) if target else None
        if item and item not in targets:
            targets.append(item)

    add(imp.module, is_member=False)
    for name in imp.names:
        if not name:
            continue
        if imp.module.endswith("."):
            add(f"{imp.module}{name}", is_member=True)
        elif imp.module:
            add(f"{imp.module}.{name}", is_member=True)
        else:
            add(name, is_member=True)
    return targets


def _has_changes(p: dict) -> bool:
    return any(
        p.get(k)
        for k in (
            "add_exposes",
            "remove_exposes",
            "needs_review",
            "add_consumes",
            "add_consume_interfaces",
            "coarsen_consume_interfaces",
            "remove_consumes",
            "remove_consume_edges",
            "unknown_consumes",  # surfaced so a dangling-consumes-only subsystem still appears
        )
    )


def _has_applicable_changes(p: dict, prune_unknown: bool) -> bool:
    """Whether applying this proposal would actually rewrite the manifest, given the prune flag.

    ``needs_review`` and (un-pruned) ``unknown_consumes`` are SURFACED in the diff but never
    written, so a proposal carrying only those must not trigger a no-op manifest rewrite (which
    would strip comments for nothing)."""
    if any(p.get(k) for k in
           ("add_exposes", "remove_exposes", "add_consumes", "add_consume_interfaces",
            "coarsen_consume_interfaces", "remove_consumes", "remove_consume_edges")):
        return True
    return prune_unknown and bool(p.get("unknown_consumes"))


def _accept_supported_review_removals(proposal: dict) -> None:
    """Move supported-language review removals into the applied removal set.

    ``needs_review`` has two meanings: "still consumed elsewhere" and "unverifiable unsupported
    language." This option is the explicit CLI path for the first case only; removing hand-authored
    unsupported-language exposes would be data loss, not calibration."""
    reasons = proposal.get("review_reasons") or {}
    accepted = [
        name for name in proposal.get("needs_review", [])
        if reasons.get(name) != "unsupported_language"
    ]
    if not accepted:
        return
    proposal["remove_exposes"] = sorted(set(proposal.get("remove_exposes", [])) | set(accepted))
    proposal["needs_review"] = [
        name for name in proposal.get("needs_review", [])
        if name not in set(accepted)
    ]
    proposal["review_reasons"] = {
        name: reason for name, reason in reasons.items()
        if name in proposal.get("needs_review", [])
    }


# ---------------------------------------------------------------------------
# Drift baseline + check (the freshness gate)
# ---------------------------------------------------------------------------
# A "drift key" is a stable, content-addressed identifier for one proposed reconciliation —
# an undeclared export, a vanished declared export, a missing/stale consumes edge. The set of
# keys is the schema-vs-source drift; the baseline records the keys that already exist on main
# so the check flags only NEW keys a branch introduces. ``needs_review`` is deliberately
# excluded: it is a real contract a human must resolve, never auto-drift the gate blocks on.
_KEY_SEP = "\t"  # tab: never appears in a symbol/subsystem name, so keys never collide


def drift_keys(proposals: dict[str, dict]) -> list[str]:
    """Canonical, sorted drift keys for a calibrate proposals dict (deterministic)."""
    keys: set[str] = set()
    for sub in sorted(proposals):
        p = proposals[sub]
        for add in p.get("add_exposes", []):
            keys.add(_KEY_SEP.join((sub, "add_expose", str(add["name"]))))
        for name in p.get("remove_exposes", []):
            keys.add(_KEY_SEP.join((sub, "remove_expose", str(name))))
        for prov in p.get("add_consumes", []):
            provider = prov.get("subsystem") if isinstance(prov, dict) else str(prov)
            ifaces = ",".join(prov.get("interfaces") or []) if isinstance(prov, dict) else ""
            keys.add(_KEY_SEP.join((sub, "add_consume", f"{provider}:{ifaces}")))
        for aci in p.get("add_consume_interfaces", []):
            for iface in aci.get("interfaces", []):
                keys.add(_KEY_SEP.join((sub, "add_consume_interface", f"{aci['subsystem']}:{iface}")))
        for rc in p.get("remove_consumes", []):
            for iface in rc.get("interfaces", []):
                keys.add(_KEY_SEP.join((sub, "remove_consume", f"{rc['subsystem']}:{iface}")))
        for provider in p.get("remove_consume_edges", []):
            keys.add(_KEY_SEP.join((sub, "remove_consume_edge", str(provider))))
    return sorted(keys)


def _baseline_path(project_root: Path) -> Path:
    return config.config_dir(project_root) / config.DRIFT_BASELINE_FILE


def _cycle_baseline_path(project_root: Path) -> Path:
    return config.config_dir(project_root) / config.CYCLE_BASELINE_FILE


def load_cycle_baseline(project_root: Path) -> set[str]:
    """Accepted-cycle keys from ``.bounds/cycle-baseline.json`` (empty if absent/malformed).

    Fail-soft: a missing/unreadable/wrong-shape file yields an empty set so the caller treats it
    as "no baseline" rather than crashing — same posture as the drift/surface baselines.
    """
    path = _cycle_baseline_path(project_root)
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    cycles = data.get("cycles") if isinstance(data, dict) else None
    if not isinstance(cycles, list):
        return set()
    return {str(k) for k in cycles}


def _dump_cycle_baseline(project_root: Path, subsystems: dict) -> tuple[Path, int]:
    """Write the current accepted-cycle keys; return ``(path, count)``."""
    keys = current_cycle_keys(subsystems)
    path = _cycle_baseline_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "cycles": keys}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path, len(keys)


def _load_baseline(project_root: Path) -> tuple[set[str], bool]:
    """Return ``(drift_keys, valid)`` for the committed baseline.

    ``valid`` is True only when the file exists AND parses to the expected shape. A missing,
    unreadable, or malformed baseline yields ``(set(), False)`` so the caller treats it as
    absent rather than as an empty (everything-is-new) baseline that exists — fail-soft, no raise.
    """
    path = _baseline_path(project_root)
    if not path.is_file():
        return set(), False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set(), False
    drift = data.get("drift") if isinstance(data, dict) else None
    if not isinstance(drift, list):
        return set(), False
    return {str(k) for k in drift}, True


def _current_proposals(project_root: Path, subsystem: str | None) -> dict[str, dict]:
    """The current calibrate proposals (no writes) — the live schema-vs-source drift."""
    return run_calibrate(project_root, subsystem=subsystem, apply=False)["subsystems"]


def dump_baseline(project_root: Path, *, subsystem: str | None = None) -> dict:
    """Write the current drift keys to ``.bounds/drift-baseline.json`` and report the count.

    Run once on a clean main branch and commit the result; ``calibrate --check`` then flags
    only drift introduced ABOVE this baseline. Writing nothing else keeps the file a stable,
    reviewable record of accepted-existing drift.
    """
    keys = drift_keys(_current_proposals(project_root, subsystem))
    path = _baseline_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "drift": keys}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    result = {
        "mode": "calibrate-baseline",
        "baseline": path.relative_to(project_root).as_posix(),
        "drift_count": len(keys),
        "note": f"wrote drift baseline with {len(keys)} existing item(s) — commit it so CI compares against this",
    }
    # Also snapshot each subsystem's UNSUPPORTED-language surface (per-file content hash) so `validate`
    # can later flag E_UNSUPPORTED_SURFACE_STALE when a hand-authored expose's file changes. Whole-repo
    # (the surface baseline is holistic) and gitignore-aware to match validate; written only when there
    # IS unsupported source, so pure-supported repos get no extra committed file.
    _, subs, _ = manifest_loader.load_all(project_root)
    repo = gitutil.repo_root(project_root) or project_root
    surfaces = unsupported_surface_files(project_root, subs, load_matcher(project_root), repo=repo)
    if surfaces:
        spath = surface.write_baseline(project_root, surfaces)
        n_files = sum(len(f) for f in surfaces.values())
        result["surface_baseline"] = spath.relative_to(project_root).as_posix()
        result["surface_files"] = n_files
        result["note"] += (
            f"; recorded {n_files} unsupported-language file hash(es) across "
            f"{len(surfaces)} subsystem(s) for staleness detection"
        )
    # Also snapshot the accepted subsystem-level dependency cycles so `validate`/`preflight` fail
    # only on NEW cycles. Whole-repo by nature (cycles are a graph property, not per-subsystem), so
    # this ignores the `subsystem` filter. Written even when empty, so committing a clean baseline
    # documents "zero accepted cycles" and arms the regression gate.
    cpath, n_cycles = _dump_cycle_baseline(project_root, subs)
    result["cycle_baseline"] = cpath.relative_to(project_root).as_posix()
    result["cycle_count"] = n_cycles
    result["note"] += f"; recorded {n_cycles} accepted cycle(s) — only new cycles will fail the gate"
    return result


def check_drift(project_root: Path, *, subsystem: str | None = None) -> dict:
    """Compare current drift against the committed baseline; report only NEW drift.

    Pure detection — never writes a manifest. ``ok`` is False when the branch introduces
    drift keys absent from the baseline (the CLI then exits non-zero so CI can gate on it);
    resolving pre-existing drift never fails the check.
    """
    current = drift_keys(_current_proposals(project_root, subsystem))
    baseline, has_baseline = _load_baseline(project_root)  # has_baseline ⇒ present AND valid
    new_drift = sorted(set(current) - baseline)
    resolved = sorted(baseline - set(current))
    ok = not new_drift
    if ok and has_baseline:
        note = "no new structural drift above the committed baseline"
    elif ok:
        note = "no structural drift (manifests match source)"
    else:
        note = (
            f"{len(new_drift)} new structural drift item(s) since the baseline — "
            "run `bounds calibrate` to review, then `--apply` or `--dump-baseline`"
        )
    return {
        "mode": "calibrate-check",
        "ok": ok,
        "has_baseline": has_baseline,
        "new_drift": [_drift_key_to_dict(k) for k in new_drift],
        "new_count": len(new_drift),
        "resolved_count": len(resolved),
        "baseline_count": len(baseline),
        "current_count": len(current),
        "note": note,
    }


def _drift_key_to_dict(key: str) -> dict:
    """Expand a tab-joined drift key back into a structured ``{subsystem, change, target}``."""
    parts = key.split(_KEY_SEP)
    sub = parts[0] if parts else ""
    change = parts[1] if len(parts) > 1 else ""
    target = parts[2] if len(parts) > 2 else ""
    return {"subsystem": sub, "change": change, "target": target}


def _summarize(proposals: dict[str, dict]) -> dict:
    return {
        "added": sum(len(p["add_exposes"]) for p in proposals.values()),
        "removed": sum(len(p["remove_exposes"]) for p in proposals.values()),
        "needs_review": sum(len(p["needs_review"]) for p in proposals.values()),
        "consumes_added": sum(len(p["add_consumes"]) for p in proposals.values()),
        "consume_interfaces_added": sum(
            len(aci.get("interfaces", []))
            for p in proposals.values()
            for aci in p.get("add_consume_interfaces", [])
        ),
        "consume_interfaces_removed": sum(
            len(aci.get("interfaces", []))
            for p in proposals.values()
            for aci in p.get("coarsen_consume_interfaces", [])
        ),
        "consumes_removed": (
            sum(len(p["remove_consumes"]) for p in proposals.values())
            + sum(len(p.get("remove_consume_edges", [])) for p in proposals.values())
        ),
        "consumes_unknown": sum(len(p.get("unknown_consumes", [])) for p in proposals.values()),
    }


def _next_steps(proposals: dict[str, dict], *, applied: bool, track_interfaces: bool = False) -> list[str]:
    """Actionable follow-up for calibration's exact scope.

    Calibration can reconcile manifests with extracted source, but it cannot map new files or break
    source-level dependency cycles. Naming that boundary is the difference between a user knowing
    what to do next and repeatedly running `calibrate` expecting unrelated validate errors to clear.
    """
    summary = _summarize(proposals)
    if not proposals:
        return [
            "No manifest/source drift was found; `calibrate` does not add unmapped source files. "
            "Run `bounds validate -H` for coverage gaps, cycles, boundary violations, and contract "
            "issues outside calibration's scope."
        ]

    steps: list[str] = []
    if applied:
        steps.append(
            "Re-run `bounds validate -H`; remaining E_CYCLE_DETECTED or E_COVERAGE_GAP issues require "
            "source-boundary or mapping changes, not more calibration."
        )
    else:
        steps.append(
            "Review this diff, then run `bounds calibrate --apply` to write only these manifest/source "
            "reconciliation changes."
        )
    if summary["needs_review"]:
        steps.append(
            "Resolve needs-review exposes by updating consumers or, for supported-language exports that "
            "are intentionally gone, re-run with `--prune-missing-exports --apply`."
        )
    if summary["consumes_unknown"]:
        steps.append(
            "For consumes? entries, rename the referenced subsystem or re-run with "
            "`--prune-unknown --apply` to remove stale dangling edges."
        )
    if summary["consume_interfaces_removed"]:
        steps.append(
            "Interface lists were removed from consumes edges, leaving provider edges intact. "
            "Use `--track-interfaces` later only for providers whose exact public contract you "
            "want orphan-export checks to judge."
        )
    if summary["consumes_added"] and not track_interfaces:
        steps.append(
            "New consumes edges were recorded at subsystem granularity. Use "
            "`bounds calibrate --track-interfaces --apply` only when you want curated "
            "interface-level contracts and orphan-export checks for those providers."
        )
    steps.append(
        "`calibrate` does not add unmapped source files to subsystems and does not break import cycles; "
        "use `bounds validate -H` for those exact issue classes and fixes."
    )
    return steps


# ---------------------------------------------------------------------------
# Apply (rewrite manifest YAML)
# ---------------------------------------------------------------------------
def _apply_proposal(sub, proposal: dict, prune_unknown: bool = False) -> None:
    """Rewrite the subsystem's manifest YAML with the proposed exposes/consumes changes.

    Re-serializes via ``yaml.safe_dump`` (comments are not preserved — calibrate is a
    reviewed reconcile step, so the developer has already seen the diff). ``role`` and
    ``criticality`` are passed through untouched. With ``prune_unknown`` the consumes edges
    naming a non-existent subsystem (``proposal["unknown_consumes"]``) are dropped too.
    """
    path = Path(sub.source_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    exposes = list(raw.get("exposes") or [])
    removed = set(proposal["remove_exposes"])
    exposes = [e for e in exposes if _expose_name(e) not in removed]
    for add in proposal["add_exposes"]:
        exposes.append({"name": add["name"], "kind": add["kind"]})
    raw["exposes"] = exposes

    consumes = list(raw.get("consumes") or [])
    if proposal.get("remove_consume_edges"):
        stale_edges = set(proposal["remove_consume_edges"])
        consumes = [
            e for e in consumes
            if not (isinstance(e, dict) and e.get("subsystem") in stale_edges)
        ]
    # Prune dangling edges (consumes a subsystem that doesn't exist) only when asked.
    if prune_unknown and proposal.get("unknown_consumes"):
        unknown = set(proposal["unknown_consumes"])
        consumes = [
            e for e in consumes
            if not (isinstance(e, dict) and e.get("subsystem") in unknown)
        ]
    # Drop stale interfaces from existing edges.
    stale_by_provider = {rc["subsystem"]: set(rc["interfaces"]) for rc in proposal["remove_consumes"]}
    for entry in consumes:
        if isinstance(entry, dict) and entry.get("subsystem") in stale_by_provider:
            ifaces = [i for i in (entry.get("interfaces") or []) if i not in stale_by_provider[entry["subsystem"]]]
            # Drop the key entirely when no interfaces remain rather than writing a noisy
            # `interfaces: []` — the dependency edge stays as a bare `{subsystem: x}`.
            if ifaces:
                entry["interfaces"] = ifaces
            else:
                entry.pop("interfaces", None)
    add_ifaces_by_provider = {
        aci["subsystem"]: set(aci.get("interfaces") or [])
        for aci in proposal.get("add_consume_interfaces", [])
    }
    for entry in consumes:
        if not isinstance(entry, dict):
            continue
        provider = entry.get("subsystem")
        if provider not in add_ifaces_by_provider:
            continue
        merged = sorted(set(entry.get("interfaces") or []) | add_ifaces_by_provider[provider])
        if merged:
            entry["interfaces"] = merged
    coarsen_providers = {
        aci["subsystem"] for aci in proposal.get("coarsen_consume_interfaces", [])
    }
    for entry in consumes:
        if isinstance(entry, dict) and entry.get("subsystem") in coarsen_providers:
            entry.pop("interfaces", None)
    for prov in proposal["add_consumes"]:
        if isinstance(prov, dict):
            entry = {"subsystem": prov["subsystem"]}
            if prov.get("interfaces"):
                entry["interfaces"] = list(prov["interfaces"])
            consumes.append(entry)
        else:
            consumes.append({"subsystem": prov})
    if consumes:
        raw["consumes"] = consumes
    else:
        raw.pop("consumes", None)  # pruned the last edge → drop the key, don't keep the stale one

    path.write_text(yaml.safe_dump(raw, sort_keys=False, default_flow_style=False), encoding="utf-8")


def _expose_name(entry) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return str(entry.get("name", ""))
    return ""
