"""Tests for `bounds guide` — the state-aware setup checklist."""

from __future__ import annotations

import json

from click.testing import CliRunner

from bounds import guide
from bounds.cli import main

_ROOT_YAML = ('version: "1"\nproject: x\nlanguages: [python]\n'
              'enforce: "on"\nsubsystems: []\n')


def test_guide_fresh_project_all_todo(tmp_path):
    """A fresh project shows all setup steps (init/discover/coverage/agents/ci) undone, with next pointing at the first one — the state-aware onboarding contract."""
    payload = guide.run_guide(tmp_path)
    assert payload["mode"] == "guide"
    assert payload["complete"] is False
    assert [s["id"] for s in payload["steps"]] == [
        "init", "discover", "coverage", "agents", "ci"
    ]
    assert all(not s["done"] for s in payload["steps"])
    assert payload["next"] == "bounds init --root"  # first undone step
    assert payload["daily"]  # daily commands always present


def test_guide_after_init_points_to_discover(tmp_path):
    """guide reads on-disk state: with root.yaml present but no subsystems mapped, init is done and next advances to `discover --apply`."""
    (tmp_path / ".bounds").mkdir()
    (tmp_path / ".bounds" / "root.yaml").write_text(_ROOT_YAML, encoding="utf-8")
    (tmp_path / ".bounds" / "manifests").mkdir()
    payload = guide.run_guide(tmp_path)
    done = {s["id"]: s["done"] for s in payload["steps"]}
    assert done["init"] is True
    assert done["discover"] is False  # no subsystems mapped yet
    assert payload["next"] == "bounds discover --apply"


def test_guide_coverage_step_surfaces_unsupported_gap(tmp_path):
    """With subsystems mapped but an unsupported-language file unmapped, the coverage step is
    not-done and its `why` names the gap and the durable hand-authored fix — so an agent running
    `guide` is told loudly what's still dark, not just that discover ran."""
    cfg = tmp_path / ".bounds"
    (cfg / "manifests").mkdir(parents=True)
    (cfg / "root.yaml").write_text(
        'version: "1"\nproject: poly\nlanguages: [python]\nsubsystems: [app]\n', encoding="utf-8")
    (cfg / "manifests" / "app.yaml").write_text(
        "name: app\nrole: library\ncriticality: leaf\npaths: [app]\nexposes: []\n", encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "m.py").write_text("def f():\n    pass\n", encoding="utf-8")
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "main.go").write_text("package main\nfunc F() {}\n", encoding="utf-8")

    cov_step = next(s for s in guide.run_guide(tmp_path)["steps"] if s["id"] == "coverage")
    assert cov_step["done"] is False
    why = cov_step["why"].lower()
    assert "go" in why and "unsupported" in why and "durable" in why


def test_guide_coverage_step_done_when_fully_mapped(tmp_path):
    """When every library file is mapped, the coverage step reads done (no false gap)."""
    cfg = tmp_path / ".bounds"
    (cfg / "manifests").mkdir(parents=True)
    (cfg / "root.yaml").write_text(
        'version: "1"\nproject: full\nlanguages: [python]\nsubsystems: [app]\n', encoding="utf-8")
    (cfg / "manifests" / "app.yaml").write_text(
        "name: app\nrole: library\ncriticality: leaf\npaths: [app]\nexposes: []\n", encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "m.py").write_text("def f():\n    pass\n", encoding="utf-8")

    cov_step = next(s for s in guide.run_guide(tmp_path)["steps"] if s["id"] == "coverage")
    assert cov_step["done"] is True


def test_guide_detects_ci_gate(tmp_path):
    """guide marks the ci step done when a .github/workflows/bounds.yml gate exists — it detects real setup, not just self-reported progress."""
    (tmp_path / ".bounds").mkdir()
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "bounds.yml").write_text("name: bounds\n", encoding="utf-8")
    ci_step = next(s for s in guide.run_guide(tmp_path)["steps"] if s["id"] == "ci")
    assert ci_step["done"] is True


