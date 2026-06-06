"""The 8 checks (six structural + schema health + adapter output contracts), their context, and mode dispatch.

Every check is a pure function ``(CheckContext) -> list[Issue]``. None of them read the filesystem or
call tree-sitter — they operate on the already-extracted results and the loaded manifests. All produced
issues use their natural severity; the engine downgrades errors to warnings for ``quick`` mode.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from posixpath import normpath

from ...shared import errors, tsconfig
from ..extract import get_adapter
from ..extract.scan import is_framework_entry_file, is_test_file, is_test_symbol, strip_ext
from ...shared.models import ExtractResult, Issue, RootManifest, SubsystemCompact
from .schema import SCHEMA_LANGUAGES, _fold_subsystem_schema, schema_diagnostics

__all__ = ["CheckContext", "check_cycles", "index_extracts", "resolve_import"]

# Sentinel for "not yet computed" so a genuinely absent tsconfig (cached as None) isn't reloaded.
_UNSET = object()

# Importer extensions for which tsconfig path aliases apply (a Python file never uses them).
_TS_IMPORTER_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


def _issue(
    code: str,
    message: str,
    *,
    severity: str | None = None,
    subsystem: str | None = None,
    file: str | None = None,
    fix: str | None = None,
    count: int = 1,
) -> Issue:
    """Construct an :class:`Issue`, defaulting its severity from the single-source
    ``errors.SEVERITY`` table. An explicit ``severity`` overrides it for the
    context-dependent cases — e.g. an undeclared export surfaced at ``info`` rather than the
    code's canonical ``error`` — so the table stays the one home for canonical severities.
    ``count`` (>1) marks an issue that rolls up that many findings into one line.
    """
    return Issue(
        code,
        severity or errors.SEVERITY[code],
        message,
        count=count,
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
    # Subsystems that own an UNSUPPORTED-language source file (Go/Rust/Java/…). For those Bounds has
    # no adapter, so it extracted nothing and has zero evidence a declared expose is gone — such a
    # declared-but-absent expose must NOT be flagged as structural drift (it is hand-authored and
    # unverifiable). Computed once by the engine via scan.subsystems_with_unsupported_source — the
    # SAME helper calibrate uses — so the two can never disagree about an unsupported-language manifest.
    unsupported_owners: set[str] = field(default_factory=set)
    # Coverage signal: count of local-looking imports that did NOT resolve to an owned
    # file. checks are pure (CheckContext)->list[Issue], so the count is threaded here (the
    # engine reads it after running checks) rather than returned. Only check_boundary writes it.
    unresolved_local_imports: int = 0
    # Files with a generated-code marker. The engine reads this from source only when it parses a
    # file, then caches it so quick validation can skip generated exports without source rereads.
    generated_files: set[str] = field(default_factory=set)

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

    def ts_aliases(self) -> "tsconfig.TsAliases | None":
        """The project's tsconfig path aliases (loaded once from ``project_root``), or None.

        Lets import resolution follow ``@/…`` / ``baseUrl`` imports the same way ``discover``/
        ``calibrate``/``where`` do, so boundary checks see the same edge set those commands wrote.
        """
        cached = getattr(self, "_ts_aliases", _UNSET)
        if cached is _UNSET:
            cached = tsconfig.load(self.project_root)
            self._ts_aliases = cached
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


def _is_ts_like(importer_rel: str) -> bool:
    """True when the importing file is TS/JS — only then do tsconfig path aliases apply."""
    return posixpath.splitext(importer_rel)[1] in _TS_IMPORTER_EXTS


def _candidate_stems(
    importer_rel: str, module: str, aliases: "tsconfig.TsAliases | None" = None
) -> list[str]:
    """Possible extension-less path stems a module specifier could resolve to.

    ``aliases`` (a project's tsconfig ``baseUrl``/``paths``) only contributes for *bare* specifiers
    imported from a TS/JS file — e.g. ``@/common`` → ``src/common`` — and is tried before the raw
    fallback. A relative specifier never aliases; a Python importer never consults tsconfig.
    """
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
        # The two relative-import dialects disagree on what a "." means in `rest`, and conflating
        # them silently drops most TS edges in large backends:
        #   * TS/JS — `rest` is already a filesystem path ("./auth.service" -> rest "/auth.service").
        #     Dots belong to the *filename* (`auth.service.ts`); splitting them yields the bogus
        #     stem `.../auth/service`, which never matches. A TS specifier always has a slash after
        #     its leading dots, so `rest` starts with "/".
        #   * Python — `rest` is dotted package notation ("..models" -> rest "models",
        #     "..a.b" -> "a.b"); here a "." IS the separator and must become "/". No slash present.
        if rest.startswith("/"):
            rest_path = rest.strip("/")
        else:
            rest_path = rest.replace(".", "/").strip("/")
        stem = normpath(posixpath.join(up, rest_path)) if rest_path else normpath(up or ".")
        return [] if stem in (".", "", "/") else [stem]
    # Bare dotted (Python "a.b.c") or bare package (TS "react", "@scope/pkg", "@/alias").
    candidates: list[str] = []
    if aliases is not None and _is_ts_like(importer_rel):
        candidates.extend(aliases.candidate_stems(module))
    candidates.append(module.replace(".", "/"))
    seen: set[str] = set()
    return [c for c in candidates if c and not (c in seen or seen.add(c))]


def resolve_import(
    importer_rel: str,
    module: str,
    known_noext: dict[str, str],
    suffix_index: dict[str, str] | None = None,
    aliases: "tsconfig.TsAliases | None" = None,
) -> str | None:
    """Resolve an import specifier to a known extracted file path, or None if external/ambiguous.

    ``suffix_index`` (from :func:`build_suffix_index`) backs the trailing-segment fallback in
    ``O(1)``; when omitted it is built on demand so ad-hoc callers stay correct. ``aliases`` (a
    project's tsconfig ``baseUrl``/``paths``) lets a bare TS specifier like ``@/common`` resolve;
    when omitted, only relative + exact + suffix resolution apply (the prior behavior). Resolution
    order per candidate stem: exact stem, then package ``/index``/``/__init__``, then the smallest
    stem ending in that segment suffix.
    """
    if suffix_index is None:
        suffix_index = build_suffix_index(known_noext)
    for stem in _candidate_stems(importer_rel, module, aliases):
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
_DRIFT_SAMPLE_CAP = 5  # token-lean: a rolled-up drift issue names at most this many symbols, then "+N more"
_ORPHAN_SAMPLE_CAP = 5  # same, for the rolled-up per-subsystem orphan-export issue


def _capped_sample(items: list[str], cap: int) -> str:
    """Render a rolled-up issue's sample: the first ``cap`` names, then ``+N more`` (token-lean).

    Shared by the drift and orphan rollups so both format their sample identically.
    """
    sample = ", ".join(items[:cap])
    extra = len(items) - cap
    return sample + (f", +{extra} more" if extra > 0 else "")


def check_structural_drift(ctx: CheckContext) -> list[Issue]:
    issues: list[Issue] = []
    for name in sorted(ctx.subsystems):
        sub = ctx.subsystems[name]
        declared = sub.expose_names()
        actual = ctx.actual_exports(name)
        schema = ctx.schema_tables(name)
        # A subsystem owning an UNSUPPORTED-language file (Go/Rust/Java/…) has exposes Bounds can't
        # verify — it has no adapter, extracted nothing, so a declared-but-absent expose is NOT
        # proven-stale drift. Skip the declared-but-missing branch for it (consistent with calibrate,
        # which routes the same exposes to needs_review rather than remove). Undeclared *actual*
        # exports can't arise here either (nothing was extracted), so the second branch is unaffected.
        owns_unsupported = name in ctx.unsupported_owners
        for missing in sorted(declared - actual):
            if owns_unsupported:
                continue  # unverifiable hand-authored expose for an unparseable language — never drift
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
            # BOUNDS-015: a test case (``test_*`` function / ``Test*`` class in a test file) is
            # intentionally kept OUT of a subsystem's exposes (discover._exposes_for does the same,
            # BOUNDS-014). It must therefore not be re-reported here as an "undeclared export", or a
            # repo with a tests subsystem floods with hundreds of info drifts — the exact noise
            # BOUNDS-014 removed. Excluded symmetrically, by the shared is_test_* predicates.
            test_case_exports = {
                s.name
                for p in ctx.files_of(name) if is_test_file(p)
                for s in ctx.extracts[p].symbols
                if is_test_symbol(s.name, s.kind)
            }
            # BOUNDS-016: a Next.js framework entry file (page/layout/route/… under app/ or pages/)
            # exports symbols the framework invokes by convention (the default component, GET/POST
            # handlers, route-segment config). Nothing imports them — they are not a consumable
            # surface — so, like test cases, exclude the whole file's exports from undeclared-export
            # drift (discover._exposes_for omits them too, keeping the two symmetric). This is what
            # stops a real Next.js app from flooding with hundreds of framework-callback "drifts".
            framework_exports = {
                s.name
                for p in ctx.files_of(name) if is_framework_entry_file(p)
                for s in ctx.extracts[p].symbols if s.exported
            }
            generated_exports = {
                s.name
                for p in ctx.files_of(name) if p in ctx.generated_files
                for s in ctx.extracts[p].symbols if s.exported
            }
            extras = [
                extra
                for extra in sorted(
                    actual - declared - test_case_exports - framework_exports - generated_exports
                )
                if extra not in declared_table_parents
            ]
            # Roll the per-symbol undeclared-export drift into ONE info issue per subsystem: on a large
            # repo this is the dominant agent-context bloat (one issue per symbol = thousands of tokens
            # a `bounds validate --quick` dumps after every edit). `count` carries the true magnitude so
            # overview's drift tally is unchanged; the message names a capped sample. Severity stays
            # info (never blocks). The declared-but-missing branch above stays per-item — it is
            # error-severity, gate-relevant, and usually few.
            if extras:
                n = len(extras)
                sample = _capped_sample(extras, _DRIFT_SAMPLE_CAP)
                issues.append(
                    _issue(
                        errors.E_STRUCTURAL_DRIFT,
                        f"subsystem '{name}' exports {n} symbol(s) not declared in exposes: {sample}",
                        severity="info",
                        subsystem=name,
                        count=n,
                        fix=f"add them to {name}.exposes if part of the public surface, "
                        "or run `bounds calibrate` to sync the manifest",
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
    aliases = ctx.ts_aliases()
    for name in sorted(ctx.subsystems):
        for rel in ctx.files_of(name):
            if is_test_file(rel):
                continue
            result = ctx.extracts[rel]
            for imp in result.imports:
                target = resolve_import(rel, imp.module, known, suffix_index, aliases)
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
    all_cycles = _find_cycles(graph)
    if not all_cycles:
        return []

    issues: list[Issue] = []
    # If the graph is a bowl of spaghetti, reporting 1,000 individual cycles is a flood, not
    # helpful context. Report the shortest 10, then roll the rest into a summary that names
    # the most frequent bottleneck edges (the ones whose removal breaks the most cycles).
    reported_cycles = all_cycles[:10]
    for cycle in reported_cycles:
        chain = " -> ".join(cycle + [cycle[0]])
        issues.append(
            _issue(
                errors.E_CYCLE_DETECTED,
                f"circular dependency: {chain}",
                subsystem=cycle[0],
                fix="break the cycle via an interface/inversion, or move shared code into a library subsystem",
            )
        )

    if len(all_cycles) > 10:
        edge_counts: dict[tuple[str, str], int] = {}
        for cyc in all_cycles:
            for i in range(len(cyc)):
                u, v = cyc[i], cyc[(i + 1) % len(cyc)]
                edge_counts[(u, v)] = edge_counts.get((u, v), 0) + 1

        # Take the top 3 bottleneck edges
        bottlenecks = sorted(edge_counts.items(), key=lambda x: x[1], reverse=True)[:3]
        bottleneck_text = ", ".join(f"'{u}->{v}' ({count})" for (u, v), count in bottlenecks)

        issues.append(
            _issue(
                errors.E_CYCLE_DETECTED,
                f"... and {len(all_cycles) - 10} more cycles; top bottlenecks: {bottleneck_text}",
                count=len(all_cycles) - 10,
                fix="Break a bottleneck edge to resolve many cycles at once; see docs/troubleshooting-ci.md",
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
    # Subsystems for which at least one consumer declared the *specific* interfaces it uses. Orphan
    # detection compares an export against this interface-level data; without it (e.g. the
    # subsystem-granularity edges `discover` emits) every public export of a library would read as
    # orphaned — it is consumed by external users, not a sibling subsystem. So we only judge orphans
    # for subsystems that actually have interface-level consumption recorded (curated contracts).
    iface_tracked: set[str] = set()
    for sub in ctx.subsystems.values():
        for c in sub.consumes:
            if c.interfaces:
                iface_tracked.add(c.subsystem)
            for iface in c.interfaces:
                consumed.add((c.subsystem, iface))

    issues: list[Issue] = []
    for name in sorted(ctx.subsystems):
        sub = ctx.subsystems[name]
        if ctx.role_exposes_orphans(name):  # service-like roles expose unconsumed entrypoints
            continue
        if name not in iface_tracked:  # no interface-level consumption to judge against — skip
            continue
        orphans = [i for i in sorted(sub.expose_names()) if (name, i) not in consumed]
        if not orphans:
            continue
        # Roll up per subsystem (same pattern as the drift rollup above): a library public surface
        # consumed only externally would otherwise emit ONE issue per export — calibrate enriching
        # discover's bare consumes edges to interface level (intended) flips this check on, and a
        # 100-export module then floods `validate` with 100 rows after every calibrate. One issue
        # carrying the true magnitude in `count` + a capped sample keeps the signal (and overview's
        # tally) intact while killing the per-symbol flood. The fix still points at the resolution.
        n = len(orphans)
        sample = _capped_sample(orphans, _ORPHAN_SAMPLE_CAP)
        plural = "s" if n != 1 else ""
        issues.append(
            _issue(
                errors.E_ORPHAN_EXPORT,
                f"{n} interface{plural} exposed by '{name}' {'are' if n != 1 else 'is'} "
                f"consumed by no subsystem: {sample}",
                subsystem=name,
                count=n,
                fix=f"trim unused exports from {name}.exposes, "
                f"or mark '{name}' as a service entrypoint if its API is consumed externally",
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
# Check 8 — adapter output contracts (advisory; warnings only, never blocks)
# ===========================================================================
def check_adapter_contracts(ctx: CheckContext) -> list[Issue]:
    """Run each adapter's self-consistency contract against its own extracted output.

    A pure regression guard (zero LLM; no tree-sitter parse, no filesystem read — it
    only inspects already-built ``ExtractResult``s): for every extracted file it resolves
    the owning adapter by extension and asks it to validate its own output. Catches the
    class of bug that adapter logic alone can silently regress on — a Prisma relation
    field leaking in as a column, or an all-unparsable SQL migration whose revision header
    masked the failure. Always advisory (``E_ADAPTER_CONTRACT``
    is a warning in ``errors.SEVERITY``), so it never changes exit codes.
    """
    issues: list[Issue] = []
    for rel in sorted(ctx.extracts):
        adapter = get_adapter(rel)
        if adapter is None:
            continue
        for issue in adapter.check_contract(ctx.extracts[rel]):
            if issue.subsystem is None:
                issue.subsystem = ctx.file_owner.get(rel)
            issues.append(issue)
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
    check_adapter_contracts,
]

CHECKS_BY_MODE = {
    "quick": [check_structural_drift, check_cross_impact, check_schema, check_adapter_contracts],
    "full": list(_ALL),
    "preflight": list(_ALL),
    "audit": list(_ALL),
    "hotfix": [],
}
