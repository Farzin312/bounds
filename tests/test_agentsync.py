"""Tests for the cross-agent protocol (s-18): bounds.agentsync.

AGENTS.md is the single canonical contract (a marked block, always written on sync); every
per-tool file is a short pointer to it. Covers the three modes (sync / detect / check),
marked-block insertion + idempotent re-sync, custom-edit safety (no clobber of hand-written
shared files), and `only` filtering.
"""

from __future__ import annotations

import pytest

from bounds import agentsync, errors
from bounds.agentsync import _looks_bounds_authored


def _mk_root(tmp_path):
    """Create a project root with a .bounds/ dir (the synced-project precondition)."""
    (tmp_path / ".bounds").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------
def test_sync_writes_canonical_agents_md_and_pointer_files(tmp_path):
    root = _mk_root(tmp_path)
    report = agentsync.run_agent(root, mode="sync")

    # Canonical contract lives in AGENTS.md as a marked block.
    agents_md = root / "AGENTS.md"
    assert agents_md.exists()
    assert report["canonical"] == "AGENTS.md"
    body = agents_md.read_text(encoding="utf-8")
    assert "<!-- BOUNDS:START -->" in body and "<!-- BOUNDS:END -->" in body
    assert "architecture contract for agents" in body
    assert "bounds validate --quick" in body

    # No bespoke BOUNDS.md anymore.
    assert not (root / "BOUNDS.md").exists()

    # Dedicated, bounds-only pointer files for claude / cursor / windsurf.
    for f in (
        root / ".claude/commands/bounds.md",
        root / ".cursor/rules/bounds.mdc",
        root / ".windsurf/rules/bounds.md",
    ):
        assert f.exists(), f
        text = f.read_text(encoding="utf-8")
        assert "bounds list" in text
        assert "AGENTS.md" in text  # points at the canonical

    # Each tool needs activating front-matter or the rule sits dormant.
    assert "description:" in (root / ".claude/commands/bounds.md").read_text("utf-8")
    assert "alwaysApply: true" in (root / ".cursor/rules/bounds.mdc").read_text("utf-8")
    assert "trigger: always_on" in (root / ".windsurf/rules/bounds.md").read_text("utf-8")

    created = set(report["created"])
    assert {"AGENTS.md", ".claude/commands/bounds.md", ".cursor/rules/bounds.mdc",
            ".windsurf/rules/bounds.md", "GEMINI.md", ".github/copilot-instructions.md",
            ".aider.conf.yml"} <= created
    assert report["skipped_custom"] == []


def test_sync_paths_are_sorted_posix(tmp_path):
    root = _mk_root(tmp_path)
    report = agentsync.run_agent(root, mode="sync")
    assert report["created"] == sorted(report["created"])
    assert all("\\" not in p for p in report["created"])


def test_sync_inserts_marked_block_into_existing_agents_md(tmp_path):
    root = _mk_root(tmp_path)
    prior = "# My Project\n\nSome existing developer notes here.\n"
    (root / "AGENTS.md").write_text(prior, encoding="utf-8")

    report = agentsync.run_agent(root, mode="sync", only={"codex"})

    text = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "Some existing developer notes here." in text  # prior content preserved
    assert "<!-- BOUNDS:START -->" in text and "<!-- BOUNDS:END -->" in text
    assert "bounds validate --quick" in text
    assert "AGENTS.md" in report["updated"]


def test_resync_is_idempotent_and_only_touches_block(tmp_path):
    root = _mk_root(tmp_path)
    (root / "AGENTS.md").write_text("# My Project\n\nKeep me.\n", encoding="utf-8")

    agentsync.run_agent(root, mode="sync", only={"codex"})
    after_first = (root / "AGENTS.md").read_text(encoding="utf-8")

    edited = after_first.replace("Keep me.", "Keep me AND this edit.")
    (root / "AGENTS.md").write_text(edited, encoding="utf-8")

    report2 = agentsync.run_agent(root, mode="sync", only={"codex"})
    after_second = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "Keep me AND this edit." in after_second
    assert after_second.count("<!-- BOUNDS:START -->") == 1
    assert "AGENTS.md" not in report2["created"]
    assert "AGENTS.md" not in report2["updated"]

    report3 = agentsync.run_agent(root, mode="sync", only={"codex"})
    assert (root / "AGENTS.md").read_text(encoding="utf-8") == after_second
    assert report3["created"] == [] and report3["updated"] == []


