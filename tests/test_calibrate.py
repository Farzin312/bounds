"""Calibration: reconcile manifests against tree-sitter reality."""

from __future__ import annotations

import yaml

from bounds import config
from bounds.calibrate import run_calibrate


def _build(tmp_path):
    src = tmp_path / "src"
    (src / "db").mkdir(parents=True)
    (src / "auth").mkdir(parents=True)
    (src / "db" / "store.py").write_text("def connect():\n    pass\ndef query(sql):\n    pass\n")
    (src / "auth" / "login.py").write_text(
        "from ..db.store import query\n"
        "def login(u):\n    pass\n"
        "def verify(t):\n    pass\n"  # exported but undeclared -> ADD
    )

    cfg = tmp_path / config.BOUNDS_DIR
    (cfg / config.MANIFESTS_DIR).mkdir(parents=True)
    (cfg / config.ROOT_FILE).write_text(
        yaml.safe_dump({"version": "1", "project": "x", "subsystems": ["db", "auth"]})
    )
    (cfg / config.MANIFESTS_DIR / "db.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "db",
                "role": "platform",
                "criticality": "core",
                "paths": ["src/db"],
                "exposes": [
                    {"name": "connect", "kind": "function"},
                    {"name": "query", "kind": "function"},
                    {"name": "DROPME", "kind": "function"},  # stale, unconsumed -> REMOVE
                    {"name": "KEEPME", "kind": "function"},  # stale, consumed -> NEEDS_REVIEW
                    {"name": "SECRET", "kind": "function", "internal": True},  # stale -> EXEMPT
                ],
                "consumes": [],
            },
            sort_keys=False,
        )
    )
    (cfg / config.MANIFESTS_DIR / "auth.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "auth",
                "role": "service",
                "criticality": "leaf",
                "paths": ["src/auth"],
                "exposes": [{"name": "login", "kind": "function"}],
                "consumes": [{"subsystem": "db", "interfaces": ["query", "KEEPME", "GHOST"]}],
            },
            sort_keys=False,
        )
    )
    return tmp_path


def test_calibrate_remove_keep_and_exempt(tmp_path):
    _build(tmp_path)
    result = run_calibrate(tmp_path)
    db = result["subsystems"]["db"]
    assert db["remove_exposes"] == ["DROPME"]          # stale + unconsumed
    assert db["needs_review"] == ["KEEPME"]            # stale but consumed -> never auto-removed
    # SECRET (internal) is exempt: it appears nowhere.
    blob = str(result)
    assert "SECRET" not in blob


def test_calibrate_adds_undeclared_export(tmp_path):
    _build(tmp_path)
    result = run_calibrate(tmp_path)
    auth = result["subsystems"]["auth"]
    assert any(e["name"] == "verify" for e in auth["add_exposes"])


def test_calibrate_flags_ghost_consumes_interface(tmp_path):
    _build(tmp_path)
    result = run_calibrate(tmp_path)
    auth = result["subsystems"]["auth"]
    removed = {tuple(rc["interfaces"]) for rc in auth["remove_consumes"]}
    assert ("GHOST",) in removed  # db never exposes GHOST


def test_calibrate_role_criticality_never_touched(tmp_path):
    _build(tmp_path)
    result = run_calibrate(tmp_path)
    assert "role" not in str(result["subsystems"]["db"])
    assert "criticality" not in str(result["subsystems"]["db"])


def test_calibrate_apply_rewrites_manifests(tmp_path):
    _build(tmp_path)
    run_calibrate(tmp_path, apply=True)
    cfg = tmp_path / config.BOUNDS_DIR / config.MANIFESTS_DIR

    db = yaml.safe_load((cfg / "db.yaml").read_text())
    names = {e["name"] for e in db["exposes"]}
    assert "DROPME" not in names      # removed
    assert "KEEPME" in names          # needs_review -> kept
    assert "SECRET" in names          # internal -> kept
    assert db["role"] == "platform"   # untouched

    auth = yaml.safe_load((cfg / "auth.yaml").read_text())
    auth_names = {e["name"] for e in auth["exposes"]}
    assert "verify" in auth_names     # added
    db_edge = next(c for c in auth["consumes"] if c["subsystem"] == "db")
    assert "GHOST" not in db_edge["interfaces"]  # stale interface pruned


def test_calibrate_single_subsystem_scope(tmp_path):
    _build(tmp_path)
    result = run_calibrate(tmp_path, subsystem="auth")
    assert set(result["subsystems"]) <= {"auth"}


def test_calibrate_apply_drops_fully_stale_edge_to_bare(tmp_path):
    # An edge whose ONLY interface is a ghost must not be left as `interfaces: []`.
    _build(tmp_path)
    cfg = tmp_path / config.BOUNDS_DIR / config.MANIFESTS_DIR
    # auth consumes db with only a ghost interface.
    (cfg / "auth.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "auth", "role": "service", "criticality": "leaf", "paths": ["src/auth"],
                "exposes": [{"name": "login", "kind": "function"}],
                "consumes": [{"subsystem": "db", "interfaces": ["GHOST"]}],
            },
            sort_keys=False,
        )
    )
    run_calibrate(tmp_path, subsystem="auth", apply=True)
    auth = yaml.safe_load((cfg / "auth.yaml").read_text())
    db_edge = next(c for c in auth["consumes"] if c["subsystem"] == "db")
    # Bare edge, not a noisy empty list.
    assert db_edge.get("interfaces", []) == [] and "interfaces" not in db_edge


def test_calibrate_clean_subsystem_absent_from_proposal(tmp_path):
    # A subsystem whose manifest already matches source should not appear in proposals.
    _build(tmp_path)
    # auth declares login but verify is undeclared -> auth will appear; db will appear.
    # After applying, a second calibrate should be a no-op.
    run_calibrate(tmp_path, apply=True)
    result = run_calibrate(tmp_path)
    assert result["summary"]["added"] == 0
    assert result["summary"]["removed"] == 0
