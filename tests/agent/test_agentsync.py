"""Tests for the cross-agent protocol: bounds.agentsync.

AGENTS.md is the single canonical contract (a marked block, always written on sync); every
per-tool file is a short pointer to it. Covers the three modes (sync / detect / check),
marked-block insertion + idempotent re-sync, custom-edit safety (no clobber of hand-written
shared files), and `only` filtering.
"""

from __future__ import annotations

import json
import re

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


def test_every_generated_block_is_marked_and_stamped(tmp_path):
    """Every synced file carries BOUNDS markers + a version/hash stamp (not the literal version
    — we assert structure, since the version string is volatile)."""
    root = _mk_root(tmp_path)
    (root / ".aider.conf.yml").write_text("model: gpt-4\n", encoding="utf-8")
    agentsync.run_agent(root, mode="sync")

    md_files = [
        root / "AGENTS.md",
        root / ".claude/commands/bounds.md",
        root / ".cursor/rules/bounds.mdc",
        root / ".windsurf/rules/bounds.md",
        root / "GEMINI.md",
        root / ".github/copilot-instructions.md",
    ]
    for f in md_files:
        text = f.read_text(encoding="utf-8")
        assert "<!-- BOUNDS:START -->" in text and "<!-- BOUNDS:END -->" in text, f
        assert re.search(r"<!-- BOUNDS:GENERATED v=\S+ h=[0-9a-f]{8} -->", text), f

    yaml_text = (root / ".aider.conf.yml").read_text(encoding="utf-8")
    assert "# BOUNDS:START" in yaml_text and "# BOUNDS:END" in yaml_text
    assert re.search(r"# BOUNDS:GENERATED v=\S+ h=[0-9a-f]{8}", yaml_text)


def test_claude_command_forwards_arguments(tmp_path):
    """The Claude slash command must forward $ARGUMENTS so `/bounds describe auth` runs
    `bounds describe auth`, with a bare-invocation default and a usage list. Front-matter
    (the activating `description:`) stays OUTSIDE the markers, at the top of the file."""
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync", only={"claude"})
    text = (root / ".claude/commands/bounds.md").read_text(encoding="utf-8")

    assert "bounds $ARGUMENTS" in text  # forwards verbatim
    assert "bounds overview -H" in text  # empty-args default
    assert "/bounds describe <name>" in text  # usage list present
    # Space-separated subcommands are pinned: never hyphenate a bounds command.
    assert "bounds-describe" not in text and "bounds-validate" not in text

    # Front-matter precedes the managed block (so Claude registers the command).
    fm_idx = text.index("description:")
    start_idx = text.index("<!-- BOUNDS:START -->")
    assert fm_idx < start_idx


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


def test_resync_refreshes_legacy_unstamped_block(tmp_path):
    """A block we wrote *before* stamping existed (no BOUNDS:GENERATED line) is still ours, so
    a re-sync refreshes it in place rather than treating it as a hand-edit."""
    root = _mk_root(tmp_path)
    legacy = (
        "<!-- BOUNDS:START -->\n"
        "## Bounds\n\nOld pre-stamp contract body.\n"
        "<!-- BOUNDS:END -->\n"
    )
    path = root / "AGENTS.md"
    path.write_text(legacy, encoding="utf-8")

    report = agentsync.run_agent(root, mode="sync", only={"codex"})
    refreshed = path.read_text(encoding="utf-8")
    assert "bounds validate --quick" in refreshed and "Old pre-stamp contract body." not in refreshed
    assert "BOUNDS:GENERATED" in refreshed  # now carries a stamp
    assert "AGENTS.md" in report["updated"]
    assert "AGENTS.md" not in report["skipped_custom"]


def test_resync_preserves_handedited_in_marker_block(tmp_path):
    """A human edit *inside* the markers (hash no longer matches the stamp) is never clobbered;
    the file is reported skipped_custom and left untouched."""
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync", only={"codex"})
    path = root / "AGENTS.md"
    edited = path.read_text(encoding="utf-8").replace("bounds validate --quick", "MY HAND EDIT")
    path.write_text(edited, encoding="utf-8")

    report = agentsync.run_agent(root, mode="sync", only={"codex"})
    after = path.read_text(encoding="utf-8")
    assert after == edited  # untouched
    assert "MY HAND EDIT" in after
    assert "AGENTS.md" in report["skipped_custom"]
    assert "AGENTS.md" not in report["updated"]


def test_dedicated_file_preserves_out_of_marker_edits(tmp_path):
    """A dedicated file is now marker-managed: a human's additions OUTSIDE the markers survive a
    re-sync, and the block INSIDE is refreshed (here, with the same body → unchanged)."""
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync", only={"claude"})
    path = root / ".claude/commands/bounds.md"

    text = path.read_text(encoding="utf-8")
    appended = text + "\n## My team notes\n\nRemember to run lint.\n"
    path.write_text(appended, encoding="utf-8")

    agentsync.run_agent(root, mode="sync", only={"claude"})
    after = path.read_text(encoding="utf-8")
    assert "## My team notes" in after  # out-of-marker addition preserved
    assert "Remember to run lint." in after
    assert "bounds $ARGUMENTS" in after  # our block still intact


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


