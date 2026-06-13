"""Tests for the individual validation checks (structural drift, boundary, contract, cross-impact, cycles, orphans)."""

from __future__ import annotations

from bounds.shared import errors
from bounds.shared.models import (
    Consumes,
    ExtractResult,
    ImportRef,
    Interface,
    Symbol,
)
from bounds.shared.models import SubsystemCompact as Sub
from bounds.core.validate import engine
from bounds.core.validate.checks import (
    _find_cycles,
    check_boundary,
    check_composition_root,
    check_contract,
    check_cross_impact,
    check_cycles,
    check_orphans,
    check_schema,
    check_structural_drift,
)
from bounds.shared.models import Consumes as _Con, ExtractResult as _ER, Symbol as _Sym
from _validate_helpers import _ctx  # sibling module (pytest adds tests/validate/ to sys.path)


def test_private_exports_do_not_create_structural_drift(py_project):
    """Validation and calibration agree that underscore-prefixed symbols are private."""
    source = py_project / "src" / "models" / "thing.py"
    source.write_text(
        "class Thing:\n    pass\n\n"
        "def _helper():\n    pass\n\n"
        "__version__ = 'test'\n",
        encoding="utf-8",
    )

    report = engine.run(py_project, mode="full", persist=False)

    private_drift = [
        issue
        for issue in report.issues
        if issue.code == errors.E_STRUCTURAL_DRIFT
        and ("_helper" in issue.message or "__version__" in issue.message)
    ]
    assert private_drift == []

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


def test_drift_undeclared_exports_roll_up_into_one_issue_with_count():
    """Many undeclared exports in one subsystem collapse to a SINGLE info issue carrying `count` (the
    magnitude) and a capped symbol sample — the token-discipline rollup, so a large repo doesn't dump
    one issue per symbol into agent context. Names beyond the cap become '+N more'."""
    syms = [Symbol("keep", "function", 1, True)] + [
        Symbol(f"x{i}", "function", i + 2, True) for i in range(8)  # 8 undeclared exports
    ]
    subs = {"a": Sub(name="a", criticality="leaf", paths=["x"], exposes=[Interface("keep")])}
    extracts = {"x/f.py": ExtractResult("x/f.py", "python", syms)}
    drift = [
        i for i in check_structural_drift(_ctx(subs, extracts, {"x/f.py": "a"}))
        if i.code == errors.E_STRUCTURAL_DRIFT and i.severity == "info"
    ]
    assert len(drift) == 1            # ONE rolled issue, not 8 separate ones
    assert drift[0].count == 8        # magnitude preserved so overview's drift tally is unchanged
    assert "8 symbol(s) not declared" in drift[0].message
    assert "+3 more" in drift[0].message  # 8 - cap(5) = 3 elided
    for nm in ("x0", "x1", "x2", "x3", "x4"):
        assert nm in drift[0].message     # first 5 sorted names shown
    assert "x5" not in drift[0].message   # beyond the cap — elided into "+3 more", never dumped


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


def test_drift_excludes_generated_file_exports():
    """BOUNDS-021: generated files are skipped by calibrate, so validate must not report their undeclared exports."""
    subs = {"a": Sub(name="a", paths=["src"], exposes=[Interface("public")])}
    extracts = {
        "src/public.py": ExtractResult(
            "src/public.py", "python", [Symbol("public", "function", 1, True)]
        ),
        "src/generated.py": ExtractResult(
            "src/generated.py", "python", [Symbol("GeneratedClient", "class", 1, True)]
        ),
    }
    issues = check_structural_drift(
        _ctx(
            subs,
            extracts,
            {"src/public.py": "a", "src/generated.py": "a"},
            generated_files={"src/generated.py"},
        )
    )
    assert issues == [], [i.message for i in issues]


def test_drift_skips_declared_expose_for_unsupported_language_owner():
    """A subsystem owning an UNSUPPORTED-language file (Go/Rust/Java) has exposes Bounds can't
    verify — it has no adapter, extracted nothing, so a declared-but-absent expose is NOT
    proven-stale drift. Consistent with calibrate (which routes the same exposes to needs_review,
    never remove): the two must agree on an unsupported-language manifest."""
    subs = {"payments": Sub(name="payments", paths=["services/payments"],
                            exposes=[Interface("Charge"), Interface("Refund")])}
    # No extracts (no Go adapter) and payments flagged as an unsupported-language owner.
    issues = check_structural_drift(
        _ctx(subs, extracts={}, file_owner={}, unsupported_owners={"payments"})
    )
    assert issues == [], [i.message for i in issues]