def test_resync_refreshes_stale_block(tmp_path):
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync", only={"codex"})
    path = root / "AGENTS.md"
    stale = path.read_text(encoding="utf-8").replace("bounds validate --quick", "OUTDATED")
    path.write_text(stale, encoding="utf-8")

    report = agentsync.run_agent(root, mode="sync", only={"codex"})
    refreshed = path.read_text(encoding="utf-8")
    assert "bounds validate --quick" in refreshed and "OUTDATED" not in refreshed
    assert "AGENTS.md" in report["updated"]


def test_handwritten_agents_md_without_markers_is_skipped(tmp_path):
    root = _mk_root(tmp_path)
    handwritten = "# Agents\n\nUse `bounds list` to see the map. Do not edit blindly.\n"
    (root / "AGENTS.md").write_text(handwritten, encoding="utf-8")

    report = agentsync.run_agent(root, mode="sync", only={"codex"})
    assert (root / "AGENTS.md").read_text(encoding="utf-8") == handwritten  # untouched
    assert "AGENTS.md" in report["skipped_custom"]
    assert "AGENTS.md" not in report["created"] and "AGENTS.md" not in report["updated"]


def test_unrelated_agents_md_is_appended_not_skipped(tmp_path):
    root = _mk_root(tmp_path)
    (root / "AGENTS.md").write_text("# Agents\n\nRun the test suite before pushing.\n", encoding="utf-8")

    report = agentsync.run_agent(root, mode="sync", only={"codex"})
    text = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "Run the test suite before pushing." in text
    assert "<!-- BOUNDS:START -->" in text
    assert "AGENTS.md" in report["updated"] and "AGENTS.md" not in report["skipped_custom"]


def test_only_filters_to_single_agent_but_canonical_always_written(tmp_path):
    root = _mk_root(tmp_path)
    report = agentsync.run_agent(root, mode="sync", only={"claude"})

    # AGENTS.md (canonical) is ALWAYS written; the claude pointer references it.
    assert (root / "AGENTS.md").exists()
    assert (root / ".claude/commands/bounds.md").exists()
    assert not (root / ".cursor/rules/bounds.mdc").exists()
    assert not (root / "GEMINI.md").exists()
    assert set(report["created"]) == {"AGENTS.md", ".claude/commands/bounds.md"}


def test_aider_yaml_block_points_at_agents_md(tmp_path):
    root = _mk_root(tmp_path)
    (root / ".aider.conf.yml").write_text("model: gpt-4\n", encoding="utf-8")
    report = agentsync.run_agent(root, mode="sync", only={"aider"})
    text = (root / ".aider.conf.yml").read_text(encoding="utf-8")
    assert "model: gpt-4" in text
    assert "# BOUNDS:START" in text and "# BOUNDS:END" in text
    assert "read: [AGENTS.md]" in text
    assert ".aider.conf.yml" in report["updated"]


def test_codex_and_opencode_dedupe_to_one_write(tmp_path):
    root = _mk_root(tmp_path)
    report = agentsync.run_agent(root, mode="sync", only={"codex", "opencode"})
    all_paths = report["created"] + report["updated"] + report["skipped_custom"]
    assert all_paths.count("AGENTS.md") == 1


def test_sync_rejects_unknown_agent_key(tmp_path):
    root = _mk_root(tmp_path)
    with pytest.raises(errors.BoundsError) as exc:
        agentsync.run_agent(root, mode="sync", only={"bogus"})
    assert exc.value.code == errors.E_USAGE


