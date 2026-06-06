"""Regression tests for the coverage/fix-coverage command services."""

from __future__ import annotations

import json
import subprocess

from click.testing import CliRunner

from bounds import coverage
from bounds.cli import main
from bounds.models import SubsystemCompact


def _project(root):
    (root / ".bounds" / "manifests").mkdir(parents=True)
    (root / ".bounds" / "root.yaml").write_text(
        'version: "1"\nproject: coverage\nlanguages: [typescript]\nsubsystems: [app]\n',
        encoding="utf-8",
    )
    (root / ".bounds" / "manifests" / "app.yaml").write_text(
        "name: app\nrole: library\ncriticality: leaf\npaths: [app]\nexposes: []\n",
        encoding="utf-8",
    )
    (root / "app").mkdir()
    (root / "app" / "main.ts").write_text("export const main = true\n", encoding="utf-8")
    (root / "vite.config.ts").write_text("export default {}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return {"app": SubsystemCompact(name="app", paths=["app"])}


def test_coverage_why_uses_the_same_classifier_as_the_report(tmp_path, monkeypatch):
    """The per-file explanation and aggregate bucket agree on known tool configuration."""
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["coverage", "--why", "vite.config.ts"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "algorithm_miss"
    assert "--auto" in payload["command"]


def test_fix_coverage_previews_then_writes_exact_root_relative_paths(tmp_path):
    """Auto-fix never writes basename-wide patterns and never mutates without explicit --apply."""
    subs = _project(tmp_path)

    preview = coverage.run_fix_coverage(tmp_path, subs, auto=True, apply=False)
    assert preview["proposed"] == ["vite.config.ts"]
    assert not (tmp_path / ".boundsignore").exists()

    applied = coverage.run_fix_coverage(tmp_path, subs, auto=True, apply=True)
    assert applied["applied"] is True
    assert (tmp_path / ".boundsignore").read_text(encoding="utf-8").splitlines()[-1] == "vite.config.ts"
    assert coverage.run_coverage(tmp_path, subs)["supported"]["unowned"] == 0


def test_fix_coverage_apply_requires_auto(tmp_path, monkeypatch):
    """The mutating flag cannot be used without the explicit auto-fix mode."""
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["fix-coverage", "--apply"])

    assert result.exit_code == 2
    assert "E_USAGE" in result.output


def test_coverage_human_output_leads_with_decisions_not_nested_json(tmp_path, monkeypatch):
    """The human view summarizes the three actionable buckets and the next command."""
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["coverage", "--summary", "--human"])

    assert result.exit_code == 0
    assert "needs ownership decision:" in result.output
    assert "deterministic algorithm miss: 1" in result.output
    assert "next:" in result.output


def test_top_level_help_groups_new_commands_by_job():
    """Coverage repair and SDD stay discoverable in the purpose-ordered command help."""
    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0
    assert "Set up:" in result.output and "sdd" in result.output
    assert "Catch drift:" in result.output
    assert "coverage" in result.output and "fix-coverage" in result.output
