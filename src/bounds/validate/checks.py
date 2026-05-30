"""The 6 structural checks + the CheckContext they run against + the per-mode dispatch table.

Every check is a pure function ``(CheckContext) -> list[Issue]``. None of them read the filesystem or
call tree-sitter — they operate on the already-extracted results and the loaded manifests. All produced
issues use their natural severity; the engine downgrades errors to warnings for ``quick`` mode.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from posixpath import normpath

from .. import errors
from ..models import ExtractResult, Issue, RootManifest, SubsystemCompact


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

    def files_of(self, subsystem: str) -> list[str]:
        """Extracted files owned by ``subsystem`` (sorted, only those present in extracts)."""
        return sorted(
            p for p, owner in self.file_owner.items() if owner == subsystem and p in self.extracts
        )

    def actual_exports(self, subsystem: str) -> set[str]:
        """Union of exported symbol names across the subsystem's extracted files."""
        out: set[str] = set()
        for p in self.files_of(subsystem):
            out |= self.extracts[p].exported_names()
        return out

    def all_symbol_names(self, subsystem: str) -> set[str]:
        """All symbol names (exported or not) the subsystem actually defines."""
        out: set[str] = set()
        for p in self.files_of(subsystem):
            out |= {s.name for s in self.extracts[p].symbols}
        return out

    def known_noext(self) -> dict[str, str]:
        """Map of extension-stripped path -> real rel path, for import resolution (cached)."""
        cached = getattr(self, "_known_noext", None)
        if cached is None:
            cached = {}
            for rel in sorted(self.extracts):
                cached.setdefault(_strip_ext(rel), rel)
            self._known_noext = cached
        return cached

    def role_exposes_orphans(self, subsystem: str) -> bool:
        """True if the subsystem's role legitimately exposes unconsumed entrypoints (s-17)."""
        sub = self.subsystems.get(subsystem)
        if sub is None:
            return False
        registry = self.root.role_registry()
        return bool(registry.get(sub.role, {}).get("orphan_exposes", False))

    def is_unbounded_criticality(self, subsystem: str) -> bool:
        """True if the subsystem's criticality propagates unbounded (depth -1), e.g. 'core' (s-17)."""
        sub = self.subsystems.get(subsystem)
        if sub is None:
            return False
        return self.root.criticality_registry().get(sub.criticality, 0) == -1


# ===========================================================================
# Import resolution helpers (best-effort; never produces false positives by guessing)
# ===========================================================================
def _strip_ext(rel: str) -> str:
    suffix = PurePosixPath(rel).suffix
    return rel[: -len(suffix)] if suffix else rel


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


def resolve_import(importer_rel: str, module: str, known_noext: dict[str, str]) -> str | None:
    """Resolve an import specifier to a known extracted file path, or None if it's external/ambiguous."""
    for stem in _candidate_stems(importer_rel, module):
        if stem in known_noext:
            return known_noext[stem]
        for suffix in ("/index", "/__init__"):
            if stem + suffix in known_noext:
                return known_noext[stem + suffix]
        for noext in sorted(known_noext):
            if noext == stem or noext.endswith("/" + stem):
                return known_noext[noext]
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
        for missing in sorted(declared - actual):
            issues.append(
                Issue(
                    errors.E_STRUCTURAL_DRIFT,
                    "error",
                    f"subsystem '{name}' declares '{missing}' in exposes but no source file exports it",
                    subsystem=name,
                    fix=f"remove '{missing}' from {name}.exposes, or export it from {sub.paths or ['its sources']}",
                )
            )
        # Undeclared public surface on an unbounded-criticality (core-like) subsystem is info only.
        if ctx.is_unbounded_criticality(name) and declared:
            for extra in sorted(actual - declared):
                issues.append(
                    Issue(
                        errors.E_STRUCTURAL_DRIFT,
                        "info",
                        f"subsystem '{name}' exports '{extra}' which is not declared in exposes",
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
    for name in sorted(ctx.subsystems):
        for rel in ctx.files_of(name):
            result = ctx.extracts[rel]
            for imp in result.imports:
                target = resolve_import(rel, imp.module, known)
                if not target:
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
                            Issue(
                                errors.E_BOUNDARY_VIOLATION,
                                "error",
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
                    Issue(
                        errors.E_UNRESOLVED_REFERENCE,
                        "warning",
                        f"subsystem '{name}' consumes unknown subsystem '{c.subsystem}'",
                        subsystem=name,
                        fix=f"create subsystem '{c.subsystem}', or fix the reference in {name}.consumes",
                    )
                )
                continue
            exposed = provider.expose_names()
            for iface in sorted(c.interfaces):
                if iface not in exposed:
                    issues.append(
                        Issue(
                            errors.E_CONTRACT_MISSING_EXPORT,
                            "error",
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
            Issue(
                errors.E_STALE_INTERFACE,
                "error",
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
            Issue(
                errors.E_CYCLE_DETECTED,
                "error",
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
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}
    stack: list[str] = []
    seen: set[tuple[str, ...]] = set()
    cycles: list[list[str]] = []

    def dfs(u: str) -> None:
        color[u] = GRAY
        stack.append(u)
        for v in graph.get(u, []):
            cv = color.get(v, WHITE)
            if cv == GRAY:
                cyc = _rotate_min(stack[stack.index(v):])
                key = tuple(cyc)
                if key not in seen:
                    seen.add(key)
                    cycles.append(cyc)
            elif cv == WHITE:
                dfs(v)
        color[u] = BLACK
        stack.pop()

    for n in sorted(graph):
        if color[n] == WHITE:
            dfs(n)
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
                    Issue(
                        errors.E_ORPHAN_EXPORT,
                        "warning",
                        f"interface '{iface}' exposed by '{name}' is consumed by no subsystem",
                        subsystem=name,
                        fix=f"remove '{iface}' from {name}.exposes if unused, "
                        f"or mark '{name}' as a service entrypoint",
                    )
                )
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
]

CHECKS_BY_MODE = {
    "quick": [check_structural_drift, check_cross_impact],
    "full": list(_ALL),
    "preflight": list(_ALL),
    "audit": list(_ALL),
    "hotfix": [],
}
