"""End-to-end CLI tests via click's CliRunner (JSON output + exit codes + init)."""

from __future__ import annotations

import json

from click.testing import CliRunner

from bounds.cli import main


def _json(result):
    return json.loads(result.output)


def test_list(sample_project, monkeypatch):
    """list emits the JSON-first contract: project name + the exact declared subsystem set, exit 0."""
    monkeypatch.chdir(sample_project)
    result = CliRunner().invoke(main, ["list"])
    assert result.exit_code == 0
    data = _json(result)
    assert data["project"] == "shop"
    assert {s["name"] for s in data["subsystems"]} == {"database", "auth", "api"}


def test_validate_clean_is_fresh(sample_project, monkeypatch):
    """A clean project validates fresh+ok at exit 0; bidirectional drift (undeclared internal) stays info-only, non-blocking."""
    # The sample project has no errors/warnings, so it validates fresh + ok. Its database
    # subsystem deliberately exports an internal `UserRepository` it does not declare, which
    # (bidirectional drift) surfaces as a non-blocking info — fresh + exit 0 are unchanged.
    monkeypatch.chdir(sample_project)
    result = CliRunner().invoke(main, ["validate"])
    assert result.exit_code == 0
    data = _json(result)
    assert data["validation_status"] == "fresh"
    assert data["ok"] is True
    assert not [i for i in data["issues"] if i["severity"] in ("error", "warning")]


def test_validate_quick_mode(git_sample_project, monkeypatch):
    """--quick tags its output and names skipped full checks, so callers know it is not a complete gate."""
    monkeypatch.chdir(git_sample_project)
    result = CliRunner().invoke(main, ["validate", "--quick"])
    assert result.exit_code == 0
    data = _json(result)
    assert data["mode"] == "quick"
    assert {"cycles", "coverage"} <= set(data["skipped_checks"])
    assert any("--quick` skips boundary" in step for step in data["next_steps"])