def test_guide_detects_ci_in_precommit(tmp_path):
    """guide also counts a pre-commit hook (bounds-preflight in .pre-commit-config.yaml) as the ci gate — the gate need not be a GitHub workflow."""
    (tmp_path / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n      - id: bounds-preflight\n", encoding="utf-8")
    ci_step = next(s for s in guide.run_guide(tmp_path)["steps"] if s["id"] == "ci")
    assert ci_step["done"] is True


def test_guide_cli_is_json_when_piped(tmp_path, monkeypatch):
    """When piped (non-TTY), guide emits the JSON contract (mode/steps/daily) by default — agents get JSON, never the prose checklist."""
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(main, ["guide"])  # CliRunner is non-TTY → JSON
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["mode"] == "guide" and "steps" in data and "daily" in data


def test_guide_includes_sdd_track_when_root_enables_it(tmp_path):
    """An enabled root sdd block adds an SDD phase track to the same JSON payload; absent config keeps today's guide shape."""
    cfg = tmp_path / ".bounds"
    (cfg / "manifests").mkdir(parents=True)
    (cfg / "root.yaml").write_text(
        'version: "1"\nproject: sdd\nsubsystems: []\nsdd:\n'
        "  enabled: true\n  agent: codex\n  phases: [specify, plan, implement, verify]\n",
        encoding="utf-8",
    )

    payload = guide.run_guide(tmp_path)
    assert payload["sdd"]["enabled"] is True
    assert payload["sdd"]["agent"] == "codex"
    assert [s["phase"] for s in payload["sdd"]["steps"]] == [
        "specify",
        "plan",
        "implement",
        "verify",
    ]
    assert "bounds validate --quick" == payload["sdd"]["freshness"]["during_implementation"]


def test_guide_sdd_flag_forces_preview_without_root_config(tmp_path, monkeypatch):
    """`bounds guide --sdd` shows the SDD track as a preview without making SDD globally enabled."""
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(main, ["guide", "--sdd"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["sdd"]["enabled"] is False
    assert data["sdd"]["forced"] is True
    assert [s["phase"] for s in data["sdd"]["steps"]] == [
        "specify",
        "clarify",
        "plan",
        "tasks",
        "analyze",
        "implement",
        "verify",
    ]


def test_guide_sdd_empty_phase_list_is_respected(tmp_path):
    """An explicit empty phase list is a real customization, not a signal to fall back to all phases."""
    cfg = tmp_path / ".bounds"
    cfg.mkdir()
    (cfg / "root.yaml").write_text(
        'version: "1"\nproject: sdd\nsdd:\n  enabled: true\n  phases: []\n',
        encoding="utf-8",
    )

    payload = guide.run_guide(tmp_path)
    assert payload["sdd"]["phases"] == []
    assert payload["sdd"]["steps"] == []


def _complete_project(tmp_path, agentsync_mod):
    """Scaffold a fully-wired project and stub out agent detection so all 5 steps are done."""
    cfg = tmp_path / ".bounds"
    (cfg / "manifests").mkdir(parents=True)
    (cfg / "root.yaml").write_text(
        'version: "1"\nproject: full\nlanguages: [python]\nsubsystems: [app]\n',
        encoding="utf-8",
    )
    (cfg / "manifests" / "app.yaml").write_text(
        "name: app\nrole: library\ncriticality: leaf\npaths: [app]\nexposes: []\n",
        encoding="utf-8",
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "m.py").write_text("def f():\n    pass\n", encoding="utf-8")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "bounds.yml").write_text("name: bounds\n", encoding="utf-8")
    # Stub the agent check so the agents step reads as done without needing a wired AGENTS.md.
    agentsync_mod.run_agent = lambda *a, **kw: {"ok": True, "configured": ["codex"]}


def test_guide_complete_includes_optional_features(tmp_path, monkeypatch):
    """When all 5 required steps are done, guide includes an 'optional' section listing SDD and agent invocation."""
    from bounds import agentsync
    monkeypatch.chdir(tmp_path)
    _complete_project(tmp_path, agentsync)

    payload = guide.run_guide(tmp_path)

    assert payload["complete"] is True
    assert "optional" in payload
    ids = [f["id"] for f in payload["optional"]]
    assert "sdd" in ids
    assert "invocation" in ids


def test_guide_complete_optional_sdd_shows_enable_command_when_disabled(tmp_path, monkeypatch):
    """When SDD is disabled, the optional SDD entry points at `bounds sdd --enable` and current='disabled'."""
    from bounds import agentsync
    monkeypatch.chdir(tmp_path)
    _complete_project(tmp_path, agentsync)

    payload = guide.run_guide(tmp_path)

    sdd_entry = next(f for f in payload["optional"] if f["id"] == "sdd")
    assert sdd_entry["enabled"] is False
    assert sdd_entry["current"] == "disabled"
    assert "bounds sdd --enable" in sdd_entry["command"]


def test_guide_complete_optional_sdd_shows_doctor_when_enabled(tmp_path, monkeypatch):
    """When SDD is enabled, the optional SDD entry points at `bounds sdd --doctor`."""
    from bounds import agentsync
    monkeypatch.chdir(tmp_path)
    _complete_project(tmp_path, agentsync)
    # Enable SDD in root.yaml.
    root = tmp_path / ".bounds" / "root.yaml"
    root.write_text(root.read_text() + "sdd:\n  enabled: true\n", encoding="utf-8")

    payload = guide.run_guide(tmp_path)

    sdd_entry = next(f for f in payload["optional"] if f["id"] == "sdd")
    assert sdd_entry["enabled"] is True
    assert "bounds sdd --doctor" in sdd_entry["command"]


def test_guide_incomplete_does_not_include_optional(tmp_path):
    """Optional features are only shown when setup is complete — not before."""
    payload = guide.run_guide(tmp_path)  # no .bounds/ → not complete
    assert payload["complete"] is False
    assert "optional" not in payload


def test_guide_human_output_shows_optional_section(tmp_path, monkeypatch):
    """The human view shows the 'optional features' block when setup is complete."""
    from bounds import agentsync
    from click.testing import CliRunner
    monkeypatch.chdir(tmp_path)
    _complete_project(tmp_path, agentsync)

    result = CliRunner().invoke(main, ["guide", "--human"])

    assert result.exit_code == 0
    assert "optional features:" in result.output
    assert "Spec-Driven Development" in result.output
    assert "Agent invocation level" in result.output


def test_sdd_status_payload_includes_configure_hints():
    """The bounds sdd status payload carries a 'configure' key with the CLI commands for all write ops."""
    from bounds import sdd as sdd_mod
    payload = sdd_mod._status(None)
    assert "configure" in payload
    cfg = payload["configure"]
    assert "bounds sdd --enable" in cfg.get("enable", "")
    assert "bounds sdd --disable" in cfg.get("disable", "")


def test_guide_sdd_null_phases_defaults_to_all_phases(tmp_path):
    """A blank YAML phases value normalizes to the default phase list instead of crashing."""
    cfg = tmp_path / ".bounds"
    cfg.mkdir()
    (cfg / "root.yaml").write_text(
        'version: "1"\nproject: sdd\nsdd:\n  enabled: true\n  phases:\n',
        encoding="utf-8",
    )

    payload = guide.run_guide(tmp_path)
    assert payload["sdd"]["phases"] == [
        "specify",
        "clarify",
        "plan",
        "tasks",
        "analyze",
        "implement",
        "verify",
    ]


def test_guide_surfaces_manifest_error_with_cli_recovery(tmp_path):
    """Broken YAML is reported explicitly instead of being disguised as zero completed steps."""
    cfg = tmp_path / ".bounds"
    cfg.mkdir()
    (cfg / "root.yaml").write_text("project: [broken\n", encoding="utf-8")

    payload = guide.run_guide(tmp_path)

    assert payload["manifest_error"]["message"]
    assert payload["manifest_error"]["fix"] == "run `bounds validate -H` for the structured manifest error"
    assert payload["next"] == "bounds validate -H"


def test_guide_human_view_does_not_hide_manifest_error(tmp_path, monkeypatch):
    """Human rendering leads with the parse failure and the structured recovery command."""
    cfg = tmp_path / ".bounds"
    cfg.mkdir()
    (cfg / "root.yaml").write_text("project: [broken\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["guide", "--human"])

    assert result.exit_code == 0
    assert "manifest error:" in result.output
    assert "bounds validate -H" in result.output