def test_drift_supported_owner_not_in_unsupported_set_still_flags():
    """Regression guard: the unsupported-owner skip must NOT over-protect — a supported subsystem
    NOT in unsupported_owners with a declared-but-missing expose still flags E_STRUCTURAL_DRIFT."""
    subs = {"a": Sub(name="a", paths=["x"], exposes=[Interface("foo")])}
    extracts = {"x/f.py": ExtractResult("x/f.py", "python", [Symbol("bar", "function", 1, True)])}
    issues = check_structural_drift(
        _ctx(subs, extracts, {"x/f.py": "a"}, unsupported_owners=set())
    )
    assert any(i.code == errors.E_STRUCTURAL_DRIFT and i.severity == "error" and "foo" in i.message
               for i in issues)


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
# Check 5 — containment-aware cycle detection
# ===========================================================================
def test_parent_child_nesting_is_not_a_cycle():
    """A module importing its own subdirectory and back (parent ``src/auth`` ↔ child ``src/auth/guards``)
    is intra-module layering, not an architectural cycle — containment must suppress it."""
    subs = {
        "auth": Sub(name="auth", paths=["src/auth"], consumes=[Consumes("auth-guards")]),
        "auth-guards": Sub(name="auth-guards", paths=["src/auth/guards"], consumes=[Consumes("auth")]),
    }
    assert check_cycles(_ctx(subs)) == []


def test_explicit_parent_suppresses_cycle_without_path_nesting():
    """When paths don't nest, an explicit ``parent:`` still establishes containment and suppresses the
    parent↔child cycle."""
    subs = {
        "core": Sub(name="core", paths=["pkg/core"], consumes=[Consumes("core-impl")]),
        "core-impl": Sub(name="core-impl", paths=["pkg/impl"], parent="core", consumes=[Consumes("core")]),
    }
    assert check_cycles(_ctx(subs)) == []


def test_genuine_sibling_cycle_through_a_child_is_still_reported():
    """A real cross-domain cycle that merely passes through a parent→child edge (``auth`` → its child
    ``auth-guards`` → sibling ``sellers`` → ``auth``) has a non-containment edge, so it is genuine and
    must still be reported — containment only suppresses pure-nesting cycles."""
    subs = {
        "auth": Sub(name="auth", paths=["src/auth"], consumes=[Consumes("auth-guards")]),
        "auth-guards": Sub(name="auth-guards", paths=["src/auth/guards"], consumes=[Consumes("sellers")]),
        "sellers": Sub(name="sellers", paths=["src/sellers"], consumes=[Consumes("auth")]),
    }
    issues = check_cycles(_ctx(subs))
    assert any(i.code == errors.E_CYCLE_DETECTED for i in issues)


def test_sibling_cycle_unaffected_by_unrelated_nesting():
    """Two genuinely-cyclic siblings stay reported even when other subsystems are nested elsewhere."""
    subs = {
        "a": Sub(name="a", paths=["src/a"], consumes=[Consumes("b")]),
        "b": Sub(name="b", paths=["src/b"], consumes=[Consumes("a")]),
        "a-util": Sub(name="a-util", paths=["src/a/util"]),
    }
    issues = check_cycles(_ctx(subs))
    assert any(i.code == errors.E_CYCLE_DETECTED for i in issues)


def test_schema_unparsed_fix_hint_is_not_about_filename_order():
    """E_SCHEMA_UNPARSED must NOT tell the user to add a filename prefix/order header — that advice
    is for E_SCHEMA_NO_ORDER. A perfectly-named migration with an opaque PL/pgSQL body got the wrong
    remedy before; the hint must talk about SQL dialect/grammar, not ordering."""
    unparsed = _Sym("<unparsed>", "schema_error", 1, exported=False,
                    metadata={"schema_op": "unparsed", "count": 1})
    extracts = {"db/001_create_thing.sql": _ER("db/001_create_thing.sql", "sql", [unparsed])}
    subs = {"db": Sub(name="db", paths=["db"])}
    issues = check_schema(_ctx(subs, extracts, {"db/001_create_thing.sql": "db"}))
    unparsed_issues = [i for i in issues if i.code == errors.E_SCHEMA_UNPARSED]
    assert unparsed_issues, "expected an E_SCHEMA_UNPARSED advisory"
    hint = unparsed_issues[0].fix.lower()
    assert "filename prefix" not in hint and "bounds:order" not in hint
    assert "grammar" in hint or "plpgsql" in hint or "dialect" in hint


def test_build_containment_detects_strict_nesting_only():
    from bounds.core.validate.checks import build_containment

    subs = {
        "auth": Sub(name="auth", paths=["src/auth"]),
        "guards": Sub(name="guards", paths=["src/auth/guards"]),
        "sellers": Sub(name="sellers", paths=["src/sellers"]),
    }
    contains = build_containment(subs)
    assert contains["auth"] == {"guards"}
    assert contains["guards"] == set()
    assert contains["sellers"] == set()


