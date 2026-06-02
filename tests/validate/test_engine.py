"""Tests for the end-to-end validation engine (engine.run modes), propagation, and the extraction cache."""

from __future__ import annotations

from bounds import errors
from bounds.cache import store
from bounds.models import ExtractResult, ImportRef, Symbol
from bounds.models import SubsystemCompact as Sub
from bounds.models import Consumes
from bounds.validate import engine
from bounds.validate.propagation import build_consumer_index, propagate


# ===========================================================================
# hardening — concurrent cache writes
# ===========================================================================
def test_concurrent_validate_persist_does_not_crash(py_project):
    """Several validate runs racing on one cache.db must not raise 'database is locked': busy_timeout queues them and persist failures are swallowed (fail-soft)."""
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
# Propagation
# ===========================================================================
def test_consumer_index_inverts_consumes():
    """build_consumer_index inverts consumes into provider→consumers, the reverse map propagation walks to find affected dependents."""
    subs = {"p": Sub(name="p"), "c": Sub(name="c", consumes=[Consumes("p")])}
    assert build_consumer_index(subs)["p"] == ["c"]


def test_propagate_core_is_transitive():
    """A core subsystem's change propagates transitively through every downstream consumer (unbounded depth)."""
    subs = {
        "core": Sub(name="core", criticality="core"),
        "mid": Sub(name="mid", consumes=[Consumes("core")]),
        "leaf": Sub(name="leaf", consumes=[Consumes("mid")]),
    }
    assert propagate({"core"}, subs) == {"mid", "leaf"}


def test_propagate_connector_is_one_hop():
    """A connector's change propagates exactly one hop — direct consumers only, not their consumers — so blast radius matches criticality depth."""
    subs = {
        "c": Sub(name="c", criticality="connector"),
        "mid": Sub(name="mid", consumes=[Consumes("c")]),
        "leaf": Sub(name="leaf", consumes=[Consumes("mid")]),
    }
    assert propagate({"c"}, subs) == {"mid"}


def test_propagate_leaf_is_none():
    """A leaf's change propagates to nobody (depth 0): leaves are sinks, so their consumers aren't marked stale."""
    subs = {
        "l": Sub(name="l", criticality="leaf"),
        "mid": Sub(name="mid", consumes=[Consumes("l")]),
    }
    assert propagate({"l"}, subs) == set()


# ===========================================================================
# Cache
# ===========================================================================
def test_cache_roundtrip(tmp_path):
    """A FileRecord must survive save/load with both content and structure hashes and its symbols/imports intact — the cache is the source of incremental truth."""
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
    """Loading a cache that was never written returns an empty state, not a crash (fail-soft on first-ever run)."""
    assert store.load_state(tmp_path).files == {}


def test_cache_load_tolerates_corrupt(tmp_path):
    """A corrupt cache file degrades to an empty state rather than crashing — a damaged cache must never break validate."""
    (tmp_path / ".bounds").mkdir()
    (tmp_path / ".bounds" / "state.json").write_text("{ not valid json", encoding="utf-8")
    assert store.load_state(tmp_path).files == {}


# ===========================================================================
# Engine (end-to-end on a real project)
# ===========================================================================
def test_engine_full_clean(py_project):
    """A consistent project validates fresh with zero errors and the expected file count — the end-to-end happy path."""
    report = engine.run(py_project, mode="full")
    assert report.status == "fresh"
    assert report.errors() == []
    assert report.stats["files_total"] == 2


def test_engine_cache_accelerates_second_run(py_project):
    """First run parses every file (0 cache hits); an unchanged second run parses none and hits the cache for all — content-hash reuse works."""
    first = engine.run(py_project, mode="full")
    assert first.stats["files_parsed"] == 2
    assert first.stats["cache_hits"] == 0
    second = engine.run(py_project, mode="full")
    assert second.stats["files_parsed"] == 0
    assert second.stats["cache_hits"] == 2


def test_engine_quick_detects_dirty_and_propagates(py_project, git_init):
    """--quick diffs git, marks the changed core subsystem dirty, propagates to its consumer, and never blocks — the incremental-budget path."""
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
    """enforce drives gating: with drift present, enforce=off reports stale but ok; enforce=on blocks with an E_STRUCTURAL_DRIFT error."""
    # Introduce drift: manifest declares Thing, but the source no longer exports it.
    (py_project / "src" / "models" / "thing.py").write_text("x = 1\n", encoding="utf-8")
    off = engine.run(py_project, mode="full", enforce="off")
    assert off.status == "stale"
    assert off.ok is True  # full mode does not block when enforce=off
    on = engine.run(py_project, mode="full", enforce="on")
    assert on.ok is False  # ...but does when enforce=on
    assert any(i.code == errors.E_STRUCTURAL_DRIFT and i.severity == "error" for i in on.issues)


def test_engine_warn_reports_but_never_blocks(py_project):
    """enforce=warn surfaces the drift (stale + error issue) yet never blocks — not even in preflight, which blocks on errors under on/off."""
    # warn mode surfaces the drift (status stale, error issue present) yet never blocks —
    # not even in preflight, which blocks on errors under on/off.
    (py_project / "src" / "models" / "thing.py").write_text("x = 1\n", encoding="utf-8")
    full = engine.run(py_project, mode="full", enforce="warn")
    assert full.status == "stale"
    assert full.ok is True  # reported...
    assert any(i.code == errors.E_STRUCTURAL_DRIFT and i.severity == "error" for i in full.issues)
    pre = engine.run(py_project, mode="preflight", enforce="warn")
    assert pre.ok is True  # ...but the preflight gate still does not block under warn


def test_engine_hotfix_never_blocks(py_project):
    """hotfix mode always reports ok regardless of state — the explicit escape hatch for emergency changes."""
    report = engine.run(py_project, mode="hotfix")
    assert report.ok is True
    assert report.mode == "hotfix"


def test_engine_entry_point_not_blocking(py_project, git_init):
    """Under --fail-on-unowned a declared entry point is a non-blocking warning while a genuine orphan is a blocking error — entry points are sanctioned roots."""
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
    """When the only unowned file is a declared entry point, --fail-on-unowned does not block — a clean repo stays green."""
    # When the only unowned file is an entry point, --fail-on-unowned does not block.
    (py_project / "manage.py").write_text("def main():\n    pass\n", encoding="utf-8")
    rootf = py_project / ".bounds" / "root.yaml"
    rootf.write_text(rootf.read_text(encoding="utf-8") + "entry_points: [manage.py]\n", encoding="utf-8")
    git_init(py_project)

    report = engine.run(py_project, mode="full", fail_on_unowned=True)
    assert report.ok is True
    assert report.stats["entry_points"] == ["manage.py"]
    assert report.errors() == []