def test_unknown_mode_raises_usage(tmp_path):
    root = _mk_root(tmp_path)
    with pytest.raises(errors.BoundsError) as exc:
        agentsync.run_agent(root, mode="nope")
    assert exc.value.code == errors.E_USAGE


# ---------------------------------------------------------------------------
# _looks_bounds_authored heuristic
# ---------------------------------------------------------------------------
def test_looks_bounds_authored_ignores_out_of_bounds_idiom():
    assert _looks_bounds_authored("array index out of bounds in the parser") is False


def test_looks_bounds_authored_ignores_bounds_checking_prose():
    assert _looks_bounds_authored("We do bounds checking on every write.") is False


def test_looks_bounds_authored_matches_inline_command():
    assert _looks_bounds_authored("Run `bounds list` to see the map.") is True


def test_looks_bounds_authored_matches_bare_command_invocation():
    assert _looks_bounds_authored("Use bounds describe auth before editing.") is True


def test_looks_bounds_authored_matches_heading():
    assert _looks_bounds_authored("## Bounds — architecture contract") is True


def test_looks_bounds_authored_matches_inline_code_name():
    assert _looks_bounds_authored("`bounds`") is True


# ---------------------------------------------------------------------------
# detect
# ---------------------------------------------------------------------------
def test_detect_empty_root(tmp_path):
    root = _mk_root(tmp_path)
    assert agentsync.run_agent(root, mode="detect") == {"detected": []}


def test_detect_finds_footprints(tmp_path):
    root = _mk_root(tmp_path)
    (root / ".claude").mkdir()
    (root / ".cursor").mkdir()
    (root / "GEMINI.md").write_text("x\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("x\n", encoding="utf-8")

    detected = set(agentsync.run_agent(root, mode="detect")["detected"])
    assert {"claude", "cursor", "gemini", "codex", "opencode"} <= detected
    assert "copilot" not in detected


def test_bare_github_dir_does_not_detect_copilot(tmp_path):
    # A .github/ directory (workflows, templates) is universal — not a Copilot signal.
    root = _mk_root(tmp_path)
    (root / ".github" / "workflows").mkdir(parents=True)
    assert "copilot" not in agentsync.run_agent(root, mode="detect")["detected"]
    # Only its actual instruction file counts.
    (root / ".github" / "copilot-instructions.md").write_text("x\n", encoding="utf-8")
    assert "copilot" in agentsync.run_agent(root, mode="detect")["detected"]


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------
def test_check_shape_when_nothing_detected(tmp_path):
    root = _mk_root(tmp_path)
    assert agentsync.run_agent(root, mode="check") == {"ok": True, "missing": [], "configured": []}


def test_check_reports_missing_for_detected_unconfigured_agent(tmp_path):
    root = _mk_root(tmp_path)
    (root / ".claude").mkdir()  # detected, not yet synced
    result = agentsync.run_agent(root, mode="check")
    assert result["ok"] is False
    assert "claude" in result["missing"]


def test_check_reports_configured_after_sync(tmp_path):
    root = _mk_root(tmp_path)
    (root / ".claude").mkdir()
    agentsync.run_agent(root, mode="sync", only={"claude"})
    result = agentsync.run_agent(root, mode="check")
    assert result["ok"] is True
    assert "claude" in result["configured"]


def test_check_shared_file_needs_marker(tmp_path):
    root = _mk_root(tmp_path)
    (root / "AGENTS.md").write_text("# Agents\n\nUnrelated.\n", encoding="utf-8")
    result = agentsync.run_agent(root, mode="check")
    assert result["ok"] is False
    assert "codex" in result["missing"] and "opencode" in result["missing"]

    agentsync.run_agent(root, mode="sync", only={"codex"})
    result2 = agentsync.run_agent(root, mode="check")
    assert result2["ok"] is True
    assert "codex" in result2["configured"] and "opencode" in result2["configured"]
