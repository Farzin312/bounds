"""End-to-end CLI tests via click's CliRunner (JSON output + exit codes + init)."""

from __future__ import annotations

import json

from click.testing import CliRunner

from bounds.cli import main


def _json(result):
    return json.loads(result.output)


def test_list(sample_project, monkeypatch):
    monkeypatch.chdir(sample_project)
    result = CliRunner().invoke(main, ["list"])
    assert result.exit_code == 0
    data = _json(result)
    assert data["project"] == "shop"
    assert {s["name"] for s in data["subsystems"]} == {"database", "auth", "api"}


def test_validate_clean_is_fresh(sample_project, monkeypatch):
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
    monkeypatch.chdir(git_sample_project)
    result = CliRunner().invoke(main, ["validate", "--quick"])
    assert result.exit_code == 0
    assert _json(result)["mode"] == "quick"


def test_describe_returns_manifest(sample_project, monkeypatch):
    monkeypatch.chdir(sample_project)
    result = CliRunner().invoke(main, ["describe", "auth"])
    assert result.exit_code == 0
    data = _json(result)
    assert data["name"] == "auth"
    assert {e["name"] for e in data["exposes"]} == {"login", "verify", "register"}
    assert data["validation_status"] == "fresh"


def test_describe_deep_stub(sample_project, monkeypatch):
    monkeypatch.chdir(sample_project)
    result = CliRunner().invoke(main, ["describe", "auth", "--deep"])
    assert result.exit_code == 0
    assert "semantic" in _json(result)


def test_describe_unknown_subsystem(sample_project, monkeypatch):
    monkeypatch.chdir(sample_project)
    result = CliRunner().invoke(main, ["describe", "ghost"])
    assert result.exit_code == 2
    assert _json(result)["error"]["code"] == "E_SUBSYSTEM_NOT_FOUND"


def test_describe_namespace_groups(py_project, monkeypatch):
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
    monkeypatch.chdir(py_project)
    result = CliRunner().invoke(main, ["describe", "--namespace", "ghost"])
    assert result.exit_code == 0
    assert _json(result)["subsystems"] == []


def test_describe_requires_a_target(sample_project, monkeypatch):
    monkeypatch.chdir(sample_project)
    result = CliRunner().invoke(main, ["describe"])
    assert result.exit_code == 2
    assert _json(result)["error"]["code"] == "E_USAGE"


def test_describe_rejects_name_and_namespace(sample_project, monkeypatch):
    monkeypatch.chdir(sample_project)
    result = CliRunner().invoke(main, ["describe", "auth", "--namespace", "x"])
    assert result.exit_code == 2
    assert _json(result)["error"]["code"] == "E_USAGE"


def test_validate_ci_output(sample_project, monkeypatch):
    monkeypatch.chdir(sample_project)
    result = CliRunner().invoke(main, ["validate", "--ci"])
    assert result.exit_code == 0
    # CI plaintext is tab-delimited, never JSON. The sample project's lone advisory (
    # info about the undeclared internal UserRepository) renders as one severity-tagged line.
    assert "{" not in result.output
    assert "info\tE_STRUCTURAL_DRIFT\t" in result.output


def test_validate_ci_clean_ok_line(py_project, monkeypatch):
    # A genuinely clean project emits the single tab-delimited "ok\t<status>" line.
    monkeypatch.chdir(py_project)
    result = CliRunner().invoke(main, ["validate", "--ci"])
    assert result.exit_code == 0
    assert result.output.startswith("ok\t")
    assert "{" not in result.output


def test_validate_ci_fatal_stays_plaintext(tmp_path, monkeypatch):
    # No .bounds/ is fatal; under --ci it must stay tab-delimited, not fall back to JSON.
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["validate", "--ci"])
    assert result.exit_code == 2
    assert result.output.startswith("fatal\tE_MANIFEST_NOT_FOUND\t")
    assert "{" not in result.output


def test_validate_fail_on_unowned(py_project, git_init, monkeypatch):
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
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["list"])
    assert result.exit_code == 2
    assert _json(result)["error"]["code"] == "E_MANIFEST_NOT_FOUND"


def test_preflight_summary(sample_project, monkeypatch):
    monkeypatch.chdir(sample_project)
    result = CliRunner().invoke(main, ["preflight"])
    assert result.exit_code == 0
    data = _json(result)
    assert data["mode"] == "preflight"
    assert data["checks"]["cycle_detection"] == 0
    assert data["checks"]["boundary_compliance"] == 0


def test_overview(sample_project, monkeypatch):
    monkeypatch.chdir(sample_project)
    result = CliRunner().invoke(main, ["overview"])
    assert result.exit_code == 0
    data = _json(result)
    assert data["subsystems"] == 3
    assert data["health"]["ok"] is True
    assert data["cycles"] == []


def test_human_output_is_not_json(sample_project, monkeypatch):
    monkeypatch.chdir(sample_project)
    result = CliRunner().invoke(main, ["validate", "--human"])
    assert result.exit_code == 0
    assert "status:" in result.output  # human renderer, not raw JSON


def test_init_root_then_subsystem(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    r1 = runner.invoke(main, ["init", "--root"])
    assert r1.exit_code == 0
    assert (tmp_path / ".bounds" / "root.yaml").exists()
    assert _json(r1)["created"]

    r2 = runner.invoke(main, ["init", "--subsystem", "widgets"])
    assert r2.exit_code == 0
    assert (tmp_path / ".bounds" / "manifests" / "widgets.yaml").exists()

    # the scaffolded project is now discoverable
    r3 = runner.invoke(main, ["list"])
    assert r3.exit_code == 0


def test_init_requires_a_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["init"])
    assert result.exit_code == 2
    assert _json(result)["error"]["code"] == "E_USAGE"


def test_init_root_idempotent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init", "--root"])
    second = runner.invoke(main, ["init", "--root"])
    assert second.exit_code == 0
    assert (tmp_path / ".bounds" / "root.yaml") in [
        tmp_path / ".bounds" / "root.yaml"
    ]  # exists, unchanged
    assert _json(second)["skipped"]  # reported as skipped, not recreated
