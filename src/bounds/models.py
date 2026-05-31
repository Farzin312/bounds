"""The Bounds data model.

Three tiers:
  * Manifest tier (declared, human-written): Interface, Consumes, SubsystemCompact, RootManifest
  * Extraction tier (deterministic, tree-sitter): Symbol, ImportRef, ExtractResult
  * Validation tier: Issue, ValidationReport

Every dataclass provides ``to_dict()`` for stable JSON output. Manifest types provide
``from_dict()`` for the loader; extraction types provide round-trip helpers for the cache.
All collections are sorted at serialization boundaries by the producers, not here, so that
JSON output is deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import config


# ===========================================================================
# Manifest tier (declared)
# ===========================================================================
@dataclass
class Interface:
    name: str
    kind: str = "unknown"  # function|class|const|type|interface|variable|unknown
    signature: str | None = None  # Tier-3 (LLM) enrichment; None in MVP
    internal: bool = False  # exempt from calibration add/remove (deliberately private)

    def to_dict(self) -> dict:
        d = {"name": self.name, "kind": self.kind}
        if self.signature is not None:
            d["signature"] = self.signature
        if self.internal:
            d["internal"] = True
        return d

    @classmethod
    def from_dict(cls, data) -> "Interface":
        # Accept either a bare string ("login") or a mapping ({name, kind, signature, internal}).
        if isinstance(data, str):
            return cls(name=data)
        return cls(
            name=str(data.get("name", "")),
            kind=str(data.get("kind", "unknown")),
            signature=data.get("signature"),
            internal=bool(data.get("internal", False)),
        )


@dataclass
class Consumes:
    subsystem: str
    via: str | None = None
    interfaces: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"subsystem": self.subsystem, "via": self.via, "interfaces": list(self.interfaces)}

    @classmethod
    def from_dict(cls, data) -> "Consumes":
        if isinstance(data, str):
            return cls(subsystem=data)
        return cls(
            subsystem=str(data.get("subsystem", "")),
            via=data.get("via"),
            interfaces=[str(i) for i in (data.get("interfaces") or [])],
        )


@dataclass
class SubsystemCompact:
    name: str
    role: str = "library"
    criticality: str = "leaf"
    description: str = ""
    namespace: str = ""
    paths: list[str] = field(default_factory=list)
    exposes: list[Interface] = field(default_factory=list)
    consumes: list[Consumes] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    consumed_by: list[str] = field(default_factory=list)  # AUTO-filled by loader
    source_path: str = ""

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "role": self.role,
            "criticality": self.criticality,
            "description": self.description,
            "paths": list(self.paths),
            "exposes": [i.to_dict() for i in self.exposes],
            "consumes": [c.to_dict() for c in self.consumes],
            "files": list(self.files),
            "consumed_by": sorted(self.consumed_by),
        }
        if self.namespace:
            d["namespace"] = self.namespace
        return d

    @classmethod
    def from_dict(cls, data: dict, source_path: str = "") -> "SubsystemCompact":
        return cls(
            name=str(data.get("name", "")),
            role=str(data.get("role", "library")),
            criticality=str(data.get("criticality", "leaf")),
            description=str(data.get("description", "")),
            namespace=str(data.get("namespace", "")),
            paths=[str(p) for p in (data.get("paths") or [])],
            exposes=[Interface.from_dict(e) for e in (data.get("exposes") or [])],
            consumes=[Consumes.from_dict(c) for c in (data.get("consumes") or [])],
            files=[str(f) for f in (data.get("files") or [])],
            source_path=source_path,
        )

    def expose_names(self) -> set[str]:
        return {i.name for i in self.exposes}


@dataclass
class RootManifest:
    version: str = "1"
    project: str = ""
    languages: list[str] = field(default_factory=list)
    enforce: str = "off"
    subsystems: list[str] = field(default_factory=list)
    entry_points: list[str] = field(default_factory=list)  # root-level bootstrap globs
    # Optional developer-defined schema extensions. Raw YAML mappings; resolved
    # lazily via role_registry()/criticality_registry(). Empty -> built-in enums apply.
    roles: dict = field(default_factory=dict)
    criticality: dict = field(default_factory=dict)
    source_path: str = ""

    def to_dict(self) -> dict:
        d = {
            "version": self.version,
            "project": self.project,
            "languages": list(self.languages),
            "enforce": self.enforce,
            "subsystems": list(self.subsystems),
            "entry_points": list(self.entry_points),
        }
        if self.roles:
            d["roles"] = self.roles
        if self.criticality:
            d["criticality"] = self.criticality
        return d

    @classmethod
    def from_dict(cls, data: dict, source_path: str = "") -> "RootManifest":
        return cls(
            version=str(data.get("version", "1")),
            project=str(data.get("project", "")),
            languages=[str(x) for x in (data.get("languages") or [])],
            enforce=str(data.get("enforce", "off")),
            subsystems=[str(s) for s in (data.get("subsystems") or [])],
            entry_points=[str(g) for g in (data.get("entry_points") or [])],
            roles=dict(data.get("roles") or {}),
            criticality=dict(data.get("criticality") or {}),
            source_path=source_path,
        )

    def role_registry(self) -> dict[str, dict]:
        """Resolve the valid roles -> their base behavior.

        With no custom ``roles:`` block, returns the four built-ins, each mapping to itself
        as base. With a custom block, returns *only* the declared roles (built-ins are not
        auto-included — the spec's "no two competing naming systems" rule); each custom role
        inherits its base's behavior (``extends:`` or ``type:``), overridable per-flag.
        """
        if not self.roles:
            return {
                name: {"base": name, **config.ROLE_BASE_BEHAVIOR[name]}
                for name in config.BUILTIN_ROLES
            }
        registry: dict[str, dict] = {}
        for name, spec in self.roles.items():
            spec = spec if isinstance(spec, dict) else {}
            base = str(spec.get("extends") or spec.get("type") or "").strip()
            behavior = dict(config.ROLE_BASE_BEHAVIOR.get(base, {"orphan_exposes": False}))
            if "orphan_exposes" in spec:
                behavior["orphan_exposes"] = bool(spec["orphan_exposes"])
            registry[str(name)] = {"base": base, **behavior}
        return registry

    def criticality_registry(self) -> dict[str, int]:
        """Resolve the valid criticality labels -> propagation depth.

        With no custom ``criticality:`` block, returns the built-in core/connector/leaf
        depths. With a custom block, each label's ``depth:`` integer drives propagation
        (-1 unbounded, 0 none, N hops).
        """
        if not self.criticality:
            return dict(config.BUILTIN_CRITICALITY_DEPTH)
        registry: dict[str, int] = {}
        for name, spec in self.criticality.items():
            depth = 0
            if isinstance(spec, dict) and "depth" in spec:
                try:
                    depth = int(spec["depth"])
                except (TypeError, ValueError):
                    depth = 0
            registry[str(name)] = depth
        return registry


# ===========================================================================
# Extraction tier (deterministic)
# ===========================================================================
@dataclass
class Symbol:
    name: str
    kind: str  # function|class|const|type|interface|variable|table|column|drop|rename|schema_meta|schema_error
    line: int
    exported: bool = True
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"name": self.name, "kind": self.kind, "line": self.line, "exported": self.exported}
        if self.metadata:
            d["metadata"] = dict(sorted(self.metadata.items()))
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Symbol":
        return cls(
            name=d["name"],
            kind=d.get("kind", "unknown"),
            line=int(d.get("line", 0)),
            exported=bool(d.get("exported", True)),
            metadata=dict(d.get("metadata") or {}),
        )


@dataclass
class ImportRef:
    module: str
    names: list[str] = field(default_factory=list)
    line: int = 0

    def to_dict(self) -> dict:
        return {"module": self.module, "names": list(self.names), "line": self.line}

    @classmethod
    def from_dict(cls, d: dict) -> "ImportRef":
        return cls(
            module=d["module"],
            names=[str(n) for n in (d.get("names") or [])],
            line=int(d.get("line", 0)),
        )


@dataclass
class ExtractResult:
    path: str
    language: str
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[ImportRef] = field(default_factory=list)
    content_hash: str = ""
    structure_hash: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "language": self.language,
            "symbols": [s.to_dict() for s in self.symbols],
            "imports": [i.to_dict() for i in self.imports],
            "content_hash": self.content_hash,
            "structure_hash": self.structure_hash,
            "error": self.error,
        }

    def exported_names(self) -> set[str]:
        return {s.name for s in self.symbols if s.exported}


# ===========================================================================
# Validation tier
# ===========================================================================
@dataclass
class Issue:
    code: str
    severity: str  # error|warning|info
    message: str
    subsystem: str | None = None
    file: str | None = None
    fix: str | None = None

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "subsystem": self.subsystem,
            "file": self.file,
            "fix": self.fix,
        }

    def sort_key(self) -> tuple:
        sev_rank = {"error": 0, "warning": 1, "info": 2}.get(self.severity, 3)
        return (sev_rank, self.code, self.subsystem or "", self.file or "", self.message)


@dataclass
class ValidationReport:
    status: str  # fresh|stale|unresolved
    mode: str
    ok: bool = True
    issues: list[Issue] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "validation_status": self.status,
            "mode": self.mode,
            "ok": self.ok,
            "issues": [i.to_dict() for i in sorted(self.issues, key=lambda x: x.sort_key())],
            # Sort stats keys so the serialized report is byte-stable regardless of insertion
            # order (determinism); values are already sorted by their producers.
            "stats": dict(sorted(self.stats.items())),
        }

    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]
