"""Regression coverage for public contracts preserved by the package refactor."""

from __future__ import annotations

import importlib

from click.testing import CliRunner

from bounds.cli import main


def test_legacy_module_paths_remain_importable():
    """Documented pre-refactor module paths resolve to their canonical implementations."""
    names = [
        "bounds.agentsync",
        "bounds.agenthook",
        "bounds.guide",
        "bounds.validate.engine",
        "bounds.extract.scan",
        "bounds.manifest.loader",
        "bounds.coverage",
        "bounds.config",
        "bounds.update_check",
        "bounds.upgrade",
        "bounds._io",
    ]

    assert all(importlib.import_module(name) is not None for name in names)


def test_cli_keeps_legacy_validate_and_upgrade_options():
    """A package move must not silently remove supported command-line flags."""
    validate = main.commands["validate"]
    upgrade = main.commands["upgrade"]
    validate_options = {opt for param in validate.params for opt in getattr(param, "opts", ())}
    upgrade_options = {opt for param in upgrade.params for opt in getattr(param, "opts", ())}

    assert {"--quick", "--mode", "--enforce", "--base"} <= validate_options
    assert {"--ref", "--local", "--dry-run"} <= upgrade_options
    assert "--check" not in upgrade_options
    assert "--force" not in upgrade_options


def test_read_command_remains_json_by_default(tmp_path, monkeypatch):
    """Read commands stay JSON-first even when stdout reports itself as a TTY."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["list"])

    assert result.exit_code == 2
    assert result.output.lstrip().startswith("{")


def test_hidden_agent_hook_stays_out_of_top_level_help():
    """Internal hook plumbing must not appear as a user-facing command group."""
    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0
    assert "agent-hook" not in result.output