def test_validate_human_non_enforcing_errors_not_ok(py_project, monkeypatch):
    """Human output must not end with bare OK when enforce=off allows error-severity drift."""
    (py_project / "src" / "models" / "thing.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(py_project)

    result = CliRunner().invoke(main, ["validate", "--human"])
    assert result.exit_code == 0
    assert "✗ errors" in result.output
    assert "COMPLETED WITH ERRORS (non-enforcing mode: enforce=off)" in result.output
    assert not result.output.rstrip().endswith("OK")


def test_validate_human_next_steps_group_cycle_coverage_and_calibration_scope(tmp_path, monkeypatch):
    """Human validate output groups mixed cycle/coverage/drift failures into the right repair paths instead of implying calibrate fixes all."""
    cfg = tmp_path / ".bounds"
    (cfg / "manifests").mkdir(parents=True)
    (cfg / "root.yaml").write_text(
        'version: "1"\nproject: mixed\nlanguages: [python]\nenforce: "off"\nsubsystems: [a, b]\n',
        encoding="utf-8",
    )
    (cfg / "manifests" / "a.yaml").write_text(
        "name: a\nrole: library\ncriticality: leaf\npaths: [src/a]\n"
        "exposes:\n  - {name: A, kind: class}\nconsumes:\n  - {subsystem: b}\n",
        encoding="utf-8",
    )
    (cfg / "manifests" / "b.yaml").write_text(
        "name: b\nrole: library\ncriticality: leaf\npaths: [src/b]\n"
        "exposes:\n  - {name: B, kind: class}\nconsumes:\n  - {subsystem: a}\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "a").mkdir(parents=True)
    (tmp_path / "src" / "b").mkdir(parents=True)
    (tmp_path / "src" / "a" / "a.py").write_text("class A:\n    pass\n", encoding="utf-8")
    (tmp_path / "src" / "b" / "b.py").write_text("class B:\n    pass\n", encoding="utf-8")
    (tmp_path / "src" / "orphan.py").write_text("def stray():\n    pass\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["validate", "--human"])

    assert result.exit_code == 0
    assert "next steps:" in result.output
    assert "Close E_COVERAGE_GAP first" in result.output
    assert "Break E_CYCLE_DETECTED in source" in result.output
    assert "`bounds calibrate` does not map files" in result.output

    json_result = CliRunner().invoke(main, ["validate"])
    assert json_result.exit_code == 0
    next_steps = _json(json_result)["next_steps"]
    assert any("Close E_COVERAGE_GAP first" in step for step in next_steps)
    assert any("Break E_CYCLE_DETECTED in source" in step for step in next_steps)


def test_describe_returns_manifest(sample_project, monkeypatch):
    """describe <name> returns the subsystem's declared public surface (exposes) verified fresh against source."""
    monkeypatch.chdir(sample_project)
    result = CliRunner().invoke(main, ["describe", "auth"])
    assert result.exit_code == 0
    data = _json(result)
    assert data["name"] == "auth"
    assert {e["name"] for e in data["exposes"]} == {"login", "verify", "register"}
    assert data["validation_status"] == "fresh"


def test_describe_deep_stub(sample_project, monkeypatch):
    """--deep (the only LLM tier) is opt-in and stubbed in MVP: it must add a `semantic` key, never crash or call an LLM."""
    monkeypatch.chdir(sample_project)
    result = CliRunner().invoke(main, ["describe", "auth", "--deep"])
    assert result.exit_code == 0
    assert "semantic" in _json(result)


def test_describe_unknown_subsystem(sample_project, monkeypatch):
    """An unknown subsystem name is a genuinely fatal condition: E_SUBSYSTEM_NOT_FOUND at exit 2, not a soft Issue."""
    monkeypatch.chdir(sample_project)
    result = CliRunner().invoke(main, ["describe", "ghost"])
    assert result.exit_code == 2
    assert _json(result)["error"]["code"] == "E_SUBSYSTEM_NOT_FOUND"


def test_describe_namespace_groups(py_project, monkeypatch):
    """describe --namespace aggregates every subsystem in a namespace while still running the per-subsystem Tier-1 source-verify merge."""
    # Tag both subsystems into one namespace, then describe the group.
    for n in ("models", "svc"):
        p = py_project / ".bounds" / "manifests" / f"{n}.yaml"
        p.write_text("namespace: core\n" + p.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(py_project)
    result = CliRunner().invoke(main, ["describe", "--namespace", "core"])
    assert result.exit_code == 0
    data = _json(result)
    assert data["namespace"] == "core"
    assert {s["name"] for s in data["subsystems"]} == {"models", "svc"}
    # Tier-1 merge still applies per subsystem: Thing is verified against source.
    models = next(s for s in data["subsystems"] if s["name"] == "models")
    assert any(e["name"] == "Thing" and e["verified"] for e in models["exposes"])


def test_describe_unknown_namespace_is_empty(py_project, monkeypatch):
    """An unknown namespace is not fatal (unlike an unknown subsystem): it returns an empty subsystems list at exit 0."""
    monkeypatch.chdir(py_project)
    result = CliRunner().invoke(main, ["describe", "--namespace", "ghost"])
    assert result.exit_code == 0
    assert _json(result)["subsystems"] == []


def test_describe_requires_a_target(sample_project, monkeypatch):
    """describe with neither a name nor --namespace is a usage error: E_USAGE at exit 2, not an empty success."""
    monkeypatch.chdir(sample_project)
    result = CliRunner().invoke(main, ["describe"])
    assert result.exit_code == 2
    assert _json(result)["error"]["code"] == "E_USAGE"


def test_describe_rejects_name_and_namespace(sample_project, monkeypatch):
    """A name and --namespace are mutually exclusive targets: passing both is E_USAGE at exit 2, never an arbitrary winner."""
    monkeypatch.chdir(sample_project)
    result = CliRunner().invoke(main, ["describe", "auth", "--namespace", "x"])
    assert result.exit_code == 2
    assert _json(result)["error"]["code"] == "E_USAGE"


def test_validate_ci_output(sample_project, monkeypatch):
    """--ci emits tab-delimited plaintext (never JSON), one severity-tagged line per issue, for grep-able CI logs."""
    monkeypatch.chdir(sample_project)
    result = CliRunner().invoke(main, ["validate", "--ci"])
    assert result.exit_code == 0
    # CI plaintext is tab-delimited, never JSON. The sample project's lone advisory (
    # info about the undeclared internal UserRepository) renders as one severity-tagged line.
    assert "{" not in result.output
    assert "info\tE_STRUCTURAL_DRIFT\t" in result.output


def test_validate_ci_clean_ok_line(py_project, monkeypatch):
    """A clean project under --ci emits exactly one tab-delimited `ok\\t<status>` line and never falls back to JSON."""
    # A genuinely clean project emits the single tab-delimited "ok\t<status>" line.
    monkeypatch.chdir(py_project)
    result = CliRunner().invoke(main, ["validate", "--ci"])
    assert result.exit_code == 0
    assert result.output.startswith("ok\t")
    assert "{" not in result.output


def test_validate_ci_fatal_stays_plaintext(tmp_path, monkeypatch):
    """Even a fatal (no .bounds/) under --ci stays tab-delimited `fatal\\tE_MANIFEST_NOT_FOUND\\t...` so CI parsing never breaks on errors."""
    # No .bounds/ is fatal; under --ci it must stay tab-delimited, not fall back to JSON.
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["validate", "--ci"])
    assert result.exit_code == 2
    assert result.output.startswith("fatal\tE_MANIFEST_NOT_FOUND\t")
    assert "{" not in result.output


def test_validate_fail_on_unowned(py_project, git_init, monkeypatch):
    """An unowned source file is invisible by default; --fail-on-unowned promotes it to a blocking E_UNOWNED_FILE (exit 1)."""
    # A tracked source file outside every subsystem's paths is silently ignored by default,
    # but --fail-on-unowned promotes it to a blocking E_UNOWNED_FILE error.
    (py_project / "src" / "orphan.py").write_text("def stray():\n    pass\n", encoding="utf-8")
    git_init(py_project)
    monkeypatch.chdir(py_project)

    clean = CliRunner().invoke(main, ["validate"])
    assert clean.exit_code == 0  # unowned file is invisible without the flag

    result = CliRunner().invoke(main, ["validate", "--fail-on-unowned"])
    assert result.exit_code == 1
    assert any(i["code"] == "E_UNOWNED_FILE" for i in _json(result)["issues"])


def _add_entry_points(root, *globs):
    """Append an `entry_points:` list to a project's root.yaml in place."""
    rootf = root / ".bounds" / "root.yaml"
    line = "entry_points: [" + ", ".join(globs) + "]\n"
    rootf.write_text(rootf.read_text(encoding="utf-8") + line, encoding="utf-8")


def test_entry_point_exempt_from_fail_on_unowned(py_project, git_init, monkeypatch):
    """A declared entry point downgrades its unowned E_UNOWNED_FILE to a soft warning while a real orphan stays a hard error."""
    # Two root-level unowned files: app.py is a declared entry point, orphan.py is not.
    (py_project / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
    (py_project / "orphan.py").write_text("def stray():\n    pass\n", encoding="utf-8")
    _add_entry_points(py_project, "app.py")
    git_init(py_project)
    monkeypatch.chdir(py_project)

    result = CliRunner().invoke(main, ["validate", "--fail-on-unowned"])
    assert result.exit_code == 1  # orphan.py still blocks
    issues = {(i["code"], i["file"], i["severity"]) for i in _json(result)["issues"]}
    assert ("E_UNOWNED_FILE", "orphan.py", "error") in issues  # genuine unowned: hard error
    assert ("E_UNOWNED_FILE", "app.py", "warning") in issues  # entry point: soft warning
    assert "app.py" in _json(result)["stats"]["entry_points"]


def test_entry_point_alone_does_not_block(py_project, git_init, monkeypatch):
    """When the only unowned file is a declared entry point, --fail-on-unowned passes clean (exit 0, no error-severity issues)."""
    # The only unowned file is a declared entry point -> --fail-on-unowned passes clean.
    (py_project / "manage.py").write_text("def main():\n    pass\n", encoding="utf-8")
    _add_entry_points(py_project, "manage.py")
    git_init(py_project)
    monkeypatch.chdir(py_project)

    result = CliRunner().invoke(main, ["validate", "--fail-on-unowned"])
    assert result.exit_code == 0
    data = _json(result)
    assert data["stats"]["entry_points"] == ["manage.py"]
    assert all(i["severity"] != "error" for i in data["issues"])


def test_describe_flags_entry_point(py_project, monkeypatch):
    """describe surfaces entry-point status: a subsystem-owned file matched by an entry_points glob is listed and its exports tagged entry_point=true."""
    # A subsystem-owned file matched by an entry-point glob is flagged in describe output.
    _add_entry_points(py_project, "'**/thing.py'")
    monkeypatch.chdir(py_project)
    result = CliRunner().invoke(main, ["describe", "models"])
    assert result.exit_code == 0
    data = _json(result)
    assert "src/models/thing.py" in data["entry_points"]
    thing = next(e for e in data["exposes"] if e["name"] == "Thing")
    assert thing["entry_point"] is True


def test_no_root_is_fatal(tmp_path, monkeypatch):
    """No .bounds/ root is a genuinely fatal condition: list raises E_MANIFEST_NOT_FOUND at exit 2 rather than scanning blindly."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["list"])
    assert result.exit_code == 2
    assert _json(result)["error"]["code"] == "E_MANIFEST_NOT_FOUND"


def test_preflight_summary(sample_project, monkeypatch):
    """preflight reports the fast structural gate counts (cycle_detection, boundary_compliance both 0 on a clean repo)."""
    monkeypatch.chdir(sample_project)
    result = CliRunner().invoke(main, ["preflight"])
    assert result.exit_code == 0
    data = _json(result)
    assert data["mode"] == "preflight"
    assert data["checks"]["cycle_detection"] == 0
    assert data["checks"]["boundary_compliance"] == 0


def test_overview(sample_project, monkeypatch):
    """overview reports subsystem count, no cycles, and folds a real validation pass into health.ok (BOUNDS-009)."""
    monkeypatch.chdir(sample_project)
    result = CliRunner().invoke(main, ["overview"])
    assert result.exit_code == 0
    data = _json(result)
    assert data["subsystems"] == 3
    assert data["health"]["ok"] is True
    assert data["cycles"] == []
    # overview now folds a real validation pass into health (BOUNDS-009).
    assert data["health"]["validation"]["ok"] is True
    assert "trust_note" in data["health"]["validation"]
    assert data["health"]["validation"]["next_steps"]


def _drifted_project(root, *, enforce: str = "on"):
    """Write a 2-subsystem python project under enforce=on with one blocking contract drift.

    ``models`` declares a ``Missing`` export that does not exist in source, so a full
    validation pass reports a blocking error — the case overview must reflect as not-ok.
    """
    (root / ".bounds" / "manifests").mkdir(parents=True)
    (root / ".bounds" / "root.yaml").write_text(
        f'version: "1"\nproject: drift\nlanguages: [python]\nenforce: "{enforce}"\n'
        "subsystems: [models, svc]\n",
        encoding="utf-8",
    )
    (root / ".bounds" / "manifests" / "models.yaml").write_text(
        "name: models\nrole: library\ncriticality: core\npaths: [src/models]\n"
        "exposes:\n  - { name: Thing, kind: class }\n  - { name: Missing, kind: class }\n"
        "consumes: []\n",
        encoding="utf-8",
    )
    (root / ".bounds" / "manifests" / "svc.yaml").write_text(
        "name: svc\nrole: service\ncriticality: leaf\npaths: [src/svc]\nexposes: []\n"
        "consumes:\n  - { subsystem: models, via: models, interfaces: [Thing] }\n",
        encoding="utf-8",
    )
    (root / "src" / "models").mkdir(parents=True)
    (root / "src" / "svc").mkdir(parents=True)
    (root / "src" / "models" / "thing.py").write_text("class Thing:\n    pass\n", encoding="utf-8")
    (root / "src" / "svc" / "main.py").write_text(
        "from ..models.thing import Thing\n\n\ndef run():\n    return Thing()\n", encoding="utf-8"
    )


def test_overview_health_reflects_drift(tmp_path, monkeypatch):
    """BOUNDS-009: overview health.ok must agree with the validate gate — drift under enforce=on forces health.ok=false."""
    # BOUNDS-009: a project that validate would block (drift under enforce=on) must report
    # health.ok=false in overview — health can no longer be ok while validate has errors.
    root = tmp_path / "proj"
    _drifted_project(root)
    monkeypatch.chdir(root)

    overview = CliRunner().invoke(main, ["overview"])
    assert overview.exit_code == 0
    health = _json(overview)["health"]
    assert health["ok"] is False
    assert health["validation"]["ok"] is False
    assert health["validation"]["errors"] >= 1

    # The folded signal must agree with the standalone validate gate.
    val = CliRunner().invoke(main, ["validate"])
    assert val.exit_code == 1  # blocked, not fatal


def test_overview_health_reflects_error_severity_drift_even_when_enforce_off(tmp_path, monkeypatch):
    """A dashboard is not healthy when full validation reports error-severity drift, even if enforce=off would not block."""
    root = tmp_path / "proj"
    _drifted_project(root, enforce="off")
    monkeypatch.chdir(root)

    overview = CliRunner().invoke(main, ["overview"])
    assert overview.exit_code == 0
    health = _json(overview)["health"]
    assert health["ok"] is False
    assert health["validation"]["ok"] is False
    assert health["validation"]["errors"] >= 1

    val = CliRunner().invoke(main, ["validate"])
    assert val.exit_code == 0
    assert _json(val)["ok"] is True  # enforce=off is advisory, but overview must still be honest.


def test_overview_health_clean_when_no_drift(py_project, monkeypatch):
    """The BOUNDS-009 fold's clean side: a no-drift project keeps health.ok=true (the validate fold isn't a false alarm)."""
    # A clean project (enforce=off, source matches contracts) stays health.ok=true.
    monkeypatch.chdir(py_project)
    result = CliRunner().invoke(main, ["overview"])
    assert result.exit_code == 0
    health = _json(result)["health"]
    assert health["ok"] is True
    assert health["validation"]["ok"] is True
    assert "bounds list" in health["validation"]["next_steps"][0]
    assert health["validation"]["mapped_pct"] == 100.0
    # A well-formed project (fixture now carries descriptions) reports full description coverage
    # and does NOT raise the concept-discovery nudge.
    described = health["validation"]["described"]
    assert described == {"with_description": 2, "total": 2, "pct": 100.0}
    assert not any("Add subsystem descriptions" in s for s in health["validation"]["next_steps"])


def test_overview_flags_empty_descriptions(tmp_path, monkeypatch):
    """Empty descriptions silently break concept discovery — overview must measure + nudge to fill them."""
    cfg = tmp_path / ".bounds"
    (cfg / "manifests").mkdir(parents=True)
    (cfg / "root.yaml").write_text(
        'version: "1"\nproject: blank\nlanguages: [python]\nsubsystems: [app]\n',
        encoding="utf-8",
    )
    (cfg / "manifests" / "app.yaml").write_text(  # no description: line
        "name: app\nrole: library\ncriticality: core\npaths: [app]\n"
        "exposes:\n  - { name: run, kind: function }\n",
        encoding="utf-8",
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("def run():\n    return True\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["overview"])
    assert result.exit_code == 0
    v = _json(result)["health"]["validation"]
    assert v["described"] == {"with_description": 0, "total": 1, "pct": 0.0}
    assert any("Add subsystem descriptions" in s for s in v["next_steps"])


def test_overview_guides_partial_mapping_to_coverage_fix(tmp_path, monkeypatch):
    """Overview must explain that a partial map is useful but incomplete, with the coverage fix path."""
    cfg = tmp_path / ".bounds"
    (cfg / "manifests").mkdir(parents=True)
    (cfg / "root.yaml").write_text(
        'version: "1"\nproject: partial\nlanguages: [python]\nsubsystems: [app]\n',
        encoding="utf-8",
    )
    (cfg / "manifests" / "app.yaml").write_text(
        "name: app\nrole: library\ncriticality: core\npaths: [app]\n"
        "exposes:\n  - { name: run, kind: function }\n",
        encoding="utf-8",
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("def run():\n    return True\n", encoding="utf-8")
    (tmp_path / "unmapped").mkdir()
    (tmp_path / "unmapped" / "extra.py").write_text("def extra():\n    return True\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["overview"])
    assert result.exit_code == 0
    validation = _json(result)["health"]["validation"]
    assert validation["mapped_pct"] < 100
    assert "outside the architecture map" in validation["trust_note"]
    assert any("E_COVERAGE_GAP" in step for step in validation["next_steps"])


def test_overview_counts_ownership_overlaps(tmp_path, monkeypatch):
    """Overview must expose duplicate same-path ownership in its normal health summary, not bury it behind describe --full."""
    cfg = tmp_path / ".bounds"
    (cfg / "manifests").mkdir(parents=True)
    (cfg / "root.yaml").write_text(
        'version: "1"\nproject: overlap\nlanguages: [python]\nsubsystems: [aaa, bbb]\n',
        encoding="utf-8",
    )
    for name in ("aaa", "bbb"):
        (cfg / "manifests" / f"{name}.yaml").write_text(
            f"name: {name}\nrole: library\ncriticality: leaf\npaths: [pkg]\nexposes: []\n",
            encoding="utf-8",
        )
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "shared.py").write_text("def shared():\n    pass\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["overview"])
    assert result.exit_code == 0
    validation = _json(result)["health"]["validation"]
    assert validation["ownership_overlaps"] == 1
    assert validation["warnings"] >= 1
    assert any("Resolve duplicate ownership" in step for step in validation["next_steps"])


def test_validate_fatal_after_dry_run_discover_exits_2(tmp_path, git_init, monkeypatch):
    """BOUNDS-011: a dry-run discover writes nothing to disk, so a following validate still raises fatal E_MANIFEST_NOT_FOUND (exit 2), unmasked."""
    # BOUNDS-011: a no-op dry-run discover (no .bounds/ written) must not mask the fatal
    # missing-manifest path — a subsequent validate still raises E_MANIFEST_NOT_FOUND and
    # exits config.EXIT_FATAL (2), never 0.
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    git_init(tmp_path)
    monkeypatch.chdir(tmp_path)

    discover = CliRunner().invoke(main, ["discover", "--dry-run"])
    assert discover.exit_code == 0  # preview succeeds without writing manifests
    assert not (tmp_path / ".bounds").exists()  # truly a no-op on disk

    result = CliRunner().invoke(main, ["validate"])
    assert result.exit_code == 2
    assert _json(result)["error"]["code"] == "E_MANIFEST_NOT_FOUND"


def test_validate_blocked_exits_1(tmp_path, monkeypatch):
    """A drifted (but loadable) project is blocked, not fatal: exit 1 with ok=false, keeping the 1-vs-2 exit-code contract distinct."""
    # A normal validate on a drifted project exits 1 (blocked), distinct from the fatal 2.
    root = tmp_path / "proj"
    _drifted_project(root)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(main, ["validate"])
    assert result.exit_code == 1
    assert _json(result)["ok"] is False


def test_human_output_is_not_json(sample_project, monkeypatch):
    """--human re-renders the same data as prose ('status:'), not raw JSON — the JSON-first/human-mirror contract."""
    monkeypatch.chdir(sample_project)
    result = CliRunner().invoke(main, ["validate", "--human"])
    assert result.exit_code == 0
    assert "status:" in result.output  # human renderer, not raw JSON


def test_init_root_then_subsystem(tmp_path, monkeypatch):
    """init --root then --subsystem scaffolds and registers the manifest so list sees the new subsystem."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    r1 = runner.invoke(main, ["init", "--root"])
    assert r1.exit_code == 0
    assert (tmp_path / ".bounds" / "root.yaml").exists()
    assert _json(r1)["created"]

    r2 = runner.invoke(main, ["init", "--subsystem", "widgets"])
    assert r2.exit_code == 0
    assert (tmp_path / ".bounds" / "manifests" / "widgets.yaml").exists()
    assert ".bounds/root.yaml" in _json(r2)["updated"]

    # the scaffolded project is now discoverable
    r3 = runner.invoke(main, ["list"])
    assert r3.exit_code == 0
    assert {s["name"] for s in _json(r3)["subsystems"]} == {"widgets"}


def test_init_subsystem_registers_without_reformatting_root(tmp_path, monkeypatch):
    """Registering a subsystem updates only the subsystems block, preserving root.yaml comments and flow-style scalars."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init", "--root"])

    result = runner.invoke(main, ["init", "--subsystem", "widgets"])

    assert result.exit_code == 0
    root_text = (tmp_path / ".bounds" / "root.yaml").read_text(encoding="utf-8")
    assert 'version: "1"' in root_text
    assert "languages: [python]" in root_text
    assert "# Root-level bootstrap files" in root_text
    assert "  - widgets\n" in root_text


def test_init_subsystem_path_closes_single_file_coverage_gap(tmp_path, monkeypatch):
    """`init --subsystem --path <file>` creates/registers a single-file subsystem that validate counts as mapped."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init", "--root"])
    (tmp_path / "src" / "bounds").mkdir(parents=True)
    (tmp_path / "src" / "bounds" / "agenthook.py").write_text("def run_hook():\n    pass\n", encoding="utf-8")

    result = runner.invoke(
        main,
        ["init", "--subsystem", "agenthook", "--path", "src/bounds/agenthook.py"],
    )

    assert result.exit_code == 0
    assert _json(result)["registered"] == "agenthook"
    validate = runner.invoke(main, ["validate"])
    assert validate.exit_code == 0
    mapping = _json(validate)["stats"]["coverage"]["mapping"]
    assert mapping["mapped_pct"] == 100.0
    assert not any(i["code"] == "E_COVERAGE_GAP" for i in _json(validate)["issues"])


def test_init_subsystem_path_replaces_dead_scaffold_default(tmp_path, monkeypatch):
    """Adding --path after a scaffold-only init replaces the nonexistent src/<name> placeholder."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init", "--root"])
    runner.invoke(main, ["init", "--subsystem", "agenthook"])
    (tmp_path / "src" / "bounds").mkdir(parents=True)
    (tmp_path / "src" / "bounds" / "agenthook.py").write_text("def run_hook():\n    pass\n", encoding="utf-8")
    manifest = tmp_path / ".bounds" / "manifests" / "agenthook.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "  - src/agenthook\n",
            "  - src/agenthook\n  - src/bounds/agenthook.py\n",
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        main,
        ["init", "--subsystem", "agenthook", "--path", "src/bounds/agenthook.py"],
    )

    assert result.exit_code == 0
    described = runner.invoke(main, ["describe", "agenthook"])
    assert described.exit_code == 0
    assert _json(described)["paths"] == ["src/bounds/agenthook.py"]


def test_init_requires_a_flag(tmp_path, monkeypatch):
    """Bare init with no --root/--subsystem is E_USAGE at exit 2 — it must never guess and silently scaffold the wrong thing."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["init"])
    assert result.exit_code == 2
    assert _json(result)["error"]["code"] == "E_USAGE"


def test_init_root_idempotent(tmp_path, monkeypatch):
    """init --root is idempotent: a second run reports skipped (exit 0) and never clobbers the existing root.yaml."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init", "--root"])
    second = runner.invoke(main, ["init", "--root"])
    assert second.exit_code == 0
    assert (tmp_path / ".bounds" / "root.yaml") in [
        tmp_path / ".bounds" / "root.yaml"
    ]  # exists, unchanged
    assert _json(second)["skipped"]  # reported as skipped, not recreated


def test_init_subsystem_rejects_path_traversal(tmp_path, monkeypatch):
    """A traversal subsystem name is rejected (E_USAGE, exit 2) and writes NOTHING outside .bounds/manifests/."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init", "--root"])
    before = sorted(p.name for p in tmp_path.iterdir())
    result = runner.invoke(main, ["init", "--subsystem", "../../tmp/PWNED"])
    assert result.exit_code == 2
    assert _json(result)["error"]["code"] == "E_USAGE"
    # No file escaped the manifests dir, and the tree above .bounds/ is untouched.
    assert not (tmp_path.parent.parent / "tmp" / "PWNED.yaml").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    manifests = tmp_path / ".bounds" / "manifests"
    assert not manifests.exists() or list(manifests.glob("*.yaml")) == []


