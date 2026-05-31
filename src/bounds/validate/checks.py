"""The 6 structural checks (+ a schema-health advisory) + the CheckContext they run against + the per-mode dispatch table.

Every check is a pure function ``(CheckContext) -> list[Issue]``. None of them read the filesystem or
call tree-sitter — they operate on the already-extracted results and the loaded manifests. All produced
issues use their natural severity; the engine downgrades errors to warnings for ``quick`` mode.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from posixpath import normpath

from .. import errors
from ..extract.scan import strip_ext
from ..models import ExtractResult, Issue, RootManifest, SubsystemCompact
from .schema import SCHEMA_LANGUAGES, _fold_subsystem_schema, schema_diagnostics


def _issue(
    code: str,
    message: str,
    *,
    severity: str | None = None,
    subsystem: str | None = None,
    file: str | None = None,
    fix: str | None = None,
) -> Issue:
    """Construct an :class:`Issue`, defaulting its severity from the single-source
    ``errors.SEVERITY`` table. An explicit ``severity`` overrides it for the
    context-dependent cases — e.g. an undeclared export surfaced at ``info`` rather than the
    code's canonical ``error`` — so the table stays the one home for canonical severities.
    """
    return Issue(
        code,
        severity or errors.SEVERITY[code],
        message,
        subsystem=subsystem,
        file=file,
        fix=fix,
    )


# ===========================================================================
# Context
# ===========================================================================
@dataclass
class CheckContext:
    project_root: Path
    root: RootManifest
    subsystems: dict[str, SubsystemCompact]
    extracts: dict[str, ExtractResult]  # rel posix path -> result
    file_owner: dict[str, str]          # rel posix path -> subsystem name
    dirty: set[str] = field(default_factory=set)
    propagated: set[str] = field(default_factory=set)
    # Coverage signal: count of local-looking imports that did NOT resolve to an owned
    # file. checks are pure (CheckContext)->list[Issue], so the count is threaded here (the
    # engine reads it after running checks) rather than returned. Only check_boundary writes it.
    unresolved_local_imports: int = 0

    def files_of(self, subsystem: str) -> list[str]:
        """Extracted files owned by ``subsystem`` (sorted, only those present in extracts)."""
        return sorted(
            p for p, owner in self.file_owner.items() if owner == subsystem and p in self.extracts
        )

    def actual_exports(self, subsystem: str) -> set[str]:
        """Exported surface of a subsystem: code's exported symbols + folded table names.

        Table-level only here (not ``table.column``): column-granular drift is resolved
        against the fold in :func:`check_structural_drift`, so enumerating every column into
        the flat set would otherwise spam an undeclared-export ``info`` per column.
        """
        out: set[str] = set()
        for p in self.files_of(subsystem):
            out |= self.extracts[p].exported_names()
        out |= set(self.schema_tables(subsystem))
        return out

    def all_symbol_names(self, subsystem: str) -> set[str]:
        """All symbol names (exported or not) the subsystem actually defines.

        For SQL files the materialized fold is the authority (a dropped/renamed table must
        not linger via its raw CREATE symbol), so raw per-statement SQL symbols are skipped
        and the table + ``table.column`` surface comes from the fold instead.
        """
        out: set[str] = set()
        for p in self.files_of(subsystem):
            if self.extracts[p].language in SCHEMA_LANGUAGES:
                continue
            out |= {s.name for s in self.extracts[p].symbols}
        for table, state in self.schema_tables(subsystem).items():
            out.add(table)
            out |= {f"{table}.{column}" for column in state.columns}
        return out

    def schema_tables(self, subsystem: str):
        """Folded ``{table: _TableState}`` for a schema subsystem (memoized per context)."""
        cache = getattr(self, "_schema_cache", None)
        if cache is None:
            cache = self._schema_cache = {}
        if subsystem not in cache:
            cache[subsystem] = _fold_subsystem_schema(subsystem, self.extracts, self.file_owner)
        return cache[subsystem]

    def _index(self) -> tuple[dict[str, str], dict[str, str]]:
        cached = getattr(self, "_idx", None)
        if cached is None:
            cached = index_extracts(self.extracts)
            self._idx = cached
        return cached

    def known_noext(self) -> dict[str, str]:
        """Map of extension-stripped path -> real rel path, for import resolution (cached)."""
        return self._index()[0]

    def suffix_index(self) -> dict[str, str]:
        """Trailing-segment suffix -> known stem, for O(1) import resolution (cached)."""
        return self._index()[1]

    def known_top_segments(self) -> set[str]:
        """First path segments of every extracted file, for the 'local-looking' test (cached)."""
        cached = getattr(self, "_known_top", None)
        if cached is None:
            cached = {stem.split("/", 1)[0] for stem in self.known_noext()}
            self._known_top = cached
        return cached

    def role_exposes_orphans(self, subsystem: str) -> bool:
        """True if the subsystem's role legitimately exposes unconsumed entrypoints."""
        sub = self.subsystems.get(subsystem)
        if sub is None:
            return False
        registry = self.root.role_registry()
        return bool(registry.get(sub.role, {}).get("orphan_exposes", False))