# ===========================================================================
# Check 5b — composition-root detection
# ===========================================================================
def test_composition_root_flags_high_fanin_and_fanout_catchall():
    """A subsystem that both imports nearly every sibling AND is imported by nearly every sibling —
    the catch-all/composition-root signature — is flagged with E_COMPOSITION_ROOT (advisory)."""
    leaves = [f"s{i}" for i in range(10)]
    subs = {}
    # 'root' consumes every leaf (high fan-out) and every leaf consumes 'root' (high fan-in).
    subs["root"] = Sub(name="root", paths=["src"], consumes=[_Con(s) for s in leaves])
    for i, s in enumerate(leaves):
        subs[s] = Sub(name=s, paths=[f"src/{s}"], consumes=[_Con("root")])
    issues = check_composition_root(_ctx(subs))
    flagged = [i for i in issues if i.code == errors.E_COMPOSITION_ROOT]
    assert flagged and flagged[0].subsystem == "root"
    assert all(i.severity == "warning" for i in flagged)  # advisory, never blocks


def test_composition_root_silent_on_normal_hub():
    """A library many things import (high fan-IN only) or an app that imports many (high fan-OUT only)
    is a normal hub, not a catch-all — it must NOT be flagged."""
    leaves = [f"s{i}" for i in range(10)]
    subs = {"lib": Sub(name="lib", paths=["src/lib"])}  # imported by all, imports nothing
    for s in leaves:
        subs[s] = Sub(name=s, paths=[f"src/{s}"], consumes=[_Con("lib")])
    assert check_composition_root(_ctx(subs)) == []


def test_composition_root_silent_on_small_graph():
    """Below the minimum subsystem count the ratios are too noisy; no flag on a tiny repo."""
    subs = {
        "root": Sub(name="root", paths=["src"], consumes=[_Con("a"), _Con("b")]),
        "a": Sub(name="a", paths=["src/a"], consumes=[_Con("root")]),
        "b": Sub(name="b", paths=["src/b"], consumes=[_Con("root")]),
    }
    assert check_composition_root(_ctx(subs)) == []


# ===========================================================================
# Check 6 — framework-aware orphan exemption
# ===========================================================================
def test_orphan_check_exempts_nestjs_framework_entry_export():
    """A NestJS @Controller class (tagged framework_entry by the extractor) is consumed by the
    framework, not a sibling subsystem — so it must NOT be flagged E_ORPHAN_EXPORT even when no
    subsystem consumes it at interface level."""
    # 'api' exposes a controller; 'other' has an interface-tracked edge (flips the orphan check on),
    # but consumes nothing from 'api'. Without the exemption, FollowsController reads as an orphan.
    subs = {
        "api": Sub(name="api", paths=["src/api"], exposes=[Interface("FollowsController")]),
        "other": Sub(name="other", paths=["src/other"], consumes=[Consumes("api", interfaces=["x"])]),
    }
    extracts = {
        "src/api/c.ts": _ER("src/api/c.ts", "typescript",
                            [_Sym("FollowsController", "class", 1, True,
                                  metadata={"framework_entry": "nest_controller"})]),
    }
    issues = check_orphans(_ctx(subs, extracts, {"src/api/c.ts": "api"}))
    assert not any(i.code == errors.E_ORPHAN_EXPORT and "FollowsController" in i.message for i in issues)


def test_orphan_check_still_flags_plain_unconsumed_export():
    """The exemption is narrow: a plain (untagged) unconsumed export is still an orphan."""
    subs = {
        "api": Sub(name="api", paths=["src/api"], exposes=[Interface("plainHelper")]),
        "other": Sub(name="other", paths=["src/other"], consumes=[Consumes("api", interfaces=["x"])]),
    }
    extracts = {
        "src/api/h.ts": _ER("src/api/h.ts", "typescript", [_Sym("plainHelper", "function", 1, True)]),
    }
    issues = check_orphans(_ctx(subs, extracts, {"src/api/h.ts": "api"}))
    assert any(i.code == errors.E_ORPHAN_EXPORT and "plainHelper" in i.message for i in issues)


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
    # Orphans roll up to ONE issue per subsystem (a library's externally-consumed surface would
    # otherwise flood one row per export); the message names the unused exports in its sample.
    subs["user"] = Sub(name="user", consumes=[Consumes("lib", interfaces=["a"])])
    flagged = check_orphans(_ctx(subs))
    assert len(flagged) == 1 and flagged[0].count == 1
    orphan_sample = flagged[0].message.split(":", 1)[1]
    assert "b" in orphan_sample and "a" not in orphan_sample
