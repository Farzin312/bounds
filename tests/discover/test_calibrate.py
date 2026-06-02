"""Calibration: reconcile manifests against tree-sitter reality."""

from __future__ import annotations

import yaml

from bounds import config
from bounds.calibrate import check_drift, dump_baseline, run_calibrate


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
    """Stale exposes split three ways: unconsumed->remove, consumed->needs_review, internal->exempt (never proposed)."""
    _build(tmp_path)
    result = run_calibrate(tmp_path)
    db = result["subsystems"]["db"]
    assert db["remove_exposes"] == ["DROPME"]          # stale + unconsumed
    assert db["needs_review"] == ["KEEPME"]            # stale but consumed -> never auto-removed
    # SECRET (internal) is exempt: it appears nowhere.
    blob = str(result)
    assert "SECRET" not in blob


def test_calibrate_adds_undeclared_export(tmp_path):
    """A tree-sitter-found export not in the manifest (`verify`) surfaces as an add_exposes proposal."""
    _build(tmp_path)
    result = run_calibrate(tmp_path)
    auth = result["subsystems"]["auth"]
    assert any(e["name"] == "verify" for e in auth["add_exposes"])


def test_calibrate_flags_ghost_consumes_interface(tmp_path):
    """A consumes interface the producer never exposes (GHOST) is flagged for removal — dangling edges drift."""
    _build(tmp_path)
    result = run_calibrate(tmp_path)
    auth = result["subsystems"]["auth"]
    removed = {tuple(rc["interfaces"]) for rc in auth["remove_consumes"]}
    assert ("GHOST",) in removed  # db never exposes GHOST


def test_calibrate_adds_imported_interfaces_to_existing_edge(tmp_path):
    """A named import across an existing bare consume edge should enrich that edge with the interface name."""
    _build(tmp_path)
    cfg = tmp_path / config.BOUNDS_DIR / config.MANIFESTS_DIR
    auth_path = cfg / "auth.yaml"
    auth = yaml.safe_load(auth_path.read_text(encoding="utf-8"))
    auth["consumes"] = [{"subsystem": "db"}]
    auth_path.write_text(yaml.safe_dump(auth, sort_keys=False), encoding="utf-8")

    result = run_calibrate(tmp_path, subsystem="auth")
    auth_proposal = result["subsystems"]["auth"]
    assert auth_proposal["add_consume_interfaces"] == [
        {"subsystem": "db", "interfaces": ["query"]}
    ]

    run_calibrate(tmp_path, subsystem="auth", apply=True)
    updated = yaml.safe_load(auth_path.read_text(encoding="utf-8"))
    db_edge = next(c for c in updated["consumes"] if c["subsystem"] == "db")
    assert db_edge["interfaces"] == ["query"]


def test_calibrate_role_criticality_never_touched(tmp_path):
    """Calibrate reconciles only exposes/consumes vs source — human-declared role/criticality are never proposed."""
    _build(tmp_path)
    result = run_calibrate(tmp_path)
    assert "role" not in str(result["subsystems"]["db"])
    assert "criticality" not in str(result["subsystems"]["db"])


def test_calibrate_apply_rewrites_manifests(tmp_path):
    """apply=True persists the full proposal to YAML: DROPME removed, KEEPME/SECRET kept, verify added, GHOST pruned, role intact."""
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
    """subsystem='auth' confines the proposal to that subsystem — scoped calibration never touches others."""
    _build(tmp_path)
    result = run_calibrate(tmp_path, subsystem="auth")
    assert set(result["subsystems"]) <= {"auth"}


def test_calibrate_apply_drops_fully_stale_edge_to_bare(tmp_path):
    """An edge whose only interface is a ghost collapses to a bare edge (key omitted), never a noisy `interfaces: []`."""
    # An edge whose ONLY interface is a ghost must not be left as `interfaces: []`.
    _build(tmp_path)
    cfg = tmp_path / config.BOUNDS_DIR / config.MANIFESTS_DIR
    (tmp_path / "src" / "auth" / "login.py").write_text(
        "def login(u):\n    pass\n",
        encoding="utf-8",
    )
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
    """Calibrate is idempotent: after apply, a second run proposes zero adds/removes — no phantom drift on clean manifests."""
    # A subsystem whose manifest already matches source should not appear in proposals.
    _build(tmp_path)
    # auth declares login but verify is undeclared -> auth will appear; db will appear.
    # After applying, a second calibrate should be a no-op.
    run_calibrate(tmp_path, apply=True)
    result = run_calibrate(tmp_path)
    assert result["summary"]["added"] == 0
    assert result["summary"]["removed"] == 0


def _set_auth_consumes(tmp_path, edges):
    cfg = tmp_path / config.BOUNDS_DIR / config.MANIFESTS_DIR
    auth = yaml.safe_load((cfg / "auth.yaml").read_text())
    auth["consumes"] = edges
    (cfg / "auth.yaml").write_text(yaml.safe_dump(auth, sort_keys=False), encoding="utf-8")


