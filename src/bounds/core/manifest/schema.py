"""Schema validation for manifest dicts.

Pure, side-effect-free validators that turn malformed manifest data into a list of
``Issue`` objects. They never raise — a structurally broken manifest degrades to
``E_SCHEMA_INVALID`` issues so the rest of the pipeline can keep running ("fail soft,
report hard"). Checks cover required keys, enum membership (role/criticality/enforce),
and the type shapes of the collection fields.
"""

from __future__ import annotations

from ...shared import config, errors
from ...shared.models import Issue

__all__ = []


def validate_root(data: dict) -> list[Issue]:
    """Validate a root-manifest dict, returning schema Issues (never raises).

    Enforces: ``version`` and ``project`` present, ``enforce`` (if present) in
    ``config.VALID_ENFORCE``, ``subsystems`` (if present) a list of names, and
    ``entry_points`` (if present) a list of file globs.
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
                fix='set `enforce:` to "off" (advisory), "warn" (report, never block), or "on" (block) in root.yaml',
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

    entry_points = data.get("entry_points")
    if entry_points is not None and not isinstance(entry_points, list):
        issues.append(
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message="root manifest 'entry_points' must be a list of file globs",
                fix="make `entry_points` a YAML list, e.g. `entry_points: [main.py, app.py]`",
            )
        )

    issues.extend(_validate_role_defs(data.get("roles")))
    issues.extend(_validate_criticality_defs(data.get("criticality")))
    issues.extend(_validate_sdd(data.get("sdd")))
    issues.extend(_validate_agentsync(data.get("agentsync")))
    return issues


def _validate_agentsync(agentsync) -> list[Issue]:
    """Validate the optional root ``agentsync:`` block.

    Opt-in and small, like ``sdd:``. Unknown extra keys are allowed (a project may keep its own
    agent conventions alongside Bounds'), but the one field Bounds acts on must be predictable:
    ``invocation`` is one of ``config.VALID_INVOCATION`` (off|nudge|strict).
    """
    if agentsync is None:
        return []
    if not isinstance(agentsync, dict):
        return [
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message="root manifest 'agentsync' must be a mapping",
                fix="define it as `agentsync: { invocation: nudge }` or remove the block",
            )
        ]

    issues: list[Issue] = []
    invocation = agentsync.get("invocation")
    if invocation is not None and config.normalize_invocation(invocation) is None:
        issues.append(
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message=(
                    "root manifest 'agentsync.invocation' must be one of "
                    f"{sorted(config.VALID_INVOCATION)}, got {invocation!r}"
                ),
                fix='set `agentsync.invocation` to "off" (advisory files only), '
                '"nudge" (gentle reminder hook), or "strict" (pause before a broad search)',
            )
        )
    return issues


def _validate_sdd(sdd) -> list[Issue]:
    """Validate the optional root ``sdd:`` block.

    The block is intentionally small and opt-in. Unknown extra keys are allowed so a project can
    keep its own SDD conventions next to Bounds' settings, but the fields Bounds acts on must be
    predictable: ``enabled`` is boolean, ``agent`` is one supported label, and ``phases`` is a
    list drawn from the canonical phase names.
    """
    if sdd is None:
        return []
    if not isinstance(sdd, dict):
        return [
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message="root manifest 'sdd' must be a mapping",
                fix="define SDD as `sdd: { enabled: true, agent: codex }` or remove the block",
            )
        ]

    issues: list[Issue] = []
    enabled = sdd.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        issues.append(
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message="root manifest 'sdd.enabled' must be true or false",
                fix="set `sdd.enabled: true` to opt in, or remove the sdd block",
            )
        )

    agent = sdd.get("agent")
    if agent is not None and str(agent) not in config.SDD_AGENTS:
        issues.append(
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message=(
                    "root manifest 'sdd.agent' must be one of "
                    f"{list(config.SDD_AGENTS)}, got {agent!r}"
                ),
                fix="set `sdd.agent` to the coding agent whose project wiring you want Bounds to describe",
            )
        )

    phases = sdd.get("phases")
    if phases is not None:
        if not isinstance(phases, list) or not all(isinstance(p, str) for p in phases):
            issues.append(
                Issue(
                    code=errors.E_SCHEMA_INVALID,
                    severity="error",
                    message="root manifest 'sdd.phases' must be a list of phase names",
                    fix=f"make `sdd.phases` a list drawn from {list(config.SDD_PHASES)}",
                )
            )
        else:
            unknown = sorted({p for p in phases if p not in config.SDD_PHASES})
            if unknown:
                issues.append(
                    Issue(
                        code=errors.E_SCHEMA_INVALID,
                        severity="error",
                        message=(
                            "root manifest 'sdd.phases' contains unknown phase(s): "
                            + ", ".join(unknown)
                        ),
                        fix=f"use the canonical phase names: {list(config.SDD_PHASES)}",
                    )
                )
    return issues


def _validate_role_defs(roles) -> list[Issue]:
    """Validate an optional custom ``roles:`` block: each must extend a base role."""
    if roles is None:
        return []
    if not isinstance(roles, dict):
        return [
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message="root manifest 'roles' must be a mapping of name -> definition",
                fix="define each custom role with `extends:` or `type:` a built-in base "
                f"({sorted(config.BUILTIN_ROLES)})",
            )
        ]
    issues: list[Issue] = []
    for name, spec in roles.items():
        spec = spec if isinstance(spec, dict) else {}
        base = str(spec.get("extends") or spec.get("type") or "").strip()
        if base not in config.BUILTIN_ROLES:
            issues.append(
                Issue(
                    code=errors.E_SCHEMA_INVALID,
                    severity="error",
                    message=(
                        f"custom role '{name}' must `extends`/`type` a built-in base role, "
                        f"got {base!r}"
                    ),
                    fix=f"set `extends:` (or `type:`) to one of {sorted(config.BUILTIN_ROLES)}",
                )
            )
    return issues


def _validate_criticality_defs(crit) -> list[Issue]:
    """Validate an optional custom ``criticality:`` block: each needs a ``depth:`` int."""
    if crit is None:
        return []
    if not isinstance(crit, dict):
        return [
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message="root manifest 'criticality' must be a mapping of name -> {depth: int}",
                fix="define each label as `<name>: {depth: <int>}` (-1 unbounded, 0 none, N hops)",
            )
        ]
    issues: list[Issue] = []
    for name, spec in crit.items():
        depth = spec.get("depth") if isinstance(spec, dict) else spec
        if not isinstance(depth, int) or isinstance(depth, bool):
            issues.append(
                Issue(
                    code=errors.E_SCHEMA_INVALID,
                    severity="error",
                    message=f"custom criticality '{name}' must declare an integer 'depth', got {depth!r}",
                    fix=f"write `{name}: {{ depth: 1 }}` (-1 unbounded, 0 none, N hops)",
                )
            )
    return issues


def validate_subsystem(
    name: str,
    data: dict,
    valid_roles: set[str] | None = None,
    valid_criticality: set[str] | None = None,
) -> list[Issue]:
    """Validate a subsystem-manifest dict, returning schema Issues (never raises).

    Enforces: ``name`` present (inferred from ``name`` arg if YAML omits it),
    ``role`` in ``valid_roles``, ``criticality`` optional but in ``valid_criticality`` if
    present (default ``leaf``), ``paths`` a list, each ``exposes`` entry carrying a name and
    each ``consumes`` entry carrying a subsystem. Issues carry ``subsystem=name``.

    ``valid_roles``/``valid_criticality`` come from the resolved root registries;
    they default to the built-in enums so callers without a root context stay backward
    compatible.
    """
    valid_roles = valid_roles if valid_roles is not None else set(config.BUILTIN_ROLES)
    valid_criticality = (
        valid_criticality if valid_criticality is not None else set(config.BUILTIN_CRITICALITY)
    )
    if not isinstance(data, dict):
        return [
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message=f"subsystem '{name}' manifest must be a YAML mapping",
                subsystem=name,
                fix="rewrite the subsystem manifest as a top-level mapping with name/role keys",
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
                fix="add `name: <subsystem>` to the subsystem manifest",
            )
        )

    role = data.get("role")
    if role is None or role not in valid_roles:
        issues.append(
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message=f"subsystem '{declared_name}' role must be one of {sorted(valid_roles)}, got {role!r}",
                subsystem=name,
                fix=_unknown_label_fix("role", role, valid_roles),
            )
        )

    criticality = data.get("criticality")
    if criticality is not None and criticality not in valid_criticality:
        issues.append(
            Issue(
                code=errors.E_SCHEMA_INVALID,
                severity="error",
                message=(
                    f"subsystem '{declared_name}' criticality must be one of "
                    f"{sorted(valid_criticality)}, got {criticality!r}"
                ),
                subsystem=name,
                fix=_unknown_label_fix("criticality", criticality, valid_criticality),
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

    # ``docs``/``tests`` link the doc/test files that cover this subsystem — same shape as
    # ``paths``: a list of repo-relative posix path strings (file, directory, or simple glob).
    for field_name in ("docs", "tests"):
        value = data.get(field_name)
        if value is None:
            continue
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            issues.append(
                Issue(
                    code=errors.E_SCHEMA_INVALID,
                    severity="error",
                    message=f"subsystem '{declared_name}' '{field_name}' must be a list of strings",
                    subsystem=name,
                    fix=f"make `{field_name}` a YAML list of file/directory/glob paths "
                    f"(e.g. `{field_name}: [{'tests/auth' if field_name == 'tests' else 'docs/auth.md'}]`)",
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


def _unknown_label_fix(kind: str, got, valid: set[str]) -> str:
    """Build a fix hint for an invalid role/criticality, suggesting a near-miss if any."""
    base = (
        f"set `{kind}:` to one of {sorted(valid)}, "
        f"or define a custom {kind} in root.yaml under '{kind}s'"
        if kind == "role"
        else f"set `{kind}:` to one of {sorted(valid)}, or define a custom {kind} in root.yaml"
    )
    suggestion = _closest(str(got or ""), valid)
    return f"did you mean '{suggestion}'? {base}" if suggestion else base


def _closest(value: str, candidates: set[str]) -> str | None:
    """Return a candidate within edit-distance 1 of ``value`` (cheap typo detection)."""
    if not value:
        return None
    for cand in sorted(candidates):
        if _within_one_edit(value, cand):
            return cand
    return None


def _within_one_edit(a: str, b: str) -> bool:
    """True if ``a`` and ``b`` differ by at most one insert/delete/substitution."""
    if a == b:
        return False
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:  # single substitution
        return sum(x != y for x, y in zip(a, b)) == 1
    # one insertion/deletion: walk the shorter against the longer
    short, long = (a, b) if la < lb else (b, a)
    i = j = 0
    skipped = False
    while i < len(short) and j < len(long):
        if short[i] == long[j]:
            i += 1
            j += 1
        elif skipped:
            return False
        else:
            skipped = True
            j += 1
    return True


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
