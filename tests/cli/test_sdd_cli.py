"""Regression tests for the deterministic SDD command surface."""

from __future__ import annotations

import json

from click.testing import CliRunner

from bounds.cli import main


def _root(root, sdd: str = ""):
    (root / ".bounds" / "manifests").mkdir(parents=True)
    (root / ".bounds" / "root.yaml").write_text(
        'version: "1"\nproject: sdd\nsubsystems: []\n' + sdd,
        encoding="utf-8",
    )


def test_sdd_status_does_not_infer_current_prose_phase(tmp_path, monkeypatch):
    """Status reports configured commands and explicitly refuses to guess workflow progress."""
    _root(tmp_path, "sdd:\n  enabled: true\n  phases: [specify, implement]\n")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["sdd"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert [step["phase"] for step in payload["steps"]] == ["specify", "implement"]
    assert "current" not in payload
    assert "does not infer" in payload["note"]


def test_sdd_phase_lookup_is_available_for_unconfigured_phase(tmp_path, monkeypatch):
    """Explicit lookup remains useful while honestly marking a customized-out phase."""
    _root(tmp_path, "sdd:\n  enabled: true\n  phases: [implement]\n")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["sdd", "--phase", "verify"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["phase"] == "verify"
    assert payload["configured"] is False
    assert payload["command"] == "bounds preflight --ci"


def test_sdd_human_output_is_a_phase_command_table(tmp_path, monkeypatch):
    """The human view stays concise and repeats the no-inference contract."""
    _root(tmp_path, "sdd:\n  enabled: true\n  phases: [implement, verify]\n")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["sdd", "--human"])

    assert result.exit_code == 0
    assert "implement bounds validate --quick" in result.output
    assert "verify    bounds preflight --ci" in result.output
    assert "does not infer" in result.output


def test_sdd_doctor_reports_readiness_not_phase_completion(tmp_path, monkeypatch):
    """Doctor exposes deterministic prerequisites and gates without inventing prose progress."""
    _root(tmp_path, "sdd:\n  enabled: true\n")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["sdd", "--doctor"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["mode"] == "sdd-doctor"
    assert payload["ok"] is False
    assert next(check for check in payload["checks"] if check["name"] == "subsystem_map")["ok"] is False
    assert "not proof" in payload["note"]