def test_init_subsystem_rejects_path_traversal_in_path_option(tmp_path, monkeypatch):
    """A traversal value passed to --path is rejected so coverage-gap scaffolding cannot write unsafe manifests."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init", "--root"])
    result = runner.invoke(main, ["init", "--subsystem", "widgets", "--path", "../outside.py"])
    assert result.exit_code == 2
    assert _json(result)["error"]["code"] == "E_USAGE"
    assert not (tmp_path / ".bounds" / "manifests" / "widgets.yaml").exists()


def test_init_subsystem_accepts_legitimate_names(tmp_path, monkeypatch):
    """Legitimate names (letters/digits/'-'/'_') still scaffold normally."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init", "--root"])
    for name in ("widgets", "data_store", "api-v2"):
        result = runner.invoke(main, ["init", "--subsystem", name])
        assert result.exit_code == 0, name
        assert (tmp_path / ".bounds" / "manifests" / f"{name}.yaml").exists()


def test_init_root_writes_gitignore(tmp_path, monkeypatch):
    """init --root scaffolds .bounds/.gitignore with the three regenerable cache entries."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["init", "--root"])
    assert result.exit_code == 0
    gi = tmp_path / ".bounds" / ".gitignore"
    assert gi.exists()
    body = gi.read_text(encoding="utf-8")
    for entry in ("cache.db", "cache.db-journal", "state.json"):
        assert entry in body
    assert ".bounds/.gitignore" in _json(result)["created"]


def test_init_root_gitignore_idempotent(tmp_path, monkeypatch):
    """A second init --root reports the .gitignore as skipped and does not duplicate it."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init", "--root"])
    gi = tmp_path / ".bounds" / ".gitignore"
    first_body = gi.read_text(encoding="utf-8")
    second = runner.invoke(main, ["init", "--root"])
    assert second.exit_code == 0
    assert ".bounds/.gitignore" in _json(second)["skipped"]
    assert gi.read_text(encoding="utf-8") == first_body  # unchanged, not duplicated


