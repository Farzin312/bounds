"""Calibration (s-16): reconcile declared manifests against tree-sitter reality.

``bounds calibrate`` extracts each subsystem's actual interface surface and proposes a diff
against what its manifest declares — symbols to **add** (found but undeclared), to **remove**
(declared but gone), and ``consumes`` edges to reconcile against real cross-boundary imports.

Calibration is *not* auto-fix. By default it only prints the proposed diff; ``--apply`` writes
it. The reconciliation rules encode developer intent (from the absorbed s-16 spec):

* A declared expose that tree-sitter no longer finds is proposed for **removal** — UNLESS
  another subsystem consumes it (then it's a real contract → flagged ``needs_review``, never
  auto-removed) or it is marked ``internal: true`` (deliberately private → exempt entirely).
* A symbol tree-sitter exports but the manifest omits is proposed for **addition** — UNLESS its
  file is ``@generated`` or the symbol is private (the adapter already drops ``_``-prefixed
  Python names from the exported set; we double-guard).
* A real cross-subsystem import not in ``consumes`` is proposed as a new ``consumes`` edge
  (direct imports only — never invent transitive deps). A declared ``consumes`` interface the
  provider doesn't expose is proposed for removal.
* ``role`` and ``criticality`` are NEVER changed (semantic developer decisions). ``consumed_by``
  is auto-derived by the loader, so it's never written here.

Zero LLM. Deterministic: sorted output, POSIX paths, no timestamps.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from . import errors
from .extract.scan import extract_project, strip_ext
from .ignore import load_matcher
from .manifest import loader as manifest_loader
from .validate.checks import build_suffix_index, resolve_import


def run_calibrate(project_root: Path, *, subsystem: str | None = None, apply: bool = False) -> dict:
    """Reconcile manifests against source; return the proposed diff (and apply it if asked)."""
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
    known_noext = {strip_ext(rel): rel for rel in sorted(extracts)}
    suffix_index = build_suffix_index(known_noext)  # built once; O(1) per-import resolution (s-34)

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
            consumed_providers_ifaces,
        )
        if _has_changes(proposal):
            proposals[name] = proposal

    applied = False
    if apply and proposals:
        for name, proposal in proposals.items():
            _apply_proposal(subs[name], proposal)
        applied = True

    return {
        "mode": "calibrate",
        "applied": applied,
        "subsystems": proposals,
        "summary": _summarize(proposals),
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
) -> dict:
    sub = subs[name]
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
        if s in consumed_here:
            needs_review.append(s)  # real contract; never auto-remove
        else:
            remove_exposes.append(s)

    # CONSUMES reconciliation.
    actual_owners: set[str] = set()
    for rel in own_files:
        for imp in extracts[rel].imports:
            target = resolve_import(rel, imp.module, known_noext, suffix_index)
            owner = file_owner.get(target) if target else None
            if owner and owner != name and owner in subs:
                actual_owners.add(owner)
    declared_consumes = {c.subsystem for c in sub.consumes}
    add_consumes = sorted(actual_owners - declared_consumes)

    remove_consumes: list[dict] = []
    for c in sub.consumes:
        provider = subs.get(c.subsystem)
        if provider is None:
            continue  # forward reference — leave it
        stale = sorted(i for i in c.interfaces if i not in provider.expose_names())
        if stale:
            remove_consumes.append({"subsystem": c.subsystem, "interfaces": stale})

    return {
        "add_exposes": add_exposes,
        "remove_exposes": remove_exposes,
        "needs_review": needs_review,
        "add_consumes": add_consumes,
        "remove_consumes": remove_consumes,
    }


def _has_changes(p: dict) -> bool:
    return any(p[k] for k in ("add_exposes", "remove_exposes", "add_consumes", "remove_consumes"))


def _summarize(proposals: dict[str, dict]) -> dict:
    return {
        "added": sum(len(p["add_exposes"]) for p in proposals.values()),
        "removed": sum(len(p["remove_exposes"]) for p in proposals.values()),
        "needs_review": sum(len(p["needs_review"]) for p in proposals.values()),
        "consumes_added": sum(len(p["add_consumes"]) for p in proposals.values()),
        "consumes_removed": sum(len(p["remove_consumes"]) for p in proposals.values()),
    }


# ---------------------------------------------------------------------------
# Apply (rewrite manifest YAML)
# ---------------------------------------------------------------------------
def _apply_proposal(sub, proposal: dict) -> None:
    """Rewrite the subsystem's manifest YAML with the proposed exposes/consumes changes.

    Re-serializes via ``yaml.safe_dump`` (comments are not preserved — calibrate is a
    reviewed reconcile step, so the developer has already seen the diff). ``role`` and
    ``criticality`` are passed through untouched.
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
    for prov in proposal["add_consumes"]:
        consumes.append({"subsystem": prov})
    if consumes:
        raw["consumes"] = consumes

    path.write_text(yaml.safe_dump(raw, sort_keys=False, default_flow_style=False), encoding="utf-8")


def _expose_name(entry) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return str(entry.get("name", ""))
    return ""
