"""Schema flexibility: extensible roles + criticality declared in root.yaml."""

from __future__ import annotations

from bounds import config
from bounds.manifest import schema
from bounds.models import RootManifest
from bounds.validate.propagation import propagate
from bounds.models import SubsystemCompact, Consumes


# ---------------------------------------------------------------------------
# Registry resolution
# ---------------------------------------------------------------------------
def test_no_custom_block_uses_builtins():
    """With no roles/criticality block, the registries fall back to the built-ins, and service exposes orphans while library does not — the default extensible behavior."""
    root = RootManifest()
    assert set(root.role_registry()) == config.BUILTIN_ROLES
    assert root.criticality_registry() == config.BUILTIN_CRITICALITY_DEPTH
    # built-in service exposes orphans; library does not.
    assert root.role_registry()["service"]["orphan_exposes"] is True
    assert root.role_registry()["library"]["orphan_exposes"] is False


def test_custom_roles_inherit_base_behavior():
    """A custom role inherits its base role's flags (extends:service → orphan_exposes true) and, once declared, built-ins are replaced — no two competing role naming systems."""
    root = RootManifest.from_dict(
        {
            "version": "1",
            "project": "x",
            "roles": {
                "gateway": {"extends": "service"},
                "util": {"type": "library"},
            },
        }
    )
    reg = root.role_registry()
    # Custom-only: built-ins are NOT auto-included (no two competing naming systems).
    assert set(reg) == {"gateway", "util"}
    assert reg["gateway"]["base"] == "service"
    assert reg["gateway"]["orphan_exposes"] is True  # inherited from service
    assert reg["util"]["orphan_exposes"] is False


def test_custom_role_can_override_flag():
    """A custom role may override an inherited behavior flag (a library-typed role explicitly setting orphan_exposes:true), so the registry isn't a fixed enum at the check site."""
    root = RootManifest.from_dict(
        {"version": "1", "project": "x", "roles": {"sink": {"type": "library", "orphan_exposes": True}}}
    )
    assert root.role_registry()["sink"]["orphan_exposes"] is True


def test_custom_criticality_depth_drives_propagation():
    """A custom criticality's declared depth drives propagation reach (depth -1 = transitive/unbounded, depth 0 = no propagation) — propagation must read the registry, not hardcode the enum."""
    root = RootManifest.from_dict(
        {"version": "1", "project": "x", "criticality": {"critical": {"depth": -1}, "standard": {"depth": 0}}}
    )
    depth_map = root.criticality_registry()
    assert depth_map == {"critical": -1, "standard": 0}

    subs = {
        "a": SubsystemCompact(name="a", role="service", criticality="critical"),
        "b": SubsystemCompact(name="b", role="service", criticality="critical", consumes=[Consumes("a")]),
        "c": SubsystemCompact(name="c", role="service", criticality="standard", consumes=[Consumes("b")]),
    }
    # 'a' critical -> unbounded: both b and c are affected transitively.
    assert propagate({"a"}, subs, depth_map) == {"b", "c"}
    # 'c' standard depth 0 -> no propagation.
    assert propagate({"c"}, subs, depth_map) == set()


# ---------------------------------------------------------------------------
# Validation against resolved sets
# ---------------------------------------------------------------------------
def test_custom_role_accepts_declared_value():
    """Subsystem schema validation must accept a role that the resolved custom-role set declares (gateway), so custom roles aren't rejected as invalid."""
    valid_roles = {"gateway", "util"}
    issues = schema.validate_subsystem(
        "auth", {"name": "auth", "role": "gateway"}, valid_roles, {"leaf"}
    )
    assert issues == []


def test_builtin_role_rejected_when_custom_declared():
    """Once custom roles are declared, a built-in like 'service' is no longer valid and must be flagged E_SCHEMA_INVALID — the custom set fully replaces the built-ins."""
    # When custom roles are declared, plain 'service' is no longer valid.
    issues = schema.validate_subsystem(
        "auth", {"name": "auth", "role": "service"}, {"gateway"}, {"leaf"}
    )
    assert any(i.code == "E_SCHEMA_INVALID" for i in issues)


def test_typo_suggestion_in_fix():
    """A near-miss role ('servic') yields a fix string suggesting the intended built-in ('service') — schema errors must carry an actionable next step."""
    issues = schema.validate_subsystem("auth", {"name": "auth", "role": "servic"})
    assert issues
    assert "did you mean 'service'" in issues[0].fix


def test_invalid_custom_role_def_flagged():
    """A custom role extending a nonexistent base is a root-schema error naming the offending role, so a malformed registry definition is caught at load, not at a check site."""
    issues = schema.validate_root(
        {"version": "1", "project": "x", "roles": {"gateway": {"extends": "nonsense"}}}
    )
    assert any("gateway" in i.message for i in issues)


def test_invalid_custom_criticality_def_flagged():
    """A custom criticality with a non-integer depth ('lots') is a root-schema error naming the offending level, so a bad depth can't silently break propagation."""
    issues = schema.validate_root(
        {"version": "1", "project": "x", "criticality": {"high": {"depth": "lots"}}}
    )
    assert any("high" in i.message for i in issues)
