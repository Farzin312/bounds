"""Symbol- and dependency-location queries: the logic behind ``bounds where`` and ``bounds impact``.

Kept out of ``cli.py`` so the command layer stays arg-parse + a single ``go()`` closure (the same
reason ``describe``/``calibrate``/``discover`` have their own modules). Everything here is zero-LLM
and deterministic: it reuses the shared project extractor and the one import resolver, and sorts at
every boundary. No cache is trusted for ``where`` — extraction is always fresh, so a result is current.
"""

from __future__ import annotations

from pathlib import Path

from . import errors
from . import tsconfig
from .extract import scan
from .ignore import load_matcher
from .manifest import loader as manifest_loader
from .validate import propagation
from .validate.checks import index_extracts, resolve_import


def run_impact(project_root: Path, name: str, verify: bool = False, include_raw: bool = False) -> dict:
    """Blast radius of ``name``: who breaks if its public surface changes.

    The headline ``blast_radius`` counts only *declared* ``consumes`` edges, so it is an honest
    lower bound — a real import that no manifest declares is not counted. ``--verify`` resolves the
    actual import graph and lists consumers that import from ``name`` without declaring it.
    ``include_raw`` adds an advisory ``heuristic_consumers`` block (raw-SQL string refs to the
    table) — opt-in, clearly low-confidence, and never part of the verified blast radius.
    Raises ``E_SUBSYSTEM_NOT_FOUND`` for an unknown subsystem.
    """
    _, subs, _ = manifest_loader.load_all(project_root)
    if name not in subs:
        table_payload = _run_interface_impact(subs, name)
        if table_payload is not None:
            if include_raw:
                _attach_heuristic_consumers(project_root, subs, name, table_payload)
            return table_payload
        raise errors.BoundsError(errors.E_SUBSYSTEM_NOT_FOUND, f"subsystem or interface '{name}' not found", fix=f"known subsystems: {sorted(subs)}")
    direct = propagation.build_consumer_index(subs).get(name, [])
    transitive = propagation.transitive_consumers(name, subs)
    consumers = [
        {"name": cname, "via": c.via, "interfaces": sorted(c.interfaces)}
        for cname in direct
        for c in subs[cname].consumes
        if c.subsystem == name
    ]
    payload = {
        "subsystem": name,
        "criticality": subs[name].criticality,
        "direct_consumers": sorted(direct),
        "transitive_consumers": transitive,
        "blast_radius": len(transitive),
        "basis": "declared-consumes",
        "blast_radius_is_lower_bound": True,
        "note": (
            "blast_radius counts only declared `consumes` edges; an import not declared in a "
            f"manifest is not counted, so treat it as a lower bound. Run `bounds impact {name} "
            "--verify` to cross-check the resolved import graph, or `bounds validate` for "
            "boundary/contract checks."
        ),
        "consumers": consumers,
    }
    if verify:
        payload["undeclared_consumer_edges"] = _undeclared_consumer_edges(project_root, subs, name)
    if include_raw:
        _attach_heuristic_consumers(project_root, subs, name, payload)
    return payload


def _attach_heuristic_consumers(project_root: Path, subs: dict, table: str, payload: dict) -> None:
    """Add the opt-in, advisory raw-SQL-query consumers of ``table`` (never blocking, never verified)."""
    from .extract import rawquery, supported_extensions

    exts = supported_extensions()
    file_owner: dict[str, str] = {}
    for nm in sorted(subs):
        for abs_path in scan.iter_subsystem_files(project_root, subs[nm], exts):
            file_owner.setdefault(abs_path.relative_to(project_root).as_posix(), nm)
    payload["heuristic_consumers"] = rawquery.scan_table_consumers(project_root, file_owner, table)
    payload["heuristic_note"] = (
        "heuristic_consumers come from raw-SQL string matching and are LOW-CONFIDENCE / "
        "advisory only — they are never counted in blast_radius and never block validation."
    )


def _run_interface_impact(subs: dict, interface: str) -> dict | None:
    providers = [name for name, sub in sorted(subs.items()) if interface in sub.expose_names()]
    if not providers:
        return None
    consumers: list[dict] = []
    direct: set[str] = set()
    for provider in providers:
        for cname, sub in sorted(subs.items()):
            for c in sub.consumes:
                if c.subsystem == provider and interface in c.interfaces:
                    direct.add(cname)
                    consumers.append({"name": cname, "provider": provider, "via": c.via, "interfaces": sorted(c.interfaces)})
    transitive: set[str] = set()
    for consumer in sorted(direct):
        transitive.add(consumer)
        transitive.update(propagation.transitive_consumers(consumer, subs))
    return {
        "interface": interface,
        "providers": providers,
        "direct_consumers": sorted(direct),
        "transitive_consumers": sorted(transitive),
        "blast_radius": len(transitive),
        "basis": "declared-consumes-interface",
        "blast_radius_is_lower_bound": True,
        "consumers": consumers,
    }


def _undeclared_consumer_edges(project_root: Path, subs: dict, name: str) -> list[dict]:
    """Consumers that really import from ``name`` but declare no ``consumes`` edge to it.

    These are exactly the edges the declared-only blast radius misses. Built from a fresh
    project-wide extraction + the shared import resolver, so it agrees with validate on ownership.
    """
    file_owner, extracts, _ = scan.extract_project(project_root, subs, load_matcher(project_root))
    known_noext, suffix_index = index_extracts(extracts)
    aliases = tsconfig.load(project_root)
    declared = set(subs[name].consumed_by)  # subsystems that already declare consuming `name`
    evidence: dict[str, set[str]] = {}
    for rel in sorted(extracts):
        owner = file_owner.get(rel)
        if owner is None or owner == name:
            continue
        for imp in extracts[rel].imports:
            target = resolve_import(rel, imp.module, known_noext, suffix_index, aliases)
            if target and file_owner.get(target) == name:
                evidence.setdefault(owner, set()).add(rel)
    return [
        {"consumer": consumer, "files": sorted(files)}
        for consumer, files in sorted(evidence.items())
        if consumer not in declared
    ]


def run_where(project_root: Path, symbol: str, prefix: bool = False) -> dict:
    """Locate a symbol: which file defines it and which subsystem owns it.

    Returns every definition — name collisions across files are all reported — each tagged with its
    owning subsystem and whether that subsystem *declares* it in ``exposes``. Extraction is fresh
    (never a stale cache). Works for Python and TypeScript/JavaScript. Results sorted for stability.
    """
    _, subs, _ = manifest_loader.load_all(project_root)
    file_owner, extracts, _ = scan.extract_project(project_root, subs, load_matcher(project_root))
    declared_by = {n: subs[n].expose_names() for n in subs}

    def hit(sym_name: str) -> bool:
        return sym_name.startswith(symbol) if prefix else sym_name == symbol

    results: list[dict] = []
    for rel in extracts:
        owner = file_owner.get(rel, "")
        declared = declared_by.get(owner, set())
        for sym in extracts[rel].symbols:
            if hit(sym.name):
                results.append({
                    "symbol": sym.name,
                    "kind": sym.kind,
                    "file": rel,
                    "line": sym.line,
                    "owning_subsystem": owner,
                    "exposed": sym.name in declared,
                })
    results.sort(key=lambda r: (r["file"], r["symbol"], r["line"], r["kind"]))
    return {
        "symbol": symbol,
        "match": "prefix" if prefix else "exact",
        "count": len(results),
        "results": results,
    }
