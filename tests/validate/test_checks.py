"""Tests for the individual validation checks (structural drift, boundary, contract, cross-impact, cycles, orphans)."""

from __future__ import annotations

from bounds import errors
from bounds.models import (
    Consumes,
    ExtractResult,
    ImportRef,
    Interface,
    Symbol,
)
from bounds.models import SubsystemCompact as Sub
from bounds.validate.checks import (
    _find_cycles,
    check_boundary,
    check_contract,
    check_cross_impact,
    check_cycles,
    check_orphans,
    check_structural_drift,
)

from _validate_helpers import _ctx  # sibling module (pytest adds tests/validate/ to sys.path)


# ===========================================================================
# Check 1 — structural drift
# ===========================================================================
def test_drift_declared_but_missing():
    """A symbol declared in exposes but absent from the source is a blocking E_STRUCTURAL_DRIFT error — the core 'manifest lies' guard."""
    subs = {"a": Sub(name="a", paths=["x"], exposes=[Interface("foo")])}
    extracts = {"x/f.py": ExtractResult("x/f.py", "python", [Symbol("bar", "function", 1, True)])}
    issues = check_structural_drift(_ctx(subs, extracts, {"x/f.py": "a"}))
    assert any(i.code == errors.E_STRUCTURAL_DRIFT and i.severity == "error" and "foo" in i.message
               for i in issues)


def test_drift_undeclared_extra_on_core_is_info():
    """An undeclared extra export on a core subsystem is info-only (never an error): declared symbols present, extras merely noted."""
    subs = {"a": Sub(name="a", criticality="core", paths=["x"], exposes=[Interface("foo")])}
    extracts = {
        "x/f.py": ExtractResult(
            "x/f.py", "python", [Symbol("foo", "function", 1, True), Symbol("extra", "function", 2, True)]
        )
    }
    issues = check_structural_drift(_ctx(subs, extracts, {"x/f.py": "a"}))
    assert any(i.severity == "info" and "extra" in i.message for i in issues)
    assert not any(i.severity == "error" for i in issues)  # foo is declared & present


def test_drift_undeclared_extra_on_leaf_is_info():
    """Undeclared exports surface as info on leaf/connector too (where common real drift hides), still info → never blocks."""
    for crit in ("leaf", "connector"):
        subs = {"a": Sub(name="a", criticality=crit, paths=["x"], exposes=[Interface("foo")])}
        extracts = {
            "x/f.py": ExtractResult(
                "x/f.py", "python",
                [Symbol("foo", "function", 1, True), Symbol("extra", "function", 2, True)],
            )
        }
        issues = check_structural_drift(_ctx(subs, extracts, {"x/f.py": "a"}))
        assert any(i.severity == "info" and "extra" in i.message for i in issues), crit
        assert not any(i.severity == "error" for i in issues), crit


def test_drift_no_undeclared_flag_when_exposes_empty():
    """A subsystem declaring no exposes (e.g. not yet calibrated) gets zero undeclared-drift info — drift needs a declared set to drift from."""
    subs = {"a": Sub(name="a", criticality="leaf", paths=["x"], exposes=[])}
    extracts = {"x/f.py": ExtractResult("x/f.py", "python", [Symbol("extra", "function", 1, True)])}
    issues = check_structural_drift(_ctx(subs, extracts, {"x/f.py": "a"}))
    assert issues == []


def test_drift_excludes_test_cases_from_undeclared_export_noise():
    """BOUNDS-015: test cases (test_*/Test* in a test file) are intentionally kept out of exposes
    by discover (BOUNDS-014), so the drift check must not re-flag them as undeclared exports — else
    a repo's tests subsystem floods validate with hundreds of info drifts (click: 476 → 0)."""
    subs = {"tests": Sub(name="tests", criticality="leaf", paths=["tests"],
                         exposes=[Interface("make_fixture")])}
    extracts = {
        "tests/test_x.py": ExtractResult(
            "tests/test_x.py", "python",
            [
                Symbol("test_foo", "function", 1, True),
                Symbol("test_bar", "function", 2, True),
                Symbol("TestThing", "class", 3, True),
                Symbol("make_fixture", "function", 4, True),  # a genuine helper, declared
            ],
        )
    }
    issues = check_structural_drift(_ctx(subs, extracts, {"tests/test_x.py": "tests"}))
    assert issues == [], [i.message for i in issues]  # cases excluded; helper declared+present


def test_drift_test_named_symbol_in_non_test_file_still_flags():
    """The BOUNDS-015 exclusion is gated on is_test_file: a ``test_*``-named symbol in NON-test
    source is a real undeclared export and must still surface — we silence true test cases, not
    every look-alike name anywhere in the tree."""
    subs = {"a": Sub(name="a", criticality="leaf", paths=["x"], exposes=[Interface("foo")])}
    extracts = {"x/f.py": ExtractResult(
        "x/f.py", "python",
        [Symbol("foo", "function", 1, True), Symbol("test_helper", "function", 2, True)],
    )}
    issues = check_structural_drift(_ctx(subs, extracts, {"x/f.py": "a"}))
    assert any(i.severity == "info" and "test_helper" in i.message for i in issues)


