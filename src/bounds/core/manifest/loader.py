"""Discover the ``.bounds/`` directory and load root + subsystem manifests.

Discovery walks up from a starting directory looking for ``.bounds/root.yaml`` (or the
legacy ``.compact/root.yaml`` — see :func:`bounds.config.config_dir`). Loading reads YAML
with :func:`yaml.safe_load`, maps it onto the manifest dataclasses, and (for
:func:`load_all`) runs schema validation and back-fills the auto-derived ``consumed_by``
inverse of each subsystem's ``consumes`` edges.

Fatal cases — a missing root manifest, a YAML parse failure, or a missing subsystem
requested by name — raise :class:`BoundsError`. Within :func:`load_all`, a subsystem
that is declared in the root but cannot be loaded degrades to an ``Issue`` rather than
aborting the whole load.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ...shared import config, errors
from ...shared.models import Issue, RootManifest, SubsystemCompact
from . import schema

__all__ = ["find_root", "load_all", "load_root"]


def find_root(start: Path) -> Path | None:
    """Walk ``start`` and its parents for the first dir holding ``.bounds/root.yaml``.

    Returns the directory that *holds* ``.bounds`` (the project root), or ``None`` if no
    such ancestor exists. The legacy ``.compact/root.yaml`` is also accepted for backward
    compatibility, with ``.bounds`` preferred when both are present at the same ancestor.
    ``start`` may be a file or directory; its real directory is used.
    """
    start = Path(start)
    base = start if start.is_dir() else start.parent
    base = base.resolve()
    for candidate in (base, *base.parents):
        for dirname in (config.BOUNDS_DIR, config.LEGACY_DIR):
            if (candidate / dirname / config.ROOT_FILE).is_file():
                return candidate
    return None


def load_root(project_root: Path) -> RootManifest:
    """Load ``<project_root>/.bounds/root.yaml`` into a :class:`RootManifest`.

    Reads the legacy ``.compact/root.yaml`` instead when only it is present. Raises
    :class:`BoundsError` with ``E_MANIFEST_NOT_FOUND`` if the file is absent or
    ``E_MANIFEST_PARSE_ERROR`` if YAML parsing fails.
    """
    path = config.config_dir(project_root) / config.ROOT_FILE
    data = _load_yaml_mapping(
        path,
        not_found_code=errors.E_MANIFEST_NOT_FOUND,
        not_found_message=f"no root manifest at {path.as_posix()}",
        not_found_fix="run `bounds init --root` to scaffold .bounds/root.yaml",
    )
    return RootManifest.from_dict(data, source_path=path.as_posix())


def load_subsystem(project_root: Path, name: str) -> SubsystemCompact:
    """Load `.bounds/manifests/<name>.yaml` into a :class:`SubsystemCompact`.

    Raises :class:`BoundsError` with ``E_SUBSYSTEM_NOT_FOUND`` if the file is absent or
    ``E_MANIFEST_PARSE_ERROR`` if YAML parsing fails. The returned subsystem's ``name``
    defaults to ``name`` when the YAML omits it.
    """
    path = _subsystem_path(project_root, name)
    data = _load_yaml_mapping(
        path,
        not_found_code=errors.E_SUBSYSTEM_NOT_FOUND,
        not_found_message=f"no subsystem '{name}' at {path.as_posix()}",
        not_found_fix=f"run `bounds init --subsystem {name}` to scaffold it",
    )
    if not data.get("name"):
        data = {**data, "name": name}
    return SubsystemCompact.from_dict(data, source_path=path.as_posix())


def load_all(
    project_root: Path,
) -> tuple[RootManifest, dict[str, SubsystemCompact], list[Issue]]:
    """Load the root manifest and every subsystem it declares.

    Returns ``(root, {name: SubsystemCompact}, issues)``. The root manifest is validated
    via :func:`schema.validate_root`; each subsystem via :func:`schema.validate_subsystem`.
    A missing root still raises (fatal); a subsystem that cannot be loaded degrades to an
    ``E_SUBSYSTEM_NOT_FOUND``/``E_MANIFEST_PARSE_ERROR`` ``Issue`` and is skipped.

    After loading, ``consumed_by`` is back-filled: for each subsystem ``S`` and each
    ``consumes`` edge to a provider that exists, ``S.name`` is appended to the provider's
    ``consumed_by`` (kept sorted and unique).
    """
    project_root = Path(project_root)
    root = load_root(project_root)

    issues: list[Issue] = []
    issues.extend(_validate_root_yaml(project_root))

    # Resolved validity sets honour any custom roles/criticality declared in root.yaml.
    valid_roles = set(root.role_registry())
    valid_criticality = set(root.criticality_registry())

    subsystems: dict[str, SubsystemCompact] = {}
    for name in root.subsystems:
        path = _subsystem_path(project_root, name)
        raw = _read_yaml_or_issue(name, path, issues)
        if raw is None:
            continue
        issues.extend(schema.validate_subsystem(name, raw, valid_roles, valid_criticality))
        if not raw.get("name"):
            raw = {**raw, "name": name}
        subsystems[name] = SubsystemCompact.from_dict(raw, source_path=path.as_posix())

    _fill_consumed_by(subsystems)
    issues.extend(_validate_parents(subsystems))
    return root, subsystems, issues


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _subsystem_path(project_root: Path, name: str) -> Path:
    # Defense-in-depth (cli.init applies the same check before writing): the name is
    # interpolated into a path, so a traversal/separator name like `../../tmp/x` could read
    # an arbitrary `*.yaml` outside the manifests dir. Reject anything that isn't a safe
    # single segment so `describe <name>` / `calibrate --subsystem` stay confined.
    if not config.is_valid_subsystem_name(name):
        raise errors.BoundsError(
            errors.E_USAGE,
            f"invalid subsystem name {name!r}",
            fix="use only letters, digits, '-' and '_'",
        )
    return config.config_dir(project_root) / config.MANIFESTS_DIR / f"{name}.yaml"


def _load_yaml_mapping(
    path: Path,
    *,
    not_found_code: str,
    not_found_message: str,
    not_found_fix: str,
) -> dict:
    """Read and parse a YAML mapping file, raising :class:`BoundsError` on failure."""
    if not path.is_file():
        raise errors.BoundsError(not_found_code, not_found_message, fix=not_found_fix)
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except (OSError, yaml.YAMLError) as exc:
        raise errors.BoundsError(
            errors.E_MANIFEST_PARSE_ERROR,
            f"failed to parse {path.as_posix()}: {exc}",
            fix="fix the YAML syntax in this manifest",
        ) from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise errors.BoundsError(
            errors.E_MANIFEST_PARSE_ERROR,
            f"{path.as_posix()} must contain a YAML mapping, got {type(data).__name__}",
            fix="rewrite this manifest as a top-level mapping",
        )
    return data


def _read_yaml_or_issue(name: str, path: Path, issues: list[Issue]) -> dict | None:
    """Read a subsystem YAML mapping, appending an Issue and returning None on failure."""
    if not path.is_file():
        issues.append(
            Issue(
                code=errors.E_SUBSYSTEM_NOT_FOUND,
                severity="error",
                message=f"subsystem '{name}' declared in root but missing at {path.as_posix()}",
                subsystem=name,
                fix=f"create the manifest file or remove '{name}' from root.subsystems",
            )
        )
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        issues.append(
            Issue(
                code=errors.E_MANIFEST_PARSE_ERROR,
                severity="error",
                message=f"failed to parse {path.as_posix()}: {exc}",
                subsystem=name,
                fix="fix the YAML syntax in this manifest",
            )
        )
        return None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        issues.append(
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message=f"subsystem '{name}' manifest must be a YAML mapping",
                subsystem=name,
                fix="rewrite the subsystem manifest as a top-level mapping",
            )
        )
        return None
    return data


def _validate_root_yaml(project_root: Path) -> list[Issue]:
    """Re-read the root YAML as a raw dict and run schema validation against it."""
    path = config.config_dir(project_root) / config.ROOT_FILE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        # load_root already succeeded by this point, so a re-read failure is unexpected;
        # treat it softly as a schema issue rather than crashing load_all.
        return [
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message=f"could not re-read {path.as_posix()} for validation",
                fix="fix the YAML syntax in root.yaml",
            )
        ]
    if not isinstance(data, dict):
        data = {}
    return schema.validate_root(data)


def _validate_parents(subsystems: dict[str, SubsystemCompact]) -> list[Issue]:
    """Validate explicit ``parent:`` declarations: a parent must name a real subsystem and the
    containment graph must be acyclic (no subsystem is its own ancestor). Auto-detected
    path-nesting containment cannot form a cycle by construction, so only explicit edges are
    checked here. Reported soft (warnings) — a bad ``parent`` never blocks, it just isn't applied."""
    issues: list[Issue] = []
    for name in sorted(subsystems):
        declared = (subsystems[name].parent or "").strip()
        if not declared:
            continue
        if declared == name:
            issues.append(
                Issue(
                    code=errors.E_SCHEMA_INVALID,
                    severity="warning",
                    message=f"subsystem '{name}' declares itself as its own parent",
                    subsystem=name,
                    fix=f"remove `parent: {name}` from {name}.yaml",
                )
            )
            continue
        if declared not in subsystems:
            issues.append(
                Issue(
                    code=errors.E_SCHEMA_INVALID,
                    severity="warning",
                    message=f"subsystem '{name}' declares parent '{declared}' which is not a known subsystem",
                    subsystem=name,
                    fix=f"point `parent:` at a subsystem listed in root.subsystems, or remove the key",
                )
            )
            continue
        # Walk the explicit parent chain; flag a cycle (and stop) if we revisit a node.
        seen = {name}
        cursor = declared
        while cursor:
            if cursor in seen:
                issues.append(
                    Issue(
                        code=errors.E_SCHEMA_INVALID,
                        severity="warning",
                        message=f"subsystem '{name}' has a circular parent chain via '{cursor}'",
                        subsystem=name,
                        fix="remove a `parent:` edge so the containment chain is acyclic",
                    )
                )
                break
            seen.add(cursor)
            cursor = (subsystems[cursor].parent or "").strip() if cursor in subsystems else ""
    return issues


def _fill_consumed_by(subsystems: dict[str, SubsystemCompact]) -> None:
    """Back-fill each provider's ``consumed_by`` from consumers' ``consumes`` edges."""
    inverse: dict[str, set[str]] = {name: set() for name in subsystems}
    for consumer_name, sub in subsystems.items():
        for edge in sub.consumes:
            provider = edge.subsystem
            if provider in inverse:
                inverse[provider].add(consumer_name)
    for name, consumers in inverse.items():
        subsystems[name].consumed_by = sorted(consumers)