# ===========================================================================
# Import resolution helpers (best-effort; never produces false positives by guessing)
# ===========================================================================
def index_extracts(extracts: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Build ``(known_noext, suffix_index)`` from an extracts map — the one home for this
    projection, shared by the validate path (:class:`CheckContext`) and the standalone callers
    (``locate``, ``calibrate``, ``discover``). First-wins on a stem collision (sorted iteration +
    ``setdefault``), so import resolution is deterministic and identical everywhere.
    """
    known_noext: dict[str, str] = {}
    for rel in sorted(extracts):
        known_noext.setdefault(strip_ext(rel), rel)
    return known_noext, build_suffix_index(known_noext)


def build_suffix_index(known_noext: dict[str, str]) -> dict[str, str]:
    """Map every trailing path-segment suffix of each known stem to a known stem.

    Replaces import resolution's old ``O(files)`` ``endswith`` scan with an ``O(1)`` lookup.
    For ``"src/bounds/models"`` the suffixes ``"models"``, ``"bounds/models"`` and the full
    path all point back at it. On a collision the lexicographically smallest stem wins, so
    the result is identical to the previous ``sorted(known_noext)`` first-match (determinism).
    """
    idx: dict[str, str] = {}
    for noext in known_noext:
        parts = noext.split("/")
        for i in range(len(parts)):
            suffix = "/".join(parts[i:])
            cur = idx.get(suffix)
            if cur is None or noext < cur:
                idx[suffix] = noext
    return idx


def _is_local_looking(module: str, known_top: set[str]) -> bool:
    """True if ``module`` plausibly refers to an in-project file (coverage signal).

    A relative specifier (``./x``, ``..pkg``) is always local. A bare specifier is local-looking
    only when its first segment matches a top-level segment of some extracted file — so stdlib /
    third-party imports (``os``, ``react``) don't inflate the unresolved-import count, while a
    real intra-repo import we failed to resolve does.
    """
    if not module:
        return False
    if module.startswith("."):
        return True
    first = module.replace(".", "/").split("/", 1)[0]
    return first in known_top


def _candidate_stems(importer_rel: str, module: str) -> list[str]:
    """Possible extension-less path stems a module specifier could resolve to."""
    if not module:
        return []
    if module.startswith("."):
        # Relative: TS "./x"/"../y" or Python dotted-with-leading-dots "..models".
        leading = len(module) - len(module.lstrip("."))
        rest = module[leading:]
        importer_dir = posixpath.dirname(importer_rel)
        up = importer_dir
        for _ in range(max(leading - 1, 0)):
            up = posixpath.dirname(up)
        rest_path = rest.replace(".", "/").strip("/")
        stem = normpath(posixpath.join(up, rest_path)) if rest_path else normpath(up or ".")
        return [] if stem in (".", "", "/") else [stem]
    # Bare dotted (Python "a.b.c") or bare package (TS "react", "os" -> won't match local files).
    return [module.replace(".", "/")]


def resolve_import(
    importer_rel: str,
    module: str,
    known_noext: dict[str, str],
    suffix_index: dict[str, str] | None = None,
) -> str | None:
    """Resolve an import specifier to a known extracted file path, or None if external/ambiguous.

    ``suffix_index`` (from :func:`build_suffix_index`) backs the trailing-segment fallback in
    ``O(1)``; when omitted it is built on demand so ad-hoc callers stay correct. Resolution
    order per candidate stem: exact stem, then package ``/index``/``/__init__``, then the
    smallest stem ending in that segment suffix.
    """
    if suffix_index is None:
        suffix_index = build_suffix_index(known_noext)
    for stem in _candidate_stems(importer_rel, module):
        if stem in known_noext:
            return known_noext[stem]
        for suffix in ("/index", "/__init__"):
            if stem + suffix in known_noext:
                return known_noext[stem + suffix]
        hit = suffix_index.get(stem)
        if hit is not None:
            return known_noext[hit]
    return None


# ===========================================================================
# Check 1 — structural drift
# ===========================================================================
def check_structural_drift(ctx: CheckContext) -> list[Issue]:
    issues: list[Issue] = []
    for name in sorted(ctx.subsystems):
        sub = ctx.subsystems[name]
        declared = sub.expose_names()
        actual = ctx.actual_exports(name)
        schema = ctx.schema_tables(name)
        for missing in sorted(declared - actual):
            # A column-granular expose (``users.email``) is satisfied when the fold still has
            # that table+column; a dropped column then correctly drifts. This mirrors the
            # consumes resolution in check_contract so exposes and consumes agree.
            if "." in missing:
                table, column = missing.split(".", 1)
                state = schema.get(table)
                if table in actual and state is not None and column in state.columns:
                    continue
            issues.append(
                _issue(
                    errors.E_STRUCTURAL_DRIFT,
                    f"subsystem '{name}' declares '{missing}' in exposes but no source file exports it",
                    subsystem=name,
                    fix=f"remove '{missing}' from {name}.exposes, or export it from {sub.paths or ['its sources']}",
                )
            )
        # Undeclared public surface — a symbol the source exports but the manifest doesn't
        # list — is surfaced as info for ANY subsystem with a declared expose set
        # (bidirectional drift). Previously this only fired on unbounded/core subsystems, so the
        # most common real drift (a new undeclared export on a leaf/connector) was invisible.
        # Severity stays info (never blocks), so exit codes are unchanged.
        if declared:
            # Tables whose columns are declared column-granularly (``users.email``) shouldn't
            # also be reported as an undeclared table-level export.
            declared_table_parents = {d.split(".", 1)[0] for d in declared if "." in d}
            for extra in sorted(actual - declared):
                if extra in declared_table_parents:
                    continue
                issues.append(
                    _issue(
                        errors.E_STRUCTURAL_DRIFT,
                        f"subsystem '{name}' exports '{extra}' which is not declared in exposes",
                        severity="info",
                        subsystem=name,
                        fix=f"add '{extra}' to {name}.exposes if it is part of the public surface",
                    )
                )
    return issues


# ===========================================================================
# Check 2 — boundary compliance
# ===========================================================================
def check_boundary(ctx: CheckContext) -> list[Issue]:
    issues: list[Issue] = []
    known = ctx.known_noext()
    suffix_index = ctx.suffix_index()
    known_top = ctx.known_top_segments()
    for name in sorted(ctx.subsystems):
        for rel in ctx.files_of(name):
            result = ctx.extracts[rel]
            for imp in result.imports:
                target = resolve_import(rel, imp.module, known, suffix_index)
                if not target:
                    # Unresolved: if it looks intra-repo, it's a gap in boundary coverage
                    # (an owned file we couldn't attribute) — count it for the coverage signal.
                    if _is_local_looking(imp.module, known_top):
                        ctx.unresolved_local_imports += 1
                    continue
                owner = ctx.file_owner.get(target)
                if not owner or owner == name:
                    continue
                provider = ctx.subsystems.get(owner)
                if provider is None:
                    continue
                exposed = provider.expose_names()
                provider_symbols = ctx.all_symbol_names(owner)
                for nm in sorted(imp.names):
                    if nm in exposed:
                        continue
                    if nm in provider_symbols:  # a real internal that isn't part of the public surface
                        issues.append(
                            _issue(
                                errors.E_BOUNDARY_VIOLATION,
                                f"'{rel}' imports '{nm}' from subsystem '{owner}', which does not expose it",
                                subsystem=name,
                                file=rel,
                                fix=f"import only {owner}'s exposed interfaces, or add '{nm}' to {owner}.exposes",
                            )
                        )
    return issues


# ===========================================================================
# Check 3 — contract compliance
# ===========================================================================
def check_contract(ctx: CheckContext) -> list[Issue]:
    issues: list[Issue] = []
    for name in sorted(ctx.subsystems):
        sub = ctx.subsystems[name]
        for c in sub.consumes:
            provider = ctx.subsystems.get(c.subsystem)
            if provider is None:
                issues.append(
                    _issue(
                        errors.E_UNRESOLVED_REFERENCE,
                        f"subsystem '{name}' consumes unknown subsystem '{c.subsystem}'",
                        subsystem=name,
                        fix=f"create subsystem '{c.subsystem}', or fix the reference in {name}.consumes",
                    )
                )
                continue
            exposed = provider.expose_names()
            schema_tables = ctx.schema_tables(c.subsystem)
            for iface in sorted(c.interfaces):
                if iface in exposed:
                    continue
                if "." in iface:
                    table, column = iface.split(".", 1)
                    state = schema_tables.get(table)
                    if table in exposed and state is not None and column in state.columns:
                        continue
                issues.append(
                    _issue(
                        errors.E_CONTRACT_MISSING_EXPORT,
                        f"subsystem '{name}' depends on '{iface}' from '{c.subsystem}', "
                        f"which does not expose it",
                        subsystem=name,
                        fix=f"add '{iface}' to {c.subsystem}.exposes, or update {name}.consumes",
                    )
                )
    return issues


# ===========================================================================
# Check 4 — cross-subsystem impact
# ===========================================================================
def check_cross_impact(ctx: CheckContext) -> list[Issue]:
    issues: list[Issue] = []
    for consumer in sorted(ctx.propagated):
        if consumer not in ctx.subsystems:
            continue
        providers = sorted(
            c.subsystem for c in ctx.subsystems[consumer].consumes if c.subsystem in ctx.dirty
        )
        provider_text = ", ".join(f"'{p}'" for p in providers) if providers else "an upstream provider"
        issues.append(
            _issue(
                errors.E_STALE_INTERFACE,
                f"interface surface of {provider_text} changed; consumer '{consumer}' may be stale",
                subsystem=consumer,
                fix=f"re-validate '{consumer}' and update its consumes if the provider's interfaces changed",
            )
        )
    return issues


# ===========================================================================
# Check 5 — cycle detection
# ===========================================================================
def check_cycles(ctx: CheckContext) -> list[Issue]:
    graph = {
        name: sorted({c.subsystem for c in sub.consumes if c.subsystem in ctx.subsystems})
        for name, sub in ctx.subsystems.items()
    }
    issues: list[Issue] = []
    for cycle in _find_cycles(graph):
        chain = " -> ".join(cycle + [cycle[0]])
        issues.append(
            _issue(
                errors.E_CYCLE_DETECTED,
                f"circular dependency: {chain}",
                subsystem=cycle[0],
                fix="break the cycle via an interface/inversion, or move shared code into a library subsystem",
            )
        )
    return issues


def _rotate_min(cycle: list[str]) -> list[str]:
    """Rotate a cycle so its lexicographically smallest node is first (stable identity)."""
    i = cycle.index(min(cycle))
    return cycle[i:] + cycle[:i]


def _find_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """Enumerate distinct directed cycles. Iterative DFS: an explicit stack instead of
    recursion so a deep dependency graph can't raise an uncaught ``RecursionError``. Semantics are
    unchanged — a back-edge to a GRAY node yields the cycle from that node to the current one,
    rotated to a canonical (min-first) form and de-duplicated; results sorted by (length, chain)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}
    seen: set[tuple[str, ...]] = set()
    cycles: list[list[str]] = []

    for root in sorted(graph):
        if color[root] != WHITE:
            continue
        path: list[str] = [root]
        stack: list[tuple[str, "object"]] = [(root, iter(graph.get(root, [])))]
        color[root] = GRAY
        while stack:
            u, it = stack[-1]
            descended = False
            for v in it:
                cv = color.get(v, WHITE)
                if cv == GRAY:
                    cyc = _rotate_min(path[path.index(v):])
                    key = tuple(cyc)
                    if key not in seen:
                        seen.add(key)
                        cycles.append(cyc)
                elif cv == WHITE:
                    color[v] = GRAY
                    path.append(v)
                    stack.append((v, iter(graph.get(v, []))))
                    descended = True
                    break
                # BLACK neighbour: fully explored, skip
            if not descended:
                color[u] = BLACK
                path.pop()
                stack.pop()
    return sorted(cycles, key=lambda c: (len(c), c))


