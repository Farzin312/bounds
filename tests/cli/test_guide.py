"""Tests for `bounds guide` — the state-aware setup checklist."""

from __future__ import annotations

import json

from click.testing import CliRunner

from bounds import guide
from bounds.cli import main

_ROOT_YAML = ('version: "1"\nproject: x\nlanguages: [python]\n'
              'enforce: "on"\nsubsystems: []\n')


def test_guide_fresh_project_all_todo(tmp_path):
    payload = guide.run_guide(tmp_path)
    assert payload["mode"] == "guide"
    assert payload["complete"] is False
    assert [s["id"] for s in payload["steps"]] == ["init", "discover", "agents", "ci"]
    assert all(not s["done"] for s in payload["steps"])
    assert payload["next"] == "bounds init --root"  # first undone step
    assert payload["daily"]  # daily commands always present


def test_guide_after_init_points_to_discover(tmp_path):
    (tmp_path / ".bounds").mkdir()
    (tmp_path / ".bounds" / "root.yaml").write_text(_ROOT_YAML, encoding="utf-8")
    (tmp_path / ".bounds" / "manifests").mkdir()
    payload = guide.run_guide(tmp_path)
    done = {s["id"]: s["done"] for s in payload["steps"]}
    assert done["init"] is True
    assert done["discover"] is False  # no subsystems mapped yet
    assert payload["next"] == "bounds discover --apply"


def test_guide_detects_ci_gate(tmp_path):
    (tmp_path / ".bounds").mkdir()
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "bounds.yml").write_text("name: bounds\n", encoding="utf-8")
    ci_step = next(s for s in guide.run_guide(tmp_path)["steps"] if s["id"] == "ci")
    assert ci_step["done"] is True


def test_guide_detects_ci_in_precommit(tmp_path):
    (tmp_path / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n      - id: bounds-preflight\n", encoding="utf-8")
    ci_step = next(s for s in guide.run_guide(tmp_path)["steps"] if s["id"] == "ci")
    assert ci_step["done"] is True


def test_guide_cli_is_json_when_piped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(main, ["guide"])  # CliRunner is non-TTY → JSON
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["mode"] == "guide" and "steps" in data and "daily" in data