def test_calibrate_surfaces_consumes_to_unknown_subsystem(tmp_path):
    """A consumes edge naming a subsystem that doesn't exist is SURFACED (not silently left, the
    old behaviour): it appears in the proposal's `unknown_consumes` and the summary count, giving
    calibrate a fix path for the dangling refs that otherwise keep validate stuck on 'unresolved'."""
    _build(tmp_path)
    _set_auth_consumes(tmp_path, [
        {"subsystem": "db", "interfaces": ["query"]},
        {"subsystem": "ghostsub", "interfaces": ["x"]},
    ])
    result = run_calibrate(tmp_path, subsystem="auth")
    assert result["subsystems"]["auth"]["unknown_consumes"] == ["ghostsub"]
    assert result["summary"]["consumes_unknown"] == 1


def test_calibrate_prune_unknown_removes_dangling_edge_only_when_asked(tmp_path):
    """Plain --apply KEEPS a dangling consumes edge (it might be a genuine forward reference);
    --prune-unknown --apply removes it while keeping real edges. Forward-ref workflow preserved by
    default, opt-in cleanup available. Also exercises the prune-only apply path (no other change)."""
    _build(tmp_path)
    cfg = tmp_path / config.BOUNDS_DIR / config.MANIFESTS_DIR

    # Default apply: the ghost edge survives (could be a forward reference).
    _set_auth_consumes(tmp_path, [{"subsystem": "db", "interfaces": ["query"]}, {"subsystem": "ghostsub"}])
    run_calibrate(tmp_path, subsystem="auth", apply=True)
    auth = yaml.safe_load((cfg / "auth.yaml").read_text())
    assert any(c["subsystem"] == "ghostsub" for c in auth["consumes"])

    # Opt-in prune: ghost edge removed, the real db edge kept.
    _set_auth_consumes(tmp_path, [{"subsystem": "db", "interfaces": ["query"]}, {"subsystem": "ghostsub"}])
    run_calibrate(tmp_path, subsystem="auth", apply=True, prune_unknown=True)
    auth = yaml.safe_load((cfg / "auth.yaml").read_text())
    names = {c["subsystem"] for c in auth["consumes"]}
    assert "ghostsub" not in names and "db" in names


# ---- Unsupported-language durability (Go/Rust/Java — no adapter) ----
def _build_unsupported(tmp_path):
    """A Go subsystem (unsupported language) with hand-authored exposes + a pure-Python subsystem.

    Bounds has no Go adapter, so it extracts nothing from ``payments`` — its exposes are
    unverifiable and must never be auto-removed. ``db`` is pure supported source with a genuinely
    stale expose that must still be proposed for removal (regression guard).
    """
    (tmp_path / "services" / "payments").mkdir(parents=True)
    (tmp_path / "services" / "payments" / "charge.go").write_text(
        "package payments\nfunc Charge() error { return nil }\nfunc Refund() error { return nil }\n"
    )
    (tmp_path / "src" / "db").mkdir(parents=True)
    (tmp_path / "src" / "db" / "store.py").write_text("def connect():\n    pass\n")

    cfg = tmp_path / config.BOUNDS_DIR
    (cfg / config.MANIFESTS_DIR).mkdir(parents=True)
    (cfg / config.ROOT_FILE).write_text(
        yaml.safe_dump({"version": "1", "project": "x", "languages": ["go", "python"],
                        "subsystems": ["payments", "db"]})
    )
    (cfg / config.MANIFESTS_DIR / "payments.yaml").write_text(
        yaml.safe_dump(
            {"name": "payments", "role": "library", "criticality": "core",
             "paths": ["services/payments"],
             "exposes": [{"name": "Charge", "kind": "function"},
                         {"name": "Refund", "kind": "function"}],
             "consumes": []},
            sort_keys=False,
        )
    )
    (cfg / config.MANIFESTS_DIR / "db.yaml").write_text(
        yaml.safe_dump(
            {"name": "db", "role": "platform", "criticality": "core", "paths": ["src/db"],
             "exposes": [{"name": "connect", "kind": "function"},
                         {"name": "DROPME", "kind": "function"}],  # stale, unconsumed -> REMOVE
             "consumes": []},
            sort_keys=False,
        )
    )
    return tmp_path


def test_calibrate_never_removes_unsupported_language_exposes(tmp_path):
    """A Go (unsupported) subsystem's hand-authored exposes are UNVERIFIABLE — Bounds has no adapter,
    so a 'declared but not found' expose is never proven stale: it goes to needs_review, never
    remove_exposes (no data loss on hand-authored unsupported-language manifests)."""
    _build_unsupported(tmp_path)
    result = run_calibrate(tmp_path)
    # payments has only needs_review entries (no add/remove/consumes), so _has_changes is False and
    # it drops from proposals entirely — the point: zero remove_exposes for it anywhere.
    blob = str(result["subsystems"])
    payments = result["subsystems"].get("payments", {})
    assert payments.get("remove_exposes", []) == []      # never auto-removed
    assert "remove_exposes" not in blob or "Charge" not in str(payments.get("remove_exposes", []))
    assert result["summary"]["removed"] == 1  # only db's DROPME, not the Go exposes