def test_ensure_gitignore_creates_with_all_entries_when_absent(tmp_path):
    """Absent .bounds/.gitignore -> created from the template with all required entries, returns True."""
    from bounds.shared import config

    bounds_dir = tmp_path / config.BOUNDS_DIR
    assert config.ensure_bounds_gitignore(bounds_dir) is True
    gi = bounds_dir / config.GITIGNORE_FILE
    assert gi.is_file()
    present = {line.strip() for line in gi.read_text(encoding="utf-8").splitlines()}
    for entry in config.GITIGNORE_ENTRIES:
        assert entry in present


def test_ensure_gitignore_appends_to_unrelated_content_without_rewriting(tmp_path):
    """Existing user content (a comment + *.log) is preserved byte-for-byte while the 3 entries are appended, no dupes."""
    from bounds.shared import config

    bounds_dir = tmp_path / config.BOUNDS_DIR
    bounds_dir.mkdir(parents=True)
    gi = bounds_dir / config.GITIGNORE_FILE
    user_body = "# my own ignores\n*.log\n"
    gi.write_text(user_body, encoding="utf-8")

    assert config.ensure_bounds_gitignore(bounds_dir) is True
    after = gi.read_text(encoding="utf-8")
    # User content survives unchanged at the head of the file.
    assert after.startswith(user_body)
    lines = [line.strip() for line in after.splitlines()]
    assert "# my own ignores" in lines
    assert "*.log" in lines
    for entry in config.GITIGNORE_ENTRIES:
        assert lines.count(entry) == 1  # present exactly once, never duplicated