def test_sync_reports_unchanged_on_idempotent_resync(tmp_path):
    """A no-op re-sync surfaces already-current files in `unchanged` (so the CLI can say
    'everything already current') and writes nothing to created/updated."""
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync", only={"claude"})
    report = agentsync.run_agent(root, mode="sync", only={"claude"})
    assert report["created"] == [] and report["updated"] == []
    assert "AGENTS.md" in report["unchanged"]
    assert ".claude/commands/bounds.md" in report["unchanged"]


def test_skip_reasons_distinguish_authored_from_hand_edited(tmp_path):
    """skip_reasons explains *why* a file was left alone: a human-authored file with no markers
    is 'authored'; our managed block whose body a human changed is 'hand-edited'."""
    # authored: a hand-written AGENTS.md that mentions bounds but has no markers.
    root = _mk_root(tmp_path)
    (root / "AGENTS.md").write_text(
        "# Agents\n\nUse `bounds list` to see the map.\n", encoding="utf-8")
    report = agentsync.run_agent(root, mode="sync", only={"codex"})
    assert "AGENTS.md" in report["skipped_custom"]
    assert report["skip_reasons"]["AGENTS.md"] == "authored"

    # hand-edited: sync a clean file, then change the body inside the markers.
    second = tmp_path / "second"
    second.mkdir()
    root2 = _mk_root(second)
    agentsync.run_agent(root2, mode="sync", only={"codex"})
    path = root2 / "AGENTS.md"
    path.write_text(
        path.read_text("utf-8").replace("bounds validate --quick", "MY EDIT"), encoding="utf-8")
    report2 = agentsync.run_agent(root2, mode="sync", only={"codex"})
    assert "AGENTS.md" in report2["skipped_custom"]
    assert report2["skip_reasons"]["AGENTS.md"] == "hand-edited"


def test_only_filters_to_single_agent_but_canonical_always_written(tmp_path):
    root = _mk_root(tmp_path)
    report = agentsync.run_agent(root, mode="sync", only={"claude"})

    # AGENTS.md (canonical) is ALWAYS written; the claude pointer + skill reference it.
    assert (root / "AGENTS.md").exists()
    assert (root / ".claude/commands/bounds.md").exists()
    assert (root / ".claude/skills/bounds/SKILL.md").exists()  # auto-trigger skill
    assert not (root / ".cursor/rules/bounds.mdc").exists()
    assert not (root / ".gemini/commands/bounds.toml").exists()  # other agents untouched
    assert not (root / "GEMINI.md").exists()
    assert set(report["created"]) == {
        "AGENTS.md", ".claude/commands/bounds.md", ".claude/skills/bounds/SKILL.md",
    }


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
    # No fix key when there's nothing to fix.
    assert agentsync.run_agent(root, mode="check") == {
        "ok": True, "missing": [], "stale": [], "configured": [],
    }


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


def test_check_flags_garbled_in_marker_body_as_stale(tmp_path):
    """A synced file whose in-marker body is overwritten with garbage no longer passes --check:
    the stamp hash no longer matches the body, so it reports `stale` (not configured), with the
    exact refresh command. Existence alone is not enough."""
    root = _mk_root(tmp_path)
    (root / ".claude").mkdir()
    agentsync.run_agent(root, mode="sync", only={"claude"})
    assert agentsync.run_agent(root, mode="check")["ok"] is True  # clean right after sync

    path = root / ".claude/commands/bounds.md"
    garbled = path.read_text(encoding="utf-8").replace("bounds $ARGUMENTS", "rm -rf /")
    path.write_text(garbled, encoding="utf-8")

    result = agentsync.run_agent(root, mode="check")
    assert result["ok"] is False
    assert "claude" in result["stale"]
    assert "claude" not in result["configured"]
    assert result["fix"] == "bounds agent --sync"


def test_check_flags_unstamped_block_as_stale(tmp_path):
    """A bounds block present but lacking a version/hash stamp (e.g. synced by an older bounds
    that didn't stamp) reads as stale so `--check` prompts a refresh."""
    root = _mk_root(tmp_path)
    (root / "AGENTS.md").write_text(
        "<!-- BOUNDS:START -->\n## Bounds\n\nlegacy.\n<!-- BOUNDS:END -->\n", encoding="utf-8"
    )
    result = agentsync.run_agent(root, mode="check")
    assert result["ok"] is False
    assert "codex" in result["stale"] and "opencode" in result["stale"]
    assert result["fix"] == "bounds agent --sync"


