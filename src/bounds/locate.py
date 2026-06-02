"""Symbol- and dependency-location queries: the logic behind ``bounds where`` and ``bounds impact``.

Kept out of ``cli.py`` so the command layer stays arg-parse + a single ``go()`` closure (the same
reason ``describe``/``calibrate``/``discover`` have their own modules). Everything here is zero-LLM
and deterministic: it reuses the shared project extractor and the one import resolver, and sorts at
every boundary. No cache is trusted for ``where`` — extraction is always fresh, so a result is current.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

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


def run_where(project_root: Path, query: str, prefix: bool = False) -> dict:
    """Locate a symbol — or a file path — and report which subsystem owns it.

    ``query`` is normally a symbol name: every definition is returned (name collisions across files
    are all reported), each tagged with its owning subsystem and whether that subsystem *declares* it
    in ``exposes``. When ``query`` is instead **path-shaped** — it contains a ``/`` or matches an
    existing repo-relative source file by posix comparison — it is treated as a file lookup: the owning
    subsystem plus every symbol that file defines is returned (BOUNDS-007), so ``where src/foo.py``
    no longer comes back empty. Extraction is fresh (never a stale cache). Works for Python and
    TypeScript/JavaScript. Posix paths only; results sorted for stability.
    """
    _, subs, _ = manifest_loader.load_all(project_root)
    file_owner, extracts, _ = scan.extract_project(project_root, subs, load_matcher(project_root))
    declared_by = {n: subs[n].expose_names() for n in subs}

    target_file = _path_query(project_root, query, extracts)
    if target_file is not None:
        return _where_file(target_file, file_owner, extracts, declared_by)

    def hit(sym_name: str) -> bool:
        return sym_name.startswith(query) if prefix else sym_name == query

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
        "symbol": query,
        "match": "prefix" if prefix else "exact",
        "count": len(results),
        "results": results,
    }


def _path_query(project_root: Path, query: str, extracts: dict) -> str | None:
    """Return the repo-relative posix path ``query`` names, or ``None`` if it's a plain symbol.

    Path-shaped means either it contains a ``/`` (an explicit posix path the user typed) or it
    matches an existing repo-relative source file by posix comparison (so a bare ``main.py`` that
    exists is treated as a file, while a bare symbol name that doesn't exist on disk stays a symbol).
    Compared in posix form on both sides, never with OS separators. A path that exists but isn't an
    extracted source file still resolves (so the caller can report "owned, no symbols" honestly).
    """
    posix = PurePosixPath(query).as_posix()
    if posix in extracts:
        return posix
    abs_path = project_root / query
    try:
        is_file = abs_path.is_file()
    except OSError:
        is_file = False
    if is_file:
        try:
            return abs_path.relative_to(project_root).as_posix()
        except ValueError:
            return None
    if "/" in query:  # explicit path the user typed, even if it doesn't resolve on disk
        return posix
    return None


def _where_file(rel: str, file_owner: dict, extracts: dict, declared_by: dict) -> dict:
    """Build the ``where``-by-file payload: a file's owning subsystem + every symbol it defines.

    Mirrors the symbol-query shape (same per-result keys) so the JSON stays uniform; ``query_kind``
    distinguishes the two so an agent can tell a file lookup from a symbol lookup. ``owning_subsystem``
    comes from the shared ownership map (so it agrees with validate/describe). Results sorted.
    """
    owner = file_owner.get(rel, "")
    declared = declared_by.get(owner, set())
    result = extracts.get(rel)
    symbols = result.symbols if result is not None else []
    results = sorted(
        ({
            "symbol": sym.name,
            "kind": sym.kind,
            "file": rel,
            "line": sym.line,
            "owning_subsystem": owner,
            "exposed": sym.name in declared,
        } for sym in symbols),
        key=lambda r: (r["symbol"], r["line"], r["kind"]),
    )
    return {
        "file": rel,
        "query_kind": "file",
        "owning_subsystem": owner,
        "count": len(results),
        "results": results,
    }
