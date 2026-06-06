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


def test_sdd_conflicting_flags_returns_json_error_not_traceback(tmp_path, monkeypatch):
    """Conflicting flags must go through _run() and produce a structured JSON error, not a traceback."""
    _root(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["sdd", "--status", "--doctor"])

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["error"]["code"] == "E_USAGE"
    assert "--status" in payload["error"]["message"] or "--doctor" in payload["error"]["message"]


def test_sdd_enable_writes_root_yaml_and_returns_configure_payload(tmp_path, monkeypatch):
    """--enable sets enabled: true in root.yaml and returns a structured sdd-configure payload."""
    _root(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["sdd", "--enable"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["mode"] == "sdd-configure"
    assert payload["enabled"] is True
    assert ".bounds/root.yaml" in payload["written"]
    # Verify the file was actually patched.
    import yaml
    raw = yaml.safe_load((tmp_path / ".bounds" / "root.yaml").read_text())
    assert raw["sdd"]["enabled"] is True


def test_sdd_disable_sets_enabled_false(tmp_path, monkeypatch):
    """--disable sets enabled: false without touching other root.yaml keys."""
    _root(tmp_path, "sdd:\n  enabled: true\n  phases: [specify, verify]\n")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["sdd", "--disable"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["enabled"] is False
    import yaml
    raw = yaml.safe_load((tmp_path / ".bounds" / "root.yaml").read_text())
    assert raw["sdd"]["enabled"] is False
    # Other keys must be preserved.
    assert raw["sdd"]["phases"] == ["specify", "verify"]


def test_sdd_enable_with_phases_replaces_phase_list(tmp_path, monkeypatch):
    """--enable --phases restricts the configured phase subset."""
    _root(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["sdd", "--enable", "--phases", "specify,implement,verify"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["phases"] == ["specify", "implement", "verify"]


def test_sdd_add_phase_inserts_in_canonical_order(tmp_path, monkeypatch):
    """--add-phase inserts the phase into the canonical ordered list."""
    _root(tmp_path, "sdd:\n  enabled: true\n  phases: [specify, verify]\n")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["sdd", "--add-phase", "implement"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    # specify → implement → verify is canonical order.
    assert payload["phases"] == ["specify", "implement", "verify"]


def test_sdd_remove_phase_drops_the_phase(tmp_path, monkeypatch):
    """--remove-phase removes the given phase from the configured list."""
    _root(tmp_path, "sdd:\n  enabled: true\n  phases: [specify, implement, verify]\n")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["sdd", "--remove-phase", "implement"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert "implement" not in payload["phases"]
    assert payload["phases"] == ["specify", "verify"]


def test_sdd_conflicting_write_flags_returns_usage_error(tmp_path, monkeypatch):
    """--enable and --disable together must produce a structured E_USAGE error."""
    _root(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["sdd", "--enable", "--disable"])

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["error"]["code"] == "E_USAGE"


def test_sdd_phases_without_enable_returns_usage_error(tmp_path, monkeypatch):
    """--phases alone (without --enable) must produce a structured E_USAGE error."""
    _root(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["sdd", "--phases", "specify,verify"])

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["error"]["code"] == "E_USAGE"


def test_sdd_mixing_read_and_write_flags_returns_usage_error(tmp_path, monkeypatch):
    """Combining a read flag (--status) with a write flag (--enable) must produce E_USAGE."""
    _root(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["sdd", "--status", "--enable"])

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["error"]["code"] == "E_USAGE"


def test_sdd_add_unknown_phase_returns_usage_error(tmp_path, monkeypatch):
    """--add-phase with an invalid phase name must produce a structured E_USAGE error."""
    _root(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["sdd", "--add-phase", "notaphase"])

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["error"]["code"] == "E_USAGE"
    assert "notaphase" in payload["error"]["message"]