def test_calibrate_pure_supported_subsystem_still_removes_stale_expose(tmp_path):
    """Regression guard: the unsupported-language protection must NOT over-protect — a pure
    supported-language subsystem (db) with a genuinely stale expose (DROPME) is STILL proposed for
    removal."""
    _build_unsupported(tmp_path)
    result = run_calibrate(tmp_path)
    db = result["subsystems"]["db"]
    assert db["remove_exposes"] == ["DROPME"]


def test_calibrate_unsupported_exposes_surfaced_as_needs_review(tmp_path):
    """The protected exposes are not silently dropped — they surface as needs_review so a human can
    confirm them, exactly like a consumed-but-vanished contract."""
    from bounds.calibrate import _calibrate_one
    from bounds.extract.scan import extract_project, subsystems_with_unsupported_source
    from bounds.ignore import load_matcher
    from bounds.manifest import loader as ml
    from bounds.validate.checks import index_extracts

    _build_unsupported(tmp_path)
    _r, subs, _ = ml.load_all(tmp_path)
    m = load_matcher(tmp_path)
    fo, ex, gen = extract_project(tmp_path, subs, m)
    uns = subsystems_with_unsupported_source(tmp_path, subs, m)
    kn, sx = index_extracts(ex)
    p = _calibrate_one("payments", subs, fo, ex, gen, kn, sx, {}, None, uns)
    assert p["remove_exposes"] == []
    assert p["needs_review"] == ["Charge", "Refund"]


# ---- Drift baseline + check (the freshness gate) ----
def test_check_without_baseline_flags_all_drift(tmp_path):
    """With no baseline file, the freshness gate treats all existing drift as new (ok=False) and emits agent-structured items."""
    _build(tmp_path)  # the fixture carries real drift (verify undeclared, DROPME stale, ...)
    result = check_drift(tmp_path)
    assert result["mode"] == "calibrate-check"
    assert result["has_baseline"] is False
    assert result["ok"] is False
    assert result["new_count"] > 0
    # Each item is structured for an agent, not a raw key.
    assert all({"subsystem", "change", "target"} <= set(item) for item in result["new_drift"])


def test_baseline_then_check_is_clean(tmp_path):
    """After dumping a baseline that captures all current drift, the check passes (ok=True, zero new) — accepted drift is silenced."""
    _build(tmp_path)
    dumped = dump_baseline(tmp_path)
    assert dumped["drift_count"] > 0
    assert (tmp_path / config.BOUNDS_DIR / config.DRIFT_BASELINE_FILE).is_file()
    # With every existing item baselined, the check passes (nothing NEW above baseline).
    result = check_drift(tmp_path)
    assert result["has_baseline"] is True
    assert result["ok"] is True
    assert result["new_count"] == 0


def test_check_flags_only_new_drift_above_baseline(tmp_path):
    """Drift introduced after baselining (a new export) is the sole new_drift item — the gate fires only on genuinely new drift."""
    _build(tmp_path)
    dump_baseline(tmp_path)
    # Introduce NEW drift: add an undeclared export to db.
    (tmp_path / "src" / "db" / "store.py").write_text(
        "def connect():\n    pass\ndef query(sql):\n    pass\ndef brand_new():\n    pass\n"
    )
    result = check_drift(tmp_path)
    assert result["ok"] is False
    assert result["new_count"] == 1
    item = result["new_drift"][0]
    assert item["subsystem"] == "db" and item["change"] == "add_expose" and item["target"] == "brand_new"


def test_malformed_baseline_is_treated_as_absent(tmp_path):
    """Fail-soft: a corrupt baseline file degrades to has_baseline=False (drift counted as new) instead of raising or falsely passing."""
    # A corrupt/hand-broken baseline must not be reported as a real baseline (which would
    # imply "no new drift above it"); it fails soft to has_baseline=False, never raising.
    _build(tmp_path)
    (tmp_path / config.BOUNDS_DIR / config.DRIFT_BASELINE_FILE).write_text("{ not valid json")
    result = check_drift(tmp_path)
    assert result["has_baseline"] is False
    assert result["ok"] is False          # existing drift counts as new (no usable baseline)
    assert result["new_count"] > 0


def test_resolving_drift_does_not_fail_check(tmp_path):
    """Fixing drift (fewer keys than baseline) passes the check and reports resolved_count>0 — shrinking drift is never a failure."""
    _build(tmp_path)
    dump_baseline(tmp_path)
    # Resolve all drift by applying the reconciliation; fewer keys than baseline must pass.
    run_calibrate(tmp_path, apply=True)
    result = check_drift(tmp_path)
    assert result["ok"] is True
    assert result["resolved_count"] > 0
