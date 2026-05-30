"""Tests for the cross-agent protocol (s-18): bounds.agentsync.

Covers the three modes (sync / detect / check), marked-block insertion and idempotent
re-sync, custom-edit safety (no clobber of hand-written shared files), and `only` filtering.
"""

from __future__ import annotations

import pytest

from bounds import agentsync, errors


def _mk_root(tmp_path):
    """Create a project root with a .bounds/ dir (the synced-project precondition)."""
    (tmp_path / ".bounds").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------
def test_sync_creates_bounds_md_and_dedicated_files(tmp_path):
    root = _mk_root(tmp_path)
    report = agentsync.run_agent(root, mode="sync")

    # Canonical contract file.
    bounds_md = root / "BOUNDS.md"
    assert bounds_md.exists()
    assert report["bounds_md"] == "BOUNDS.md"
    body = bounds_md.read_text(encoding="utf-8")
    assert "# Bounds Agent Contract" in body
    assert "bounds validate --quick" in body

    # Dedicated, bounds-only files for claude / cursor / windsurf.
    claude = root / ".claude/commands/bounds.md"
    cursor = root / ".cursor/rules/bounds.mdc"
    windsurf = root / ".windsurf/rules/bounds.md"
    for f in (claude, cursor, windsurf):
        assert f.exists(), f
        assert "bounds list" in f.read_text(encoding="utf-8")

    created = set(report["created"])
    assert "BOUNDS.md" in created
    assert ".claude/commands/bounds.md" in created
    assert ".cursor/rules/bounds.mdc" in created
    assert ".windsurf/rules/bounds.md" in created
    # codex + opencode share AGENTS.md -> a single write.
    assert "AGENTS.md" in created
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
    # Prior content preserved.
    assert "Some existing developer notes here." in text
    # Our marked block added.
    assert "<!-- BOUNDS:START -->" in text
    assert "<!-- BOUNDS:END -->" in text
    assert "bounds validate --quick" in text
    assert "AGENTS.md" in report["updated"]


def test_resync_is_idempotent_and_only_touches_block(tmp_path):
    root = _mk_root(tmp_path)
    prior = "# My Project\n\nKeep me.\n"
    (root / "AGENTS.md").write_text(prior, encoding="utf-8")

    agentsync.run_agent(root, mode="sync", only={"codex"})
    after_first = (root / "AGENTS.md").read_text(encoding="utf-8")

    # A human edits text *outside* the block; re-sync must preserve it.
    edited = after_first.replace("Keep me.", "Keep me AND this edit.")
    (root / "AGENTS.md").write_text(edited, encoding="utf-8")

    report2 = agentsync.run_agent(root, mode="sync", only={"codex"})
    after_second = (root / "AGENTS.md").read_text(encoding="utf-8")

    assert "Keep me AND this edit." in after_second
    assert after_second.count("<!-- BOUNDS:START -->") == 1
    # Block content unchanged -> reported as neither created nor updated.
    assert "AGENTS.md" not in report2["created"]
    assert "AGENTS.md" not in report2["updated"]

    # Running sync twice with no edits is a no-op on disk.
    report3 = agentsync.run_agent(root, mode="sync", only={"codex"})
    assert (root / "AGENTS.md").read_text(encoding="utf-8") == after_second
    assert report3["created"] == [] and report3["updated"] == []


def test_resync_refreshes_stale_block(tmp_path):
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync", only={"codex"})
    path = root / "AGENTS.md"
    text = path.read_text(encoding="utf-8")
    # Corrupt the block interior; re-sync should rewrite it.
    stale = text.replace("bounds validate --quick", "OUTDATED")
    path.write_text(stale, encoding="utf-8")

    report = agentsync.run_agent(root, mode="sync", only={"codex"})
    refreshed = path.read_text(encoding="utf-8")
    assert "bounds validate --quick" in refreshed
    assert "OUTDATED" not in refreshed
    assert "AGENTS.md" in report["updated"]


def test_handwritten_agents_md_without_markers_is_skipped(tmp_path):
    root = _mk_root(tmp_path)
    # A human hand-wrote bounds guidance, no markers.
    handwritten = "# Agents\n\nUse `bounds list` to see the map. Do not edit blindly.\n"
    (root / "AGENTS.md").write_text(handwritten, encoding="utf-8")

    report = agentsync.run_agent(root, mode="sync", only={"codex"})

    # Untouched.
    assert (root / "AGENTS.md").read_text(encoding="utf-8") == handwritten
    assert "AGENTS.md" in report["skipped_custom"]
    assert "AGENTS.md" not in report["created"]
    assert "AGENTS.md" not in report["updated"]