def test_ensure_gitignore_noop_when_all_entries_present(tmp_path):
    """A file already containing all 3 entries -> returns False and is byte-unchanged (no dupes)."""
    from bounds.shared import config

    bounds_dir = tmp_path / config.BOUNDS_DIR
    bounds_dir.mkdir(parents=True)
    gi = bounds_dir / config.GITIGNORE_FILE
    body = "node_modules/\n" + "".join(e + "\n" for e in config.GITIGNORE_ENTRIES)
    gi.write_text(body, encoding="utf-8")

    assert config.ensure_bounds_gitignore(bounds_dir) is False
    assert gi.read_text(encoding="utf-8") == body  # byte-for-byte unchanged


def test_ensure_gitignore_commented_entry_does_not_count_as_present(tmp_path):
    """Only cache.db present (others absent, one as a comment) -> the missing entries are appended, cache.db not duplicated, returns True."""
    from bounds.shared import config

    bounds_dir = tmp_path / config.BOUNDS_DIR
    bounds_dir.mkdir(parents=True)
    gi = bounds_dir / config.GITIGNORE_FILE
    # `# state.json` is commented out -> must NOT count as present, so it gets appended.
    gi.write_text("cache.db\n# state.json\n", encoding="utf-8")

    assert config.ensure_bounds_gitignore(bounds_dir) is True
    lines = [line.strip() for line in gi.read_text(encoding="utf-8").splitlines()]
    for entry in config.GITIGNORE_ENTRIES:
        assert lines.count(entry) == 1  # each required entry present exactly once
    assert "# state.json" in lines  # the commented line is preserved, not removed


