"""Schema validation for manifest dicts.

Pure, side-effect-free validators that turn malformed manifest data into a list of
``Issue`` objects. They never raise — a structurally broken manifest degrades to
``E_SCHEMA_INVALID`` issues so the rest of the pipeline can keep running ("fail soft,
report hard"). Checks cover required keys, enum membership (role/criticality/enforce),
and the type shapes of the collection fields.
"""

from __future__ import annotations

from .. import config, errors
from ..models import Issue


def validate_root(data: dict) -> list[Issue]:
    """Validate a root-manifest dict, returning schema Issues (never raises).

    Enforces: ``version`` and ``project`` present, ``enforce`` (if present) in
    ``config.VALID_ENFORCE``, and ``subsystems`` (if present) a list of names.
    """
    issues: list[Issue] = []

    if not isinstance(data, dict):
        return [
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message="root.yaml must be a YAML mapping",
                fix="rewrite root.yaml as a top-level mapping with version/project keys",
            )
        ]

    if not data.get("version"):
        issues.append(
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message="root manifest missing required key 'version'",
                fix=f'add `version: "{config.SCHEMA_VERSION}"` to root.yaml',
            )
        )

    if not data.get("project"):
        issues.append(
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message="root manifest missing required key 'project'",
                fix="add `project: <name>` to root.yaml",
            )
        )

    enforce = data.get("enforce")
    if enforce is not None and enforce not in config.VALID_ENFORCE:
        issues.append(
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message=f"root manifest 'enforce' must be one of {sorted(config.VALID_ENFORCE)}, got {enforce!r}",
                fix='set `enforce: "off"` (or "on") in root.yaml',
            )
        )

    subsystems = data.get("subsystems")
    if subsystems is not None and not isinstance(subsystems, list):
        issues.append(
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message="root manifest 'subsystems' must be a list of names",
                fix="make `subsystems` a YAML list, e.g. `subsystems: [manifest, extract]`",
            )
        )

    return issues


def validate_subsystem(name: str, data: dict) -> list[Issue]:
    """Validate a subsystem-compact dict, returning schema Issues (never raises).

    Enforces: ``name`` present (inferred from ``name`` arg if YAML omits it),
    ``role`` in ``config.VALID_ROLES``, ``criticality`` optional but valid if present
    (default ``leaf``), ``paths`` a list, each ``exposes`` entry carrying a name and
    each ``consumes`` entry carrying a subsystem. Issues carry ``subsystem=name``.
    """
    if not isinstance(data, dict):
        return [
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message=f"subsystem '{name}' compact.yaml must be a YAML mapping",
                subsystem=name,
                fix="rewrite compact.yaml as a top-level mapping with name/role keys",
            )
        ]

    issues: list[Issue] = []

    declared_name = data.get("name") or name
    if not declared_name:
        issues.append(
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message="subsystem missing required key 'name'",
                subsystem=name or None,
                fix="add `name: <subsystem>` to compact.yaml",
            )
        )

    role = data.get("role")
    if role is None or role not in config.VALID_ROLES:
        issues.append(
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message=f"subsystem '{declared_name}' role must be one of {sorted(config.VALID_ROLES)}, got {role!r}",
                subsystem=name,
                fix="set `role:` to service|platform|connector|library",
            )
        )

    criticality = data.get("criticality")
    if criticality is not None and criticality not in config.VALID_CRITICALITY:
        issues.append(
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message=(
                    f"subsystem '{declared_name}' criticality must be one of "
                    f"{sorted(config.VALID_CRITICALITY)}, got {criticality!r}"
                ),
                subsystem=name,
                fix="set `criticality:` to core|connector|leaf (or omit for leaf)",
            )
        )

    paths = data.get("paths")
    if paths is not None and not isinstance(paths, list):
        issues.append(
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message=f"subsystem '{declared_name}' 'paths' must be a list",
                subsystem=name,
                fix="make `paths` a YAML list of directories/globs",
            )
        )

    exposes = data.get("exposes")
    if exposes is not None:
        if not isinstance(exposes, list):
            issues.append(
                Issue(
                    code=errors.E_SCHEMA_INVALID,
                    severity="error",
                    message=f"subsystem '{declared_name}' 'exposes' must be a list",
                    subsystem=name,
                    fix="make `exposes` a YAML list of interface entries",
                )
            )
        else:
            for entry in exposes:
                if not _entry_name(entry):
                    issues.append(
                        Issue(
                            code=errors.E_SCHEMA_INVALID,
                            severity="error",
                            message=f"subsystem '{declared_name}' has an exposes entry missing a name",
                            subsystem=name,
                            fix="give each exposes entry a name, e.g. `{ name: login, kind: function }`",
                        )
                    )

    consumes = data.get("consumes")
    if consumes is not None:
        if not isinstance(consumes, list):
            issues.append(
                Issue(
                    code=errors.E_SCHEMA_INVALID,
                    severity="error",
                    message=f"subsystem '{declared_name}' 'consumes' must be a list",
                    subsystem=name,
                    fix="make `consumes` a YAML list of dependency entries",
                )
            )
        else:
            for entry in consumes:
                if not _entry_subsystem(entry):
                    issues.append(
                        Issue(
                            code=errors.E_SCHEMA_INVALID,
                            severity="error",
                            message=f"subsystem '{declared_name}' has a consumes entry missing a subsystem",
                            subsystem=name,
                            fix="give each consumes entry a subsystem, e.g. `{ subsystem: models }`",
                        )
                    )

    return issues


def _entry_name(entry) -> str:
    """Extract the declared name from an exposes entry (bare string or mapping)."""
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        return str(entry.get("name") or "").strip()
    return ""


def _entry_subsystem(entry) -> str:
    """Extract the provider name from a consumes entry (bare string or mapping)."""
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        return str(entry.get("subsystem") or "").strip()
    return ""