def test_drift_excludes_nextjs_framework_entry_exports():
    """BOUNDS-016: a Next.js page/route entry file's exports (default component, GET/POST handlers,
    route-segment config like revalidate) are framework-invoked, not a consumable API, so they must
    not flood undeclared-export drift — zod's docs app alone produced 165 such info drifts."""
    subs = {"app": Sub(name="app", criticality="leaf", paths=["app"], exposes=[Interface("helper")])}
    extracts = {
        "app/lib.ts": ExtractResult("app/lib.ts", "typescript", [Symbol("helper", "function", 1, True)]),
        "app/blog/page.tsx": ExtractResult(
            "app/blog/page.tsx", "typescript",
            [Symbol("BlogIndexPage", "function", 1, True),
             Symbol("generateMetadata", "function", 2, True),
             Symbol("revalidate", "const", 3, True)],
        ),
        "app/api/route.ts": ExtractResult(
            "app/api/route.ts", "typescript",
            [Symbol("GET", "function", 1, True), Symbol("POST", "function", 2, True)],
        ),
    }
    fo = {p: "app" for p in extracts}
    issues = check_structural_drift(_ctx(subs, extracts, fo))
    msgs = " ".join(i.message for i in issues)
    for nm in ("BlogIndexPage", "generateMetadata", "revalidate", "GET", "POST"):
        assert nm not in msgs, nm
    assert issues == [], [i.message for i in issues]  # helper declared+present; framework exports excluded


def test_drift_route_file_outside_app_dir_still_flags():
    """The BOUNDS-016 exclusion is gated on an app/|pages/ path segment: an ordinary ``lib/route.ts``
    is a real module whose undeclared exports must still surface — we don't silence every file that
    merely happens to be named ``route``."""
    subs = {"a": Sub(name="a", criticality="leaf", paths=["lib"], exposes=[Interface("foo")])}
    extracts = {"lib/route.ts": ExtractResult(
        "lib/route.ts", "typescript",
        [Symbol("foo", "function", 1, True), Symbol("GET", "function", 2, True)],
    )}
    issues = check_structural_drift(_ctx(subs, extracts, {"lib/route.ts": "a"}))
    assert any(i.severity == "info" and "GET" in i.message for i in issues)


# ===========================================================================
# Cycle helper — deep-graph hardening
# ===========================================================================
def test_find_cycles_handles_deep_graph_without_recursionerror():
    """A 2000-node cycle far deeper than Python's recursion limit must be enumerated by the iterative DFS, never raise RecursionError."""
    # One big cycle 0 -> 1 -> ... -> 1999 -> 0, far deeper than Python's recursion limit.
    n = 2000
    graph = {str(i): [str((i + 1) % n)] for i in range(n)}
    cycles = _find_cycles(graph)
    assert len(cycles) == 1
    assert len(cycles[0]) == n


# ===========================================================================
# Check 2 — boundary compliance
# ===========================================================================
def test_boundary_flags_internal_import():
    """Importing a non-exposed symbol across a subsystem boundary is an E_BOUNDARY_VIOLATION — reaching past the declared public API is the violation."""
    subs = {
        "database": Sub(name="database", exposes=[Interface("findUser")]),
        "auth": Sub(name="auth", exposes=[Interface("login")]),
    }
    extracts = {
        "src/database/index.ts": ExtractResult(
            "src/database/index.ts", "typescript",
            [Symbol("findUser", "function", 1, True), Symbol("secret", "function", 2, True)],
        ),
        "src/auth/index.ts": ExtractResult(
            "src/auth/index.ts", "typescript",
            [Symbol("login", "function", 1, True)],
            [ImportRef("../database", ["secret"], 1)],
        ),
    }
    owner = {"src/database/index.ts": "database", "src/auth/index.ts": "auth"}
    issues = check_boundary(_ctx(subs, extracts, owner))
    assert any(i.code == errors.E_BOUNDARY_VIOLATION and "secret" in i.message for i in issues)


def test_boundary_allows_exposed_import():
    """Importing a symbol the owning subsystem DOES expose is legal — the boundary check must not over-flag a contract-honoring import."""
    subs = {
        "database": Sub(name="database", exposes=[Interface("findUser")]),
        "auth": Sub(name="auth", exposes=[Interface("login")]),
    }
    extracts = {
        "src/database/index.ts": ExtractResult(
            "src/database/index.ts", "typescript", [Symbol("findUser", "function", 1, True)]
        ),
        "src/auth/index.ts": ExtractResult(
            "src/auth/index.ts", "typescript",
            [Symbol("login", "function", 1, True)],
            [ImportRef("../database", ["findUser"], 1)],
        ),
    }
    owner = {"src/database/index.ts": "database", "src/auth/index.ts": "auth"}
    assert check_boundary(_ctx(subs, extracts, owner)) == []