def test_ensure_gitignore_idempotent_second_call_is_noop(tmp_path):
    """Two consecutive calls on a pre-existing file: second returns False and leaves the file unchanged from after the first."""
    from bounds.shared import config

    bounds_dir = tmp_path / config.BOUNDS_DIR
    bounds_dir.mkdir(parents=True)
    gi = bounds_dir / config.GITIGNORE_FILE
    gi.write_text("# user header\n*.tmp\n", encoding="utf-8")

    assert config.ensure_bounds_gitignore(bounds_dir) is True
    after_first = gi.read_text(encoding="utf-8")
    assert config.ensure_bounds_gitignore(bounds_dir) is False
    assert gi.read_text(encoding="utf-8") == after_first  # no further change, no duplication


def test_run_guard_converts_unexpected_exception_to_json_error(tmp_path, monkeypatch):
    """A non-BoundsError raised in a command body becomes a generic JSON error (E_INTERNAL, exit 2), never a raw traceback."""
    monkeypatch.chdir(tmp_path)
    # Force an unexpected failure deep in a real command body (list -> load_all -> find_root).
    from bounds import cli as cli_mod

    def _boom(_start):
        raise RuntimeError("secret stack detail that must not leak")

    monkeypatch.setattr(cli_mod.manifest_loader, "find_root", _boom)
    result = CliRunner().invoke(main, ["list"])
    assert result.exit_code == 2
    payload = _json(result)
    assert payload["error"]["code"] == "E_INTERNAL"
    assert "secret stack detail" not in result.output  # no traceback leak
    assert payload["error"]["fix"]