def test_unrelated_agents_md_is_appended_not_skipped(tmp_path):
    root = _mk_root(tmp_path)
    unrelated = "# Agents\n\nRun the test suite before pushing.\n"
    (root / "AGENTS.md").write_text(unrelated, encoding="utf-8")

    report = agentsync.run_agent(root, mode="sync", only={"codex"})
    text = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "Run the test suite before pushing." in text
    assert "<!-- BOUNDS:START -->" in text
    assert "AGENTS.md" in report["updated"]
    assert "AGENTS.md" not in report["skipped_custom"]


def test_only_filters_to_single_agent(tmp_path):
    root = _mk_root(tmp_path)
    report = agentsync.run_agent(root, mode="sync", only={"claude"})

    assert (root / ".claude/commands/bounds.md").exists()
    assert not (root / ".cursor/rules/bounds.mdc").exists()
    assert not (root / "AGENTS.md").exists()
    assert not (root / "GEMINI.md").exists()
    created = set(report["created"])
    assert ".claude/commands/bounds.md" in created
    assert created == {"BOUNDS.md", ".claude/commands/bounds.md"}


def test_aider_yaml_block_uses_yaml_markers(tmp_path):
    root = _mk_root(tmp_path)
    (root / ".aider.conf.yml").write_text("model: gpt-4\n", encoding="utf-8")
    report = agentsync.run_agent(root, mode="sync", only={"aider"})
    text = (root / ".aider.conf.yml").read_text(encoding="utf-8")
    assert "model: gpt-4" in text
    assert "# BOUNDS:START" in text
    assert "# BOUNDS:END" in text
    assert "read: [BOUNDS.md]" in text
    assert ".aider.conf.yml" in report["updated"]


def test_codex_and_opencode_dedupe_to_one_write(tmp_path):
    root = _mk_root(tmp_path)
    report = agentsync.run_agent(root, mode="sync", only={"codex", "opencode"})
    # AGENTS.md appears exactly once across all path lists.
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
# detect
# ---------------------------------------------------------------------------
def test_detect_empty_root(tmp_path):
    root = _mk_root(tmp_path)
    result = agentsync.run_agent(root, mode="detect")
    assert result == {"detected": []}


def test_detect_finds_footprints(tmp_path):
    root = _mk_root(tmp_path)
    (root / ".claude").mkdir()
    (root / ".cursor").mkdir()
    (root / "GEMINI.md").write_text("x\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("x\n", encoding="utf-8")

    result = agentsync.run_agent(root, mode="detect")
    detected = set(result["detected"])
    assert "claude" in detected
    assert "cursor" in detected
    assert "gemini" in detected
    # AGENTS.md implies both codex and opencode.
    assert "codex" in detected
    assert "opencode" in detected
    assert "copilot" not in detected
    assert result["detected"] == sorted(result["detected"])


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------
def test_check_shape_when_nothing_detected(tmp_path):
    root = _mk_root(tmp_path)
    result = agentsync.run_agent(root, mode="check")
    assert result == {"ok": True, "missing": [], "configured": []}


def test_check_reports_missing_for_detected_unconfigured_agent(tmp_path):
    root = _mk_root(tmp_path)
    (root / ".claude").mkdir()  # detected, but not yet synced.
    result = agentsync.run_agent(root, mode="check")
    assert result["ok"] is False
    assert "claude" in result["missing"]
    assert result["configured"] == []


def test_check_reports_configured_after_sync(tmp_path):
    root = _mk_root(tmp_path)
    (root / ".claude").mkdir()
    agentsync.run_agent(root, mode="sync", only={"claude"})
    result = agentsync.run_agent(root, mode="check")
    assert result["ok"] is True
    assert "claude" in result["configured"]
    assert result["missing"] == []


def test_check_shared_file_needs_marker(tmp_path):
    root = _mk_root(tmp_path)
    # AGENTS.md present (detected codex+opencode) but no bounds marker.
    (root / "AGENTS.md").write_text("# Agents\n\nUnrelated.\n", encoding="utf-8")
    result = agentsync.run_agent(root, mode="check")
    assert result["ok"] is False
    assert "codex" in result["missing"]
    assert "opencode" in result["missing"]

    # After sync, the marker is present -> configured.
    agentsync.run_agent(root, mode="sync", only={"codex"})
    result2 = agentsync.run_agent(root, mode="check")
    assert result2["ok"] is True
    assert "codex" in result2["configured"]
    assert "opencode" in result2["configured"]