def test_boundary_allows_test_files_to_import_internals():
    """Tests may exercise internals without turning a fresh discover into production boundary errors."""
    subs = {
        "library": Sub(name="library", exposes=[Interface("public")]),
        "tests": Sub(name="tests", paths=["tests"], consumes=[Consumes("library")]),
    }
    extracts = {
        "src/library.py": ExtractResult(
            "src/library.py", "python",
            [Symbol("public", "function", 1, True), Symbol("_private", "function", 2, True)],
        ),
        "tests/test_library.py": ExtractResult(
            "tests/test_library.py", "python",
            [],
            [ImportRef("../src/library", ["_private"], 1)],
        ),
    }
    owner = {"src/library.py": "library", "tests/test_library.py": "tests"}
    assert check_boundary(_ctx(subs, extracts, owner)) == []


# ===========================================================================
# Check 3 — contract compliance
# ===========================================================================
def test_contract_missing_export():
    """A consumer declaring an interface the provider doesn't expose is E_CONTRACT_MISSING_EXPORT; the genuinely-exported one is not flagged."""
    subs = {
        "database": Sub(name="database", exposes=[Interface("findUser")]),
        "auth": Sub(name="auth", consumes=[Consumes("database", interfaces=["findUser", "ghost"])]),
    }
    issues = check_contract(_ctx(subs))
    assert any(i.code == errors.E_CONTRACT_MISSING_EXPORT and "ghost" in i.message for i in issues)
    assert not any("findUser" in i.message for i in issues)


def test_contract_unresolved_subsystem():
    """Consuming a subsystem that doesn't exist is E_UNRESOLVED_REFERENCE — a dangling dependency edge must be caught, not ignored."""
    subs = {"auth": Sub(name="auth", consumes=[Consumes("ghostsub", interfaces=["x"])])}
    issues = check_contract(_ctx(subs))
    assert any(i.code == errors.E_UNRESOLVED_REFERENCE for i in issues)


# ===========================================================================
# Check 4 — cross-subsystem impact
# ===========================================================================
def test_cross_impact_flags_propagated_consumer():
    """When a core subsystem is dirty, each propagated consumer is flagged E_STALE_INTERFACE so its dependents know to re-verify."""
    subs = {
        "a": Sub(name="a", criticality="core"),
        "b": Sub(name="b", consumes=[Consumes("a")]),
    }
    issues = check_cross_impact(_ctx(subs, dirty={"a"}, propagated={"b"}))
    assert any(i.code == errors.E_STALE_INTERFACE and i.subsystem == "b" for i in issues)


# ===========================================================================
# Check 5 — cycle detection
# ===========================================================================
def test_cycle_detected():
    """A mutual consumes relationship (a↔b) is a dependency cycle and must raise E_CYCLE_DETECTED."""
    subs = {
        "a": Sub(name="a", consumes=[Consumes("b")]),
        "b": Sub(name="b", consumes=[Consumes("a")]),
    }
    issues = check_cycles(_ctx(subs))
    assert any(i.code == errors.E_CYCLE_DETECTED for i in issues)


def test_no_cycle_in_dag():
    """An acyclic dependency graph yields no cycle issue — the detector must not false-positive on a plain DAG."""
    subs = {
        "a": Sub(name="a", consumes=[Consumes("b")]),
        "b": Sub(name="b"),
    }
    assert check_cycles(_ctx(subs)) == []


# ===========================================================================
# Check 6 — orphan detection
# ===========================================================================
def test_orphan_export_flagged():
    """A library export no declared consumer uses is E_ORPHAN_EXPORT; the consumed one isn't flagged — dead public API surfaces."""
    subs = {
        "lib": Sub(name="lib", role="library", exposes=[Interface("used"), Interface("orphan")]),
        "user": Sub(name="user", consumes=[Consumes("lib", interfaces=["used"])]),
    }
    issues = check_orphans(_ctx(subs))
    assert any(i.code == errors.E_ORPHAN_EXPORT and "orphan" in i.message for i in issues)
    assert not any("used" in i.message for i in issues)


def test_service_exposes_are_not_orphans():
    """A service's exposes are entry points (externally invoked), so they're never orphans even with no internal consumer — role registry's orphan_exposes flag drives this."""
    subs = {"svc": Sub(name="svc", role="service", exposes=[Interface("main")])}
    assert check_orphans(_ctx(subs)) == []


def test_orphans_not_flagged_without_interface_level_consumption():
    """BOUNDS-012: a subsystem consumed only at subsystem granularity (no declared interfaces — what
    `discover` emits) must not flood orphan-export; every public export would otherwise look
    orphaned. Orphan detection needs interface-level contracts to judge 'unused'."""
    subs = {
        "lib": Sub(name="lib", role="library", exposes=[Interface("a"), Interface("b")]),
        "user": Sub(name="user", consumes=[Consumes("lib")]),  # subsystem-level edge, no interfaces
    }
    assert check_orphans(_ctx(subs)) == []
    # But once a consumer declares the interfaces it uses, a genuinely-unused export is still flagged.
    subs["user"] = Sub(name="user", consumes=[Consumes("lib", interfaces=["a"])])
    flagged = check_orphans(_ctx(subs))
    assert any("'b'" in i.message for i in flagged) and not any("'a'" in i.message for i in flagged)
