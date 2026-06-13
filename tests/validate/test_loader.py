"""Tests for manifest loading and backward-compat with the legacy ``.compact/`` layout."""

from __future__ import annotations

from pathlib import Path

import pytest

from bounds.shared import config, errors
from bounds.core.manifest import loader
from bounds.core.validate import engine


# ===========================================================================
# Manifest loading
# ===========================================================================
def test_find_root_walks_up(sample_project):
    """find_root must locate .bounds/ by walking UP from a nested CWD, never auto-loading it from elsewhere."""
    assert loader.find_root(sample_project / "src" / "auth") == sample_project


def test_find_root_none_when_absent(tmp_path):
    """No .bounds/ anywhere up-tree returns None (not a raise) so callers can fall back / prompt init."""
    assert loader.find_root(tmp_path) is None


def test_load_all_and_consumed_by(sample_project):
    """consumed_by is the loader-derived inverse of consumes; if it weren't auto-filled, impact/propagation would see no reverse edges."""
    rootm, subs, issues = loader.load_all(sample_project)
    assert rootm.project == "shop"
    assert set(subs) == {"database", "auth", "api"}
    # consumed_by is the inverse of consumes, auto-filled by the loader
    assert "auth" in subs["database"].consumed_by
    assert "api" in subs["auth"].consumed_by
    assert subs["api"].consumed_by == []


def test_load_root_missing_raises(tmp_path):
    """A missing root manifest is genuinely fatal (not fail-soft): it must raise BoundsError with the stable E_MANIFEST_NOT_FOUND code."""
    with pytest.raises(errors.BoundsError) as exc:
        loader.load_root(tmp_path)
    assert exc.value.code == errors.E_MANIFEST_NOT_FOUND


def test_load_subsystem_unknown_raises(sample_project):
    """Requesting an undeclared subsystem is fatal with the stable E_SUBSYSTEM_NOT_FOUND code, not a silent empty result."""
    with pytest.raises(errors.BoundsError) as exc:
        loader.load_subsystem(sample_project, "ghost")
    assert exc.value.code == errors.E_SUBSYSTEM_NOT_FOUND


# ===========================================================================
# Backward compatibility — legacy `.compact/` layout
# ===========================================================================
def _write_legacy_project(root: Path) -> None:
    """Scaffold a minimal one-subsystem project under the legacy ``.compact/`` dir."""
    (root / ".compact" / "manifests").mkdir(parents=True)
    (root / ".compact" / "root.yaml").write_text(
        'version: "1"\nproject: legacy\nlanguages: [python]\nenforce: "off"\n'
        "subsystems: [models]\n",
        encoding="utf-8",
    )
    (root / ".compact" / "manifests" / "models.yaml").write_text(
        "name: models\nrole: library\ncriticality: core\npaths: [src/models]\n"
        "exposes:\n  - { name: Thing, kind: class }\nconsumes: []\n",
        encoding="utf-8",
    )
    (root / "src" / "models").mkdir(parents=True)
    (root / "src" / "models" / "thing.py").write_text("class Thing:\n    pass\n", encoding="utf-8")


def test_legacy_compact_dir_is_discovered(tmp_path):
    """A project carrying only the pre-rename .compact/ layout is still found and resolved as the config dir."""
    proj = tmp_path / "legacy"
    proj.mkdir()
    _write_legacy_project(proj)
    assert loader.find_root(proj / "src" / "models") == proj
    assert config.uses_legacy_layout(proj) is True
    assert config.config_dir(proj).name == config.LEGACY_DIR


def test_legacy_compact_dir_warns_once(tmp_path, capsys, monkeypatch):
    """Resolving a legacy layout prints exactly one deprecation notice to stderr (one-shot guard), never repeated per resolution."""
    proj = tmp_path / "legacy"
    proj.mkdir()
    _write_legacy_project(proj)
    # Reset the one-shot guard via monkeypatch so it auto-restores and this test stays
    # order-independent regardless of other legacy-layout tests in the session.
    monkeypatch.setattr(config, "_legacy_warning_emitted", False)
    config.config_dir(proj)
    config.config_dir(proj)  # repeated resolution must not warn again
    err = capsys.readouterr().err
    assert err.count("deprecated") == 1
    assert config.LEGACY_DIR in err and config.BOUNDS_DIR in err


def test_legacy_compact_dir_validates(tmp_path):
    """The validation engine runs end-to-end against a legacy .compact/ project, so the rename never broke existing repos."""
    proj = tmp_path / "legacy"
    proj.mkdir()
    _write_legacy_project(proj)
    report = engine.run(proj, mode="full")
    assert report.status == "fresh"
    assert report.errors() == []

# ===========================================================================
# Parent validation
# ===========================================================================
def test_validate_parents_cycle():
    """Circular parent chains must be reported as errors and cleared from the models."""
    subs = {
        "A": loader.SubsystemCompact(name="A", parent="B"),
        "B": loader.SubsystemCompact(name="B", parent="A"),
    }
    issues = loader._validate_parents(subs)
    assert any(i.code == errors.E_SCHEMA_INVALID and "circular parent chain" in i.message for i in issues)
    assert any(i.severity == "error" for i in issues)
    assert subs["A"].parent == ""
    assert subs["B"].parent == ""


def test_validate_parents_self():
    """A subsystem declaring itself as its own parent is an error and is cleared."""
    subs = {"A": loader.SubsystemCompact(name="A", parent="A")}
    issues = loader._validate_parents(subs)
    assert any(i.code == errors.E_SCHEMA_INVALID and "own parent" in i.message for i in issues)
    assert any(i.severity == "error" for i in issues)
    assert subs["A"].parent == ""


def test_validate_parents_unknown():
    """A parent that doesn't exist is an error and is cleared."""
    subs = {"A": loader.SubsystemCompact(name="A", parent="ghost")}
    issues = loader._validate_parents(subs)
    assert any(i.code == errors.E_SCHEMA_INVALID and "not a known subsystem" in i.message for i in issues)
    assert any(i.severity == "error" for i in issues)
    assert subs["A"].parent == ""