def test_check_ignores_version_drift_when_body_matches(monkeypatch, tmp_path):
    """A bounds version bump alone must NOT mark a file stale: the dev version moves every commit,
    so staleness keys on the body hash, not the stamped version. A byte-identical body stays ok."""
    root = _mk_root(tmp_path)
    (root / ".claude").mkdir()
    agentsync.run_agent(root, mode="sync", only={"claude"})

    # Simulate a CLI upgrade: the installed version moves past the stamp on disk, but the contract
    # body is unchanged — the file is still up to date.
    monkeypatch.setattr(agentsync, "_version", lambda: "99.99.99")
    result = agentsync.run_agent(root, mode="check")
    assert result["ok"] is True
    assert "claude" in result["configured"]
    assert result["stale"] == []


def test_legacy_dedicated_file_without_markers_is_migrated(tmp_path):
    """A dedicated file written by an older bounds (bounds content, no markers) must be migrated
    to a stamped block on sync — not skipped — so it never gets stuck reporting 'missing'."""
    root = _mk_root(tmp_path)
    legacy = root / ".claude/commands/bounds.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("# /bounds\n\nRun `bounds list` to read this project's architecture.\n", "utf-8")

    report = agentsync.run_agent(root, mode="sync", only={"claude"})
    assert ".claude/commands/bounds.md" in report["updated"]
    assert ".claude/commands/bounds.md" not in report["skipped_custom"]

    text = legacy.read_text("utf-8")
    assert "<!-- BOUNDS:START -->" in text and "<!-- BOUNDS:END -->" in text
    assert text.startswith("---")  # activating front-matter supplied during migration

    result = agentsync.run_agent(root, mode="check", only={"claude"})
    assert result["ok"] is True
    assert "claude" in result["configured"]


def test_sync_restores_deleted_front_matter_on_dedicated_file(tmp_path):
    """If a human deletes a dedicated file's activating front-matter, --check flags it stale and
    --sync restores it — otherwise the tool silently de-registers while reporting healthy."""
    root = _mk_root(tmp_path)
    (root / ".claude").mkdir()
    agentsync.run_agent(root, mode="sync", only={"claude"})
    target = root / ".claude/commands/bounds.md"

    # Strip the leading front-matter, leaving the managed block intact.
    text = target.read_text("utf-8")
    target.write_text(text[text.index("<!-- BOUNDS:START -->"):], "utf-8")
    assert not target.read_text("utf-8").startswith("---")

    # The block body is still current, but the missing front-matter makes it stale.
    result = agentsync.run_agent(root, mode="check", only={"claude"})
    assert "claude" in result["stale"]

    # Re-sync restores the front-matter; the file is configured again.
    report = agentsync.run_agent(root, mode="sync", only={"claude"})
    assert ".claude/commands/bounds.md" in report["updated"]
    assert target.read_text("utf-8").startswith("---")
    result = agentsync.run_agent(root, mode="check", only={"claude"})
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# Interactive "pick your AI" picker for `bounds agent --sync`
# ---------------------------------------------------------------------------
def test_prompt_agent_selection_parses_choices(monkeypatch):
    from bounds import cli

    available = cli._AGENT_FLAGS

    def with_reply(reply):
        monkeypatch.setattr(cli.click, "prompt", lambda *a, **k: reply)

    with_reply("all")
    assert cli._prompt_agent_selection(available, set()) is None      # 'all' → all (no filter)

    with_reply("1, cursor")
    assert cli._prompt_agent_selection(available, set()) == {"claude", "cursor"}  # number + name

    with_reply("")
    assert cli._prompt_agent_selection(available, {"cursor"}) == {"cursor"}  # Enter → detected
    assert cli._prompt_agent_selection(available, set()) is None      # Enter, none detected → all

    with_reply("nonsense")
    assert cli._prompt_agent_selection(available, set()) is None      # bad input → all, never nothing


def test_sync_non_tty_skips_prompt_and_wires_all(monkeypatch, tmp_path):
    # A piped/CI run (CliRunner stdout is not a TTY) must NOT prompt — it wires every agent
    # (the prior kitchen-sink behavior) so automation never blocks on input.
    from click.testing import CliRunner

    from bounds.cli import main

    (tmp_path / ".bounds").mkdir()
    monkeypatch.chdir(tmp_path)

    def explode(*a, **k):  # the picker must never be reached in non-TTY
        raise AssertionError("prompt was shown in a non-interactive run")

    monkeypatch.setattr("bounds.cli._prompt_agent_selection", explode)
    res = CliRunner().invoke(main, ["agent", "--sync"])
    assert res.exit_code == 0
    report = json.loads(res.output)
    assert "AGENTS.md" in (report.get("created", []) + report.get("updated", []))


def test_agent_check_stays_json_when_not_a_tty(monkeypatch, tmp_path):
    # --check is a CI gate: even though sync/detect are TTY-aware, --check must stay
    # JSON-default in non-TTY so an automated gate always parses a machine result.
    from click.testing import CliRunner

    from bounds.cli import main

    (tmp_path / ".bounds").mkdir()
    monkeypatch.chdir(tmp_path)
    out = CliRunner().invoke(main, ["agent", "--check"]).output
    assert "configured" in json.loads(out)  # valid JSON gate result, not prose