# ===========================================================================
# Check 6 — orphan detection
# ===========================================================================
def check_orphans(ctx: CheckContext) -> list[Issue]:
    consumed: set[tuple[str, str]] = set()
    for sub in ctx.subsystems.values():
        for c in sub.consumes:
            for iface in c.interfaces:
                consumed.add((c.subsystem, iface))

    issues: list[Issue] = []
    for name in sorted(ctx.subsystems):
        sub = ctx.subsystems[name]
        if ctx.role_exposes_orphans(name):  # service-like roles expose unconsumed entrypoints
            continue
        for iface in sorted(sub.expose_names()):
            if (name, iface) not in consumed:
                issues.append(
                    _issue(
                        errors.E_ORPHAN_EXPORT,
                        f"interface '{iface}' exposed by '{name}' is consumed by no subsystem",
                        subsystem=name,
                        fix=f"remove '{iface}' from {name}.exposes if unused, "
                        f"or mark '{name}' as a service entrypoint",
                    )
                )
    return issues


# ===========================================================================
# Check 7 — schema health (advisory; warnings only, never blocks)
# ===========================================================================
def check_schema(ctx: CheckContext) -> list[Issue]:
    """Surface SQL-fold advisories: unparsable statements and undetermined migration order.

    Both are warnings by construction (see ``errors.SEVERITY``) — a schema that can't be
    perfectly parsed or ordered is reported, never blocking, honouring fail-soft/report-hard.
    """
    issues: list[Issue] = []
    seen: set[tuple] = set()
    for name in sorted(ctx.subsystems):
        for code, message, file in schema_diagnostics(name, ctx.extracts, ctx.file_owner):
            key = (code, name, file, message)
            if key in seen:
                continue
            seen.add(key)
            issues.append(_issue(code, message, subsystem=name, file=file,
                                 fix="add a numeric filename prefix, a revision/down_revision "
                                     "header, or '-- bounds:order N'; or fix the SQL syntax"))
    return issues


# ===========================================================================
# Mode dispatch
# ===========================================================================
_ALL = [
    check_structural_drift,
    check_boundary,
    check_contract,
    check_cross_impact,
    check_cycles,
    check_orphans,
    check_schema,
]

CHECKS_BY_MODE = {
    "quick": [check_structural_drift, check_cross_impact, check_schema],
    "full": list(_ALL),
    "preflight": list(_ALL),
    "audit": list(_ALL),
    "hotfix": [],
}
