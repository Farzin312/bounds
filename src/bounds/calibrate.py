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

import json
from pathlib import Path

import yaml

from . import config, errors
from .extract.scan import extract_project
from .ignore import load_matcher
from .manifest import loader as manifest_loader
from .validate.checks import index_extracts, resolve_import


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
    known_noext, suffix_index = index_extracts(extracts)  # one shared projection (built once)

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
            keys.add(_KEY_SEP.join((sub, "add_consume", str(prov))))
        for rc in p.get("remove_consumes", []):
            for iface in rc.get("interfaces", []):
                keys.add(_KEY_SEP.join((sub, "remove_consume", f"{rc['subsystem']}:{iface}")))
    return sorted(keys)


def _baseline_path(project_root: Path) -> Path:
    return config.config_dir(project_root) / config.DRIFT_BASELINE_FILE


def _load_baseline(project_root: Path) -> set[str]:
    """The committed baseline drift keys, or an empty set when no baseline exists yet."""
    path = _baseline_path(project_root)
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    drift = data.get("drift") if isinstance(data, dict) else None
    return {str(k) for k in drift} if isinstance(drift, list) else set()


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
    return {
        "mode": "calibrate-baseline",
        "baseline": path.relative_to(project_root).as_posix(),
        "drift_count": len(keys),
        "note": f"wrote drift baseline with {len(keys)} existing item(s) — commit it so CI compares against this",
    }


def check_drift(project_root: Path, *, subsystem: str | None = None) -> dict:
    """Compare current drift against the committed baseline; report only NEW drift.

    Pure detection — never writes a manifest. ``ok`` is False when the branch introduces
    drift keys absent from the baseline (the CLI then exits non-zero so CI can gate on it);
    resolving pre-existing drift never fails the check.
    """
    current = drift_keys(_current_proposals(project_root, subsystem))
    baseline = _load_baseline(project_root)
    has_baseline = _baseline_path(project_root).is_file()
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
