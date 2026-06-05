"""End-to-end CLI workflows on OSS-shaped throwaway repositories."""

from __future__ import annotations

import json

from click.testing import CliRunner

from bounds.cli import main


def _json(result):
    """Parse a CliRunner JSON response."""
    return json.loads(result.output)


def _write_python_package(root, package: str, imports: str = "") -> None:
    """Create a kept discover candidate: five Python files under one package directory."""
    pkg = root / "packages" / package
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for i in range(4):
        body = imports if i == 0 else ""
        (pkg / f"mod{i}.py").write_text(f"{body}\ndef {package}_fn_{i}():\n    return {i}\n", encoding="utf-8")


def test_open_source_repo_full_setup_guidance_and_coverage_repair(tmp_path, monkeypatch):
    """A realistic repo can go from no .bounds to discover, guided coverage repair, and clean validate through CLI commands only."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    _write_python_package(tmp_path, "core")
    _write_python_package(tmp_path, "api", "from packages.core.mod0 import core_fn_0\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "smoke.py").write_text("def smoke():\n    return True\n", encoding="utf-8")

    before = runner.invoke(main, ["list"])
    assert before.exit_code == 2
    assert _json(before)["error"]["code"] == "E_MANIFEST_NOT_FOUND"

    assert runner.invoke(main, ["init", "--root"]).exit_code == 0
    discovered = runner.invoke(main, ["discover", "--apply"])
    assert discovered.exit_code == 0
    assert _json(discovered)["candidates"]

    first_validate = runner.invoke(main, ["validate"])
    assert first_validate.exit_code == 0
    first = _json(first_validate)
    assert any(i["code"] == "E_COVERAGE_GAP" for i in first["issues"])
    assert any("bounds init --subsystem <name> --path" in step for step in first["next_steps"])

    guide = runner.invoke(main, ["guide", "--human"])
    assert guide.exit_code == 0
    assert "calibration does not map files" in guide.output

    calibrate = runner.invoke(main, ["calibrate", "--human"])
    assert calibrate.exit_code == 0
    assert "does not add unmapped source files" in calibrate.output

    repaired = runner.invoke(main, ["init", "--subsystem", "scripts", "--path", "scripts/smoke.py"])
    assert repaired.exit_code == 0
    assert _json(repaired)["registered"] == "scripts"

    final_validate = runner.invoke(main, ["validate"])
    assert final_validate.exit_code == 0
    final = _json(final_validate)
    assert final["stats"]["coverage"]["mapping"]["mapped_pct"] == 100.0
    assert final["issues"] == []

    listing = runner.invoke(main, ["list"])
    assert listing.exit_code == 0
    names = {s["name"] for s in _json(listing)["subsystems"]}
    assert "scripts" in names
    described = runner.invoke(main, ["describe", "scripts"])
    assert described.exit_code == 0
    assert _json(described)["paths"] == ["scripts/smoke.py"]
