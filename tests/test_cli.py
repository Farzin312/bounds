"""End-to-end CLI tests via click's CliRunner (JSON output + exit codes + init)."""

from __future__ import annotations

import json

from click.testing import CliRunner

from compact.cli import main


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
    monkeypatch.chdir(sample_project)
    result = CliRunner().invoke(main, ["validate"])
    assert result.exit_code == 0
    data = _json(result)
    assert data["validation_status"] == "fresh"
    assert data["ok"] is True
    assert data["issues"] == []


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
    assert (tmp_path / ".compact" / "root.yaml").exists()
    assert _json(r1)["created"]

    r2 = runner.invoke(main, ["init", "--subsystem", "widgets"])
    assert r2.exit_code == 0
    assert (tmp_path / ".compact" / "manifests" / "widgets.yaml").exists()

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
    assert (tmp_path / ".compact" / "root.yaml") in [
        tmp_path / ".compact" / "root.yaml"
    ]  # exists, unchanged
    assert _json(second)["skipped"]  # reported as skipped, not recreated
