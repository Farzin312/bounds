"""Tests for manifest loading, schema validation, the 6 checks, propagation, cache, and the engine."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bounds import config, errors
from bounds.cache import store
from bounds.manifest import loader, schema
from bounds.models import (
    Consumes,
    ExtractResult,
    ImportRef,
    Interface,
    RootManifest,
    Symbol,
)
from bounds.models import SubsystemCompact as Sub
from bounds.validate import engine
from bounds.validate.checks import (
    CheckContext,
    _find_cycles,
    check_boundary,
    check_contract,
    check_cross_impact,
    check_cycles,
    check_orphans,
    check_structural_drift,
    resolve_import,
)
from bounds.validate.propagation import build_consumer_index, propagate


def _ctx(subsystems, extracts=None, file_owner=None, dirty=None, propagated=None):
    return CheckContext(
        project_root=Path("."),
        root=RootManifest(),
        subsystems=subsystems,
        extracts=extracts or {},
        file_owner=file_owner or {},
        dirty=dirty or set(),
        propagated=propagated or set(),
    )


# ===========================================================================
# Manifest loading
# ===========================================================================
def test_find_root_walks_up(sample_project):
    assert loader.find_root(sample_project / "src" / "auth") == sample_project


def test_find_root_none_when_absent(tmp_path):
    assert loader.find_root(tmp_path) is None


def test_load_all_and_consumed_by(sample_project):
    rootm, subs, issues = loader.load_all(sample_project)
    assert rootm.project == "shop"
    assert set(subs) == {"database", "auth", "api"}
    # consumed_by is the inverse of consumes, auto-filled by the loader
    assert "auth" in subs["database"].consumed_by
    assert "api" in subs["auth"].consumed_by
    assert subs["api"].consumed_by == []


def test_load_root_missing_raises(tmp_path):
    with pytest.raises(errors.BoundsError) as exc:
        loader.load_root(tmp_path)
    assert exc.value.code == errors.E_MANIFEST_NOT_FOUND


def test_load_subsystem_unknown_raises(sample_project):
    with pytest.raises(errors.BoundsError) as exc:
        loader.load_subsystem(sample_project, "ghost")
    assert exc.value.code == errors.E_SUBSYSTEM_NOT_FOUND


# ===========================================================================
# Backward compatibility — legacy `.compact/` layout
# ===========================================================================
def _write_legacy_project(root: Path) -> None:
    """Scaffold a minimal one-subsystem project under the legacy ``.compact/`` dir."""
    (root / ".compact" / "manifests").mkdir(parents=True)
    (root / ".compact" / "root.yaml").write_text(
        'version: "1"\nproject: legacy\nlanguages: [python]\nenforce: "off"\n'
        "subsystems: [models]\n",
        encoding="utf-8",
    )
    (root / ".compact" / "manifests" / "models.yaml").write_text(
        "name: models\nrole: library\ncriticality: core\npaths: [src/models]\n"
        "exposes:\n  - { name: Thing, kind: class }\nconsumes: []\n",
        encoding="utf-8",
    )
    (root / "src" / "models").mkdir(parents=True)
    (root / "src" / "models" / "thing.py").write_text("class Thing:\n    pass\n", encoding="utf-8")


def test_legacy_compact_dir_is_discovered(tmp_path):
    # A project carrying only the pre-rename .compact/ layout is still found and resolved.
    proj = tmp_path / "legacy"
    proj.mkdir()
    _write_legacy_project(proj)
    assert loader.find_root(proj / "src" / "models") == proj
    assert config.uses_legacy_layout(proj) is True
    assert config.config_dir(proj).name == config.LEGACY_DIR


def test_legacy_compact_dir_warns_once(tmp_path, capsys, monkeypatch):
    # Resolving a legacy layout prints exactly one deprecation notice to stderr.
    proj = tmp_path / "legacy"
    proj.mkdir()
    _write_legacy_project(proj)
    # Reset the one-shot guard via monkeypatch so it auto-restores and this test stays
    # order-independent regardless of other legacy-layout tests in the session.
    monkeypatch.setattr(config, "_legacy_warning_emitted", False)
    config.config_dir(proj)
    config.config_dir(proj)  # repeated resolution must not warn again
    err = capsys.readouterr().err
    assert err.count("deprecated") == 1
    assert config.LEGACY_DIR in err and config.BOUNDS_DIR in err


def test_legacy_compact_dir_validates(tmp_path):
    # The validation engine runs end-to-end against a legacy .compact/ project.
    proj = tmp_path / "legacy"
    proj.mkdir()
    _write_legacy_project(proj)
    report = engine.run(proj, mode="full")
    assert report.status == "fresh"
    assert report.errors() == []


# ===========================================================================
# Schema validation
# ===========================================================================
def test_schema_root_missing_required_keys():
    issues = schema.validate_root({})
    codes = {i.code for i in issues}
    assert errors.E_SCHEMA_INVALID in codes
    assert any("version" in i.message for i in issues)
    assert any("project" in i.message for i in issues)


def test_schema_subsystem_bad_role():
    issues = schema.validate_subsystem("x", {"name": "x", "role": "bogus"})
    assert any(i.code == errors.E_SCHEMA_INVALID and "role" in i.message for i in issues)


def test_schema_root_entry_points_must_be_list():
    issues = schema.validate_root(
        {"version": "1", "project": "p", "entry_points": "main.py"}
    )
    assert any(i.code == errors.E_SCHEMA_INVALID and "entry_points" in i.message for i in issues)


def test_schema_root_entry_points_list_ok():
    issues = schema.validate_root(
        {"version": "1", "project": "p", "entry_points": ["main.py", "app.py"]}
    )
    assert issues == []


def test_schema_valid_subsystem_passes():
    issues = schema.validate_subsystem(
        "auth",
        {"name": "auth", "role": "service", "criticality": "core", "paths": ["src/auth"]},
    )
    assert issues == []


# ===========================================================================
# Import resolution
# ===========================================================================
def test_resolve_relative_typescript_index():
    known = {"src/database/index": "src/database/index.ts", "src/auth/index": "src/auth/index.ts"}
    assert resolve_import("src/auth/index.ts", "../database", known) == "src/database/index.ts"


def test_resolve_python_dotted_relative():
    known = {"src/bounds/models": "src/bounds/models.py"}
    assert resolve_import("src/bounds/extract/base.py", "..models", known) == "src/bounds/models.py"


def test_resolve_external_is_none():
    known = {"src/auth/index": "src/auth/index.ts"}
    assert resolve_import("src/auth/index.ts", "express", known) is None


# ===========================================================================
# Check 1 — structural drift
# ===========================================================================
def test_drift_declared_but_missing():
    subs = {"a": Sub(name="a", paths=["x"], exposes=[Interface("foo")])}
    extracts = {"x/f.py": ExtractResult("x/f.py", "python", [Symbol("bar", "function", 1, True)])}
    issues = check_structural_drift(_ctx(subs, extracts, {"x/f.py": "a"}))
    assert any(i.code == errors.E_STRUCTURAL_DRIFT and i.severity == "error" and "foo" in i.message
               for i in issues)


def test_drift_undeclared_extra_on_core_is_info():
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
    # undeclared exports surface as info on leaf/connector too (not just core/unbounded),
    # which is where the most common real drift hides. Still info → never blocks.
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
    # guard: a subsystem that declares *no* exposes (e.g. not yet calibrated) is not
    # spammed with an info per exported symbol — undeclared drift needs a declared set to drift from.
    subs = {"a": Sub(name="a", criticality="leaf", paths=["x"], exposes=[])}
    extracts = {"x/f.py": ExtractResult("x/f.py", "python", [Symbol("extra", "function", 1, True)])}
    issues = check_structural_drift(_ctx(subs, extracts, {"x/f.py": "a"}))
    assert issues == []


# ===========================================================================
# hardening — cycle detection on a deep graph, concurrent cache writes
# ===========================================================================
def test_find_cycles_handles_deep_graph_without_recursionerror():
    # One big cycle 0 -> 1 -> ... -> 1999 -> 0, far deeper than Python's recursion limit.
    # The iterative DFS must enumerate it instead of raising RecursionError.
    n = 2000
    graph = {str(i): [str((i + 1) % n)] for i in range(n)}
    cycles = _find_cycles(graph)
    assert len(cycles) == 1
    assert len(cycles[0]) == n


def test_concurrent_validate_persist_does_not_crash(py_project):
    # Several validate runs racing on the same cache.db must not raise "database is locked":
    # PRAGMA busy_timeout lets them queue, and persist failures are swallowed .
    import threading

    seen: list[Exception] = []

    def run() -> None:
        try:
            engine.run(py_project, mode="full")
        except Exception as exc:  # noqa: BLE001 - the whole point is "no crash"
            seen.append(exc)

    threads = [threading.Thread(target=run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == []


# ===========================================================================
# Check 2 — boundary compliance
# ===========================================================================
def test_boundary_flags_internal_import():
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


# ===========================================================================
# Check 3 — contract compliance
# ===========================================================================
def test_contract_missing_export():
    subs = {
        "database": Sub(name="database", exposes=[Interface("findUser")]),
        "auth": Sub(name="auth", consumes=[Consumes("database", interfaces=["findUser", "ghost"])]),
    }
    issues = check_contract(_ctx(subs))
    assert any(i.code == errors.E_CONTRACT_MISSING_EXPORT and "ghost" in i.message for i in issues)
    assert not any("findUser" in i.message for i in issues)


def test_contract_unresolved_subsystem():
    subs = {"auth": Sub(name="auth", consumes=[Consumes("ghostsub", interfaces=["x"])])}
    issues = check_contract(_ctx(subs))
    assert any(i.code == errors.E_UNRESOLVED_REFERENCE for i in issues)


# ===========================================================================
# Check 4 — cross-subsystem impact
# ===========================================================================
def test_cross_impact_flags_propagated_consumer():
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
    subs = {
        "a": Sub(name="a", consumes=[Consumes("b")]),
        "b": Sub(name="b", consumes=[Consumes("a")]),
    }
    issues = check_cycles(_ctx(subs))
    assert any(i.code == errors.E_CYCLE_DETECTED for i in issues)


def test_no_cycle_in_dag():
    subs = {
        "a": Sub(name="a", consumes=[Consumes("b")]),
        "b": Sub(name="b"),
    }
    assert check_cycles(_ctx(subs)) == []


# ===========================================================================
# Check 6 — orphan detection
# ===========================================================================
def test_orphan_export_flagged():
    subs = {
        "lib": Sub(name="lib", role="library", exposes=[Interface("used"), Interface("orphan")]),
        "user": Sub(name="user", consumes=[Consumes("lib", interfaces=["used"])]),
    }
    issues = check_orphans(_ctx(subs))
    assert any(i.code == errors.E_ORPHAN_EXPORT and "orphan" in i.message for i in issues)
    assert not any("used" in i.message for i in issues)


def test_service_exposes_are_not_orphans():
    subs = {"svc": Sub(name="svc", role="service", exposes=[Interface("main")])}
    assert check_orphans(_ctx(subs)) == []


# ===========================================================================
# Propagation
# ===========================================================================
def test_consumer_index_inverts_consumes():
    subs = {"p": Sub(name="p"), "c": Sub(name="c", consumes=[Consumes("p")])}
    assert build_consumer_index(subs)["p"] == ["c"]


def test_propagate_core_is_transitive():
    subs = {
        "core": Sub(name="core", criticality="core"),
        "mid": Sub(name="mid", consumes=[Consumes("core")]),
        "leaf": Sub(name="leaf", consumes=[Consumes("mid")]),
    }
    assert propagate({"core"}, subs) == {"mid", "leaf"}


def test_propagate_connector_is_one_hop():
    subs = {
        "c": Sub(name="c", criticality="connector"),
        "mid": Sub(name="mid", consumes=[Consumes("c")]),
        "leaf": Sub(name="leaf", consumes=[Consumes("mid")]),
    }
    assert propagate({"c"}, subs) == {"mid"}


def test_propagate_leaf_is_none():
    subs = {
        "l": Sub(name="l", criticality="leaf"),
        "mid": Sub(name="mid", consumes=[Consumes("l")]),
    }
    assert propagate({"l"}, subs) == set()


# ===========================================================================
# Cache
# ===========================================================================
def test_cache_roundtrip(tmp_path):
    r = ExtractResult(
        "a.py", "python",
        [Symbol("f", "function", 1, True)],
        [ImportRef("os", [], 1)],
        content_hash="ch", structure_hash="sh",
    )
    st = store.State()
    st.put(r)
    store.save_state(tmp_path, st)
    reloaded = store.load_state(tmp_path)
    rec = reloaded.get("a.py")
    assert rec is not None
    assert rec.content_hash == "ch" and rec.structure_hash == "sh"
    back = rec.to_result()
    assert back.symbols[0].name == "f"
    assert back.imports[0].module == "os"


def test_cache_load_tolerates_missing(tmp_path):
    assert store.load_state(tmp_path).files == {}


def test_cache_load_tolerates_corrupt(tmp_path):
    (tmp_path / ".bounds").mkdir()
    (tmp_path / ".bounds" / "state.json").write_text("{ not valid json", encoding="utf-8")
    assert store.load_state(tmp_path).files == {}


# ===========================================================================
# Engine (end-to-end on a real project)
# ===========================================================================
def test_engine_full_clean(py_project):
    report = engine.run(py_project, mode="full")
    assert report.status == "fresh"
    assert report.errors() == []
    assert report.stats["files_total"] == 2


def test_engine_cache_accelerates_second_run(py_project):
    first = engine.run(py_project, mode="full")
    assert first.stats["files_parsed"] == 2
    assert first.stats["cache_hits"] == 0
    second = engine.run(py_project, mode="full")
    assert second.stats["files_parsed"] == 0
    assert second.stats["cache_hits"] == 2


def test_engine_quick_detects_dirty_and_propagates(py_project, git_init):
    git_init(py_project)
    engine.run(py_project, mode="full")  # warm the cache
    # structurally change the core `models` subsystem (add a new export)
    (py_project / "src" / "models" / "thing.py").write_text(
        "class Thing:\n    pass\n\n\ndef helper():\n    return 1\n", encoding="utf-8"
    )
    report = engine.run(py_project, mode="quick")
    assert "models" in report.stats["dirty"]
    assert "svc" in report.stats["propagated"]  # core criticality propagates to its consumer
    assert report.ok is True  # quick mode never blocks


def test_engine_enforce_gates_blocking(py_project):
    # Introduce drift: manifest declares Thing, but the source no longer exports it.
    (py_project / "src" / "models" / "thing.py").write_text("x = 1\n", encoding="utf-8")
    off = engine.run(py_project, mode="full", enforce="off")
    assert off.status == "stale"
    assert off.ok is True  # full mode does not block when enforce=off
    on = engine.run(py_project, mode="full", enforce="on")
    assert on.ok is False  # ...but does when enforce=on
    assert any(i.code == errors.E_STRUCTURAL_DRIFT and i.severity == "error" for i in on.issues)


def test_engine_hotfix_never_blocks(py_project):
    report = engine.run(py_project, mode="hotfix")
    assert report.ok is True
    assert report.mode == "hotfix"


def test_engine_entry_point_not_blocking(py_project, git_init):
    # A root entry point + a genuine orphan, both unowned, under --fail-on-unowned:
    # the orphan blocks (error), the entry point is reported but never blocks (warning).
    (py_project / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    (py_project / "orphan.py").write_text("def stray():\n    pass\n", encoding="utf-8")
    rootf = py_project / ".bounds" / "root.yaml"
    rootf.write_text(rootf.read_text(encoding="utf-8") + "entry_points: [app.py]\n", encoding="utf-8")
    git_init(py_project)

    report = engine.run(py_project, mode="full", fail_on_unowned=True)
    assert report.ok is False  # orphan.py still blocks
    assert report.stats["entry_points"] == ["app.py"]
    by_file = {(i.file, i.severity) for i in report.issues if i.code == errors.E_UNOWNED_FILE}
    assert ("orphan.py", "error") in by_file
    assert ("app.py", "warning") in by_file


def test_engine_entry_point_alone_is_clean(py_project, git_init):
    # When the only unowned file is an entry point, --fail-on-unowned does not block.
    (py_project / "manage.py").write_text("def main():\n    pass\n", encoding="utf-8")
    rootf = py_project / ".bounds" / "root.yaml"
    rootf.write_text(rootf.read_text(encoding="utf-8") + "entry_points: [manage.py]\n", encoding="utf-8")
    git_init(py_project)

    report = engine.run(py_project, mode="full", fail_on_unowned=True)
    assert report.ok is True
    assert report.stats["entry_points"] == ["manage.py"]
    assert report.errors() == []
