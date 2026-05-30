"""End-to-end CLI smoke tests for the gen-3 commands (via Click's CliRunner)."""

from __future__ import annotations

import json

from click.testing import CliRunner

from bounds.cli import main


def _invoke(monkeypatch, cwd, args):
    monkeypatch.chdir(cwd)
    return CliRunner().invoke(main, args)


def test_impact_cli(monkeypatch, py_project):
    res = _invoke(monkeypatch, py_project, ["impact", "models"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["subsystem"] == "models"
    assert "svc" in data["transitive_consumers"]
    assert data["blast_radius"] >= 1


def test_impact_unknown_subsystem_is_fatal(monkeypatch, py_project):
    res = _invoke(monkeypatch, py_project, ["impact", "nope"])
    assert res.exit_code == 2
    assert "E_SUBSYSTEM_NOT_FOUND" in res.output


def test_cache_inspect_cli(monkeypatch, py_project):
    # Populate the cache first via a validate run, then inspect it.
    assert _invoke(monkeypatch, py_project, ["validate"]).exit_code in (0, 1)
    res = _invoke(monkeypatch, py_project, ["cache", "--inspect"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["backend"] == "sqlite"
    assert data["files"] >= 1


def test_cache_requires_one_flag(monkeypatch, py_project):
    res = _invoke(monkeypatch, py_project, ["cache"])
    assert res.exit_code == 2
    assert "E_USAGE" in res.output


def test_calibrate_cli(monkeypatch, py_project):
    res = _invoke(monkeypatch, py_project, ["calibrate"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["mode"] == "calibrate"
    # svc's main.py exports `run` but the manifest declares nothing -> proposed add.
    assert any(e["name"] == "run" for e in data["subsystems"].get("svc", {}).get("add_exposes", []))


def test_agent_sync_cli(monkeypatch, py_project):
    res = _invoke(monkeypatch, py_project, ["agent", "--sync", "--claude"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["bounds_md"] == "BOUNDS.md"
    assert (py_project / "BOUNDS.md").is_file()
    assert (py_project / ".claude" / "commands" / "bounds.md").is_file()


def test_agent_requires_one_mode(monkeypatch, py_project):
    res = _invoke(monkeypatch, py_project, ["agent"])
    assert res.exit_code == 2


def test_ci_install_cli(monkeypatch, py_project):
    res = _invoke(monkeypatch, py_project, ["ci", "--install", "--action"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert any("bounds.yml" in p for p in data["created"])
    assert (py_project / ".github" / "workflows" / "bounds.yml").is_file()


def test_ci_needs_install(monkeypatch, py_project):
    res = _invoke(monkeypatch, py_project, ["ci"])
    assert res.exit_code == 2


def test_discover_cli_runs(monkeypatch, py_project):
    res = _invoke(monkeypatch, py_project, ["discover"])
    assert res.exit_code == 0
    assert json.loads(res.output)["mode"] == "discover"
