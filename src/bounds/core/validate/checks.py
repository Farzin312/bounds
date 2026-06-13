"""The 8 checks (six structural + schema health + adapter output contracts), their context, and mode dispatch.

Every check is a pure function ``(CheckContext) -> list[Issue]``. None of them read the filesystem or
call tree-sitter — they operate on the already-extracted results and the loaded manifests. All produced
issues use their natural severity; the engine downgrades errors to warnings for ``quick`` mode.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from posixpath import normpath

from ...shared import errors, tsconfig
from ..extract import get_adapter
from ..extract.scan import is_framework_entry_file, is_test_file, is_test_symbol, strip_ext
from ...shared.models import ExtractResult, Issue, RootManifest, SubsystemCompact
from .schema import SCHEMA_LANGUAGES, _fold_subsystem_schema, schema_diagnostics

__all__ = ["CheckContext", "check_cycles", "current_cycle_keys", "index_extracts", "resolve_import"]

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
    # Canonical keys of subsystem-level cycles already accepted in .bounds/cycle-baseline.json. A
    # cycle whose key is here is known/accepted: it is reported (suppressed, non-blocking) so the
    # gate fails only on NEW cycles a branch introduces — parity with the drift baseline. Empty ⇒
    # no baseline committed ⇒ every real cycle is reported normally (opt-in, like the surface baseline).
    cycle_baseline: set[str] = field(default_factory=set)

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
                if extra not in declared_table_parents and not extra.startswith("_")
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
# How many individual cycle paths to surface before collapsing the rest into a root-cause summary.
_CYCLE_SAMPLE_CAP = 10
# How many root edges to name in the minimal-feedback-arc-set summary before truncating.
_CYCLE_CUT_CAP = 15


def _literal_prefix(pattern: str) -> str:
    """The fixed directory/file prefix of a repo-relative posix path glob, glob tail removed.

    ``src/auth/**`` -> ``src/auth``; ``src/foo.ts`` -> ``src/foo.ts``; ``*`` / ``.`` -> ``""``.
    Used to reason about which subsystem's declared paths nest inside another's.
    """
    head = re.split(r"[*?\[]", pattern, maxsplit=1)[0].rstrip("/")
    return "" if head in (".", "") else head


def _subsystem_prefixes(sub: SubsystemCompact) -> list[str]:
    """Literal path prefixes a subsystem declares (from ``paths`` + ``files``), glob tails removed."""
    out: list[str] = []
    for p in list(sub.paths) + list(sub.files):
        lp = _literal_prefix(p)
        if lp:
            out.append(lp)
    return out


def _strictly_under(inner: str, outer: str) -> bool:
    """True when posix path ``inner`` is a strict descendant of directory ``outer``."""
    return bool(outer) and inner != outer and inner.startswith(outer + "/")


def build_containment(subsystems: dict[str, SubsystemCompact]) -> dict[str, set[str]]:
    """Map each subsystem -> the set of subsystems whose declared path tree it strictly *contains*.

    A subsystem nested inside another's declared path (e.g. ``src/auth/guards`` inside ``src/auth``)
    is a contained child, not a peer. Parent↔child imports are normal intra-module layering
    (a module file importing its own subdirectory and vice-versa), never an architectural cycle —
    the manifest schema models them as siblings, so without this they read as false cycles.

    Containment is derived from path nesting and can be overridden/augmented with an explicit
    ``parent:`` declaration. Interleaved paths (each strictly under the other) are treated as an
    overlap, not containment, and are left for ``E_SUBSYSTEM_OVERLAP`` to report.
    """
    names = sorted(subsystems)
    prefixes = {n: _subsystem_prefixes(subsystems[n]) for n in names}
    contains: dict[str, set[str]] = {n: set() for n in names}

    for outer in names:
        opre = prefixes[outer]
        if not opre:
            continue
        for inner in names:
            if inner == outer:
                continue
            ipre = prefixes[inner]
            if not ipre:
                continue
            inner_under_outer = all(any(_strictly_under(i, o) for o in opre) for i in ipre)
            outer_under_inner = all(any(_strictly_under(o, i) for i in ipre) for o in opre)
            if inner_under_outer and not outer_under_inner:
                contains[outer].add(inner)

    # Explicit `parent:` declarations override/augment path-based detection (loader has already
    # validated that the referenced parent exists and the chain is acyclic).
    for child in names:
        declared = (subsystems[child].parent or "").strip()
        if declared and declared in contains and declared != child:
            contains[declared].add(child)
    return contains


def _is_containment_pair(u: str, v: str, contains: dict[str, set[str]]) -> bool:
    """True when ``u`` and ``v`` are in a parent↔child containment relationship (either direction)."""
    return v in contains.get(u, ()) or u in contains.get(v, ())


def _is_containment_cycle(cycle: list[str], contains: dict[str, set[str]]) -> bool:
    """True when *every* adjacent pair in a cycle is a containment pair — i.e. the whole cycle lives
    inside a single nesting chain and is intra-module layering, not a real architectural cycle. A
    cycle with even one cross-subsystem (non-nesting) edge is genuine and is kept."""
    n = len(cycle)
    for i in range(n):
        if not _is_containment_pair(cycle[i], cycle[(i + 1) % n], contains):
            return False
    return True


def _count_sccs(real_cycles: list[list[str]]) -> int:
    """Number of strongly-connected components (independent tangles) over the subgraph induced by
    the surviving real cycles. Iterative Tarjan so a deep tangle can't raise ``RecursionError``."""
    adj: dict[str, set[str]] = {}
    for cyc in real_cycles:
        n = len(cyc)
        for i in range(n):
            adj.setdefault(cyc[i], set()).add(cyc[(i + 1) % n])
            adj.setdefault(cyc[(i + 1) % n], set())
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    scc_count = 0
    for start in sorted(adj):
        if start in index_of:
            continue
        work: list[tuple[str, object]] = [(start, iter(sorted(adj[start])))]
        index_of[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index_of:
                    index_of[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(sorted(adj[nxt]))))
                    advanced = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index_of[nxt])
            if advanced:
                continue
            if low[node] == index_of[node]:
                scc_count += 1
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    if w == node:
                        break
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return scc_count


def _greedy_feedback_edges(
    real_cycles: list[list[str]], contains: dict[str, set[str]]
) -> list[tuple[tuple[str, str], int]]:
    """A ranked minimal feedback-arc-set: the smallest set of edges whose removal breaks *every*
    cycle, greedily chosen most-impactful-first. Returns ``[((u, v), breaks_count), ...]`` where
    ``breaks_count`` is how many still-unbroken cycles that single edge removal resolves.

    Containment-pair edges are never candidates — a module importing its own subdirectory is not a
    cuttable architectural dependency. Every real cycle has at least one non-containment edge (else
    it was filtered out as a containment artifact), so the cut set still covers them all.
    """
    cycle_edges: list[frozenset[tuple[str, str]]] = []
    for cyc in real_cycles:
        n = len(cyc)
        cycle_edges.append(frozenset((cyc[i], cyc[(i + 1) % n]) for i in range(n)))
    covered = [False] * len(cycle_edges)
    cut: list[tuple[tuple[str, str], int]] = []
    while True:
        counts: dict[tuple[str, str], int] = {}
        for idx, edges in enumerate(cycle_edges):
            if covered[idx]:
                continue
            for e in edges:
                if _is_containment_pair(e[0], e[1], contains):
                    continue
                counts[e] = counts.get(e, 0) + 1
        if not counts:
            break
        # Highest participation wins; ties break on the (u, v) tuple for determinism.
        best = max(sorted(counts), key=lambda e: counts[e])
        breaks = 0
        for idx, edges in enumerate(cycle_edges):
            if not covered[idx] and best in edges:
                covered[idx] = True
                breaks += 1
        cut.append((best, breaks))
    return cut


# Tab joins a canonical (min-rotated) cycle's nodes into a stable identity key. Tab never appears
# in a subsystem name, so keys never collide — same convention as the drift baseline.
_CYCLE_KEY_SEP = "\t"


def cycle_key(cycle: list[str]) -> str:
    """Stable identity for a cycle: its canonical (min-first) node order, tab-joined."""
    return _CYCLE_KEY_SEP.join(_rotate_min(list(cycle)))


def _real_cycles(subsystems: dict[str, SubsystemCompact]) -> list[list[str]]:
    """The genuine (containment-filtered) subsystem cycles — the single definition shared by the
    cycle check and the cycle-baseline dump, so a baselined key always matches a reported cycle."""
    graph = {
        name: sorted({c.subsystem for c in sub.consumes if c.subsystem in subsystems})
        for name, sub in subsystems.items()
    }
    contains = build_containment(subsystems)
    return [c for c in _find_cycles(graph) if not _is_containment_cycle(c, contains)]


def current_cycle_keys(subsystems: dict[str, SubsystemCompact]) -> list[str]:
    """Sorted canonical keys of every real cycle — what `calibrate --dump-baseline` records."""
    return sorted(cycle_key(c) for c in _real_cycles(subsystems))


def check_cycles(ctx: CheckContext) -> list[Issue]:
    graph = {
        name: sorted({c.subsystem for c in sub.consumes if c.subsystem in ctx.subsystems})
        for name, sub in ctx.subsystems.items()
    }
    all_cycles = _find_cycles(graph)
    if not all_cycles:
        return []

    # Drop parent↔child containment artifacts: a cycle entirely inside one nesting chain is
    # intra-module layering (the manifest models a nested subsystem as a peer), not a real cycle.
    contains = build_containment(ctx.subsystems)
    real_cycles = [c for c in all_cycles if not _is_containment_cycle(c, contains)]
    if not real_cycles:
        return []

    # Cycle baseline: a committed set of accepted cycles. Known cycles are reported once as a
    # suppressed (non-blocking) rollup; only NEW cycles drive the gate — so a repo can hard-gate
    # regressions without first clearing pre-existing cycle debt (parity with the drift baseline).
    issues: list[Issue] = []
    if ctx.cycle_baseline:
        new_cycles = [c for c in real_cycles if cycle_key(c) not in ctx.cycle_baseline]
        known = len(real_cycles) - len(new_cycles)
        if known:
            issues.append(
                _issue(
                    errors.E_CYCLE_DETECTED,
                    f"{known} known cycle{'s' if known != 1 else ''} accepted by "
                    f".bounds/cycle-baseline.json (suppressed)",
                    severity="info",
                    count=known,
                    fix="re-dump the baseline (`bounds calibrate --dump-baseline`) after intentionally "
                    "changing the accepted cycle set",
                )
            )
            issues[-1].suppressed = True
            issues[-1].note = "baselined cycle (accepted debt)"
        real_cycles = new_cycles
        if not real_cycles:
            return issues

    # Surface the shortest few cycles individually for direct context. If the graph is a tangle,
    # a flood of individual paths is unactionable — collapse the rest into a root-cause summary:
    # the minimal set of edges whose removal breaks every remaining cycle, ranked by impact.
    reported_cycles = real_cycles[:_CYCLE_SAMPLE_CAP]
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

    if len(real_cycles) > _CYCLE_SAMPLE_CAP:
        cut = _greedy_feedback_edges(real_cycles, contains)
        n_scc = _count_sccs(real_cycles)
        shown = cut[:_CYCLE_CUT_CAP]
        cut_text = ", ".join(f"'{u}->{v}' (breaks {breaks})" for (u, v), breaks in shown)
        if len(cut) > _CYCLE_CUT_CAP:
            cut_text += f", … (+{len(cut) - _CYCLE_CUT_CAP} more)"
        tangles = f"{n_scc} strongly-connected component{'s' if n_scc != 1 else ''}"
        issues.append(
            _issue(
                errors.E_CYCLE_DETECTED,
                f"… and {len(real_cycles) - len(reported_cycles)} more cycles across {tangles}; "
                f"these {len(cut)} root edge{'s' if len(cut) != 1 else ''} break all of them: {cut_text}",
                count=len(real_cycles) - len(reported_cycles),
                fix="Cut a ranked root edge (dependency-invert it or move shared code to a library "
                "subsystem) to resolve many cycles at once; see docs/troubleshooting-ci.md",
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
# Check 5b — composition-root detection (advisory; never blocks)
# ===========================================================================
# A subsystem must fan out to AND fan in from at least this fraction of the other subsystems to be
# flagged. High on both axes is the catch-all signature; the threshold keeps a normal hub (a shared
# library many things import, or a top-level app that imports many things, but not both) from firing.
_COMPOSITION_ROOT_RATIO = 0.5
# Below this many subsystems the ratios are too noisy to be meaningful (a 3-subsystem repo where one
# imports the other two and is imported by them is just small, not a catch-all).
_COMPOSITION_ROOT_MIN_SUBSYSTEMS = 8


def check_composition_root(ctx: CheckContext) -> list[Issue]:
    """Flag a catch-all subsystem that is simultaneously a high fan-out importer and high fan-in
    target — a DI/HTTP composition root fused with a pile of shared leaf utilities. Such a subsystem
    forms a cycle with essentially every sibling and costs a painful manual split on first adoption
    (the audit's ``src`` catch-all). Advisory only; the fix points at the deterministic remedy."""
    subsystems = ctx.subsystems
    n = len(subsystems)
    if n < _COMPOSITION_ROOT_MIN_SUBSYSTEMS:
        return []
    others = n - 1
    # fan-out: distinct sibling subsystems this one consumes; fan-in: distinct siblings that consume it.
    fan_out: dict[str, set[str]] = {name: set() for name in subsystems}
    fan_in: dict[str, set[str]] = {name: set() for name in subsystems}
    for name, sub in subsystems.items():
        for c in sub.consumes:
            if c.subsystem in subsystems and c.subsystem != name:
                fan_out[name].add(c.subsystem)
                fan_in[c.subsystem].add(name)

    threshold = _COMPOSITION_ROOT_RATIO * others
    issues: list[Issue] = []
    for name in sorted(subsystems):
        out_n, in_n = len(fan_out[name]), len(fan_in[name])
        if out_n >= threshold and in_n >= threshold:
            issues.append(
                _issue(
                    errors.E_COMPOSITION_ROOT,
                    f"subsystem '{name}' is both a high fan-out importer ({out_n}/{others} subsystems) "
                    f"and a high fan-in target ({in_n}/{others}) — the catch-all signature that fuses a "
                    f"composition root with shared leaf utilities",
                    subsystem=name,
                    fix=f"declare '{name}' an entry_point in root.yaml (a source-only composition root), "
                    f"and split its shared leaf directories into their own subsystems so siblings depend "
                    f"on the leaves, not the root — this clears the cycles it forms with everything",
                )
            )
    return issues


# ===========================================================================
# Check 6 — orphan detection
# ===========================================================================
def _framework_entry_exports(ctx: CheckContext, subsystem: str) -> set[str]:
    """Exported symbol names a framework invokes directly (NestJS ``@Controller``/``@Resolver``).

    Read from the extracted symbols' ``framework_entry`` metadata tag (set by the TS adapter), so the
    orphan check can treat an HTTP/GraphQL entrypoint as externally consumed rather than orphaned.
    """
    out: set[str] = set()
    for rel in ctx.files_of(subsystem):
        for sym in ctx.extracts[rel].symbols:
            if sym.exported and (sym.metadata or {}).get("framework_entry"):
                out.add(sym.name)
    return out


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
        # Framework-invoked entrypoints (a NestJS @Controller/@Resolver class) are consumed by the
        # framework's routing/DI layer, not by a sibling subsystem's static import — so they are
        # never true orphans, exactly like a Next.js route file's exports. Exempt them here.
        framework_entries = _framework_entry_exports(ctx, name)
        orphans = [
            i for i in sorted(sub.expose_names())
            if (name, i) not in consumed and i not in framework_entries
        ]
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
                                 fix=_SCHEMA_FIX_HINTS[code]))
    return issues


# Per-code fix hints. The order/unparsed advisories have different remedies — conflating them
# (the old single hint that told everyone to "add a numeric filename prefix") was misleading for a
# perfectly-named migration whose only "problem" is a dialect body tree-sitter can't parse.
_SCHEMA_FIX_HINTS = {
    errors.E_SCHEMA_UNPARSED: (
        "a statement uses SQL the bundled tree-sitter-sql grammar can't parse — usually a "
        "procedural PL/pgSQL body ($$…$$ / DO block) or a vendor extension. The file's "
        "parseable DDL still folded, so no action is needed unless a real table/column was "
        "lost; if so, move the table DDL out of the procedural body (or simplify the statement). "
        "This is not a filename/order problem."
    ),
    errors.E_SCHEMA_NO_ORDER: (
        "give the migrations a deterministic order: add a numeric filename prefix, a "
        "revision/down_revision header, or a '-- bounds:order N' comment"
    ),
}


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
    check_composition_root,
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
