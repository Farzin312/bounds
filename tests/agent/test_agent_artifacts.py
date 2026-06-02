"""Tests for the per-agent native command/skill artifacts written by `bounds agent --sync`.

These files are how each AI actually invokes Bounds, so format correctness is load-bearing: a
malformed TOML/frontmatter file is silently ignored by the tool. We assert each is generated in the
right place, parses in its native format, carries a stamp, is idempotent, and is hand-edit safe —
and that an agent with no committable command mechanism (aider) gets none rather than a faked file.
"""

from __future__ import annotations

import pytest
import yaml

from bounds import agentsync


def _mk_root(tmp_path):
    (tmp_path / ".bounds").mkdir()
    return tmp_path


def _frontmatter(text: str) -> dict:
    assert text.startswith("---\n"), "expected leading YAML front-matter"
    end = text.index("\n---\n", 4)
    return yaml.safe_load(text[4:end])


# Path each capable agent's targeted artifact must land at.
_ARTIFACTS = {
    "claude": ".claude/skills/bounds/SKILL.md",
    "codex": ".agents/skills/bounds/SKILL.md",
    "gemini": ".gemini/commands/bounds.toml",
    "opencode": ".opencode/commands/bounds.md",
    "copilot": ".github/prompts/bounds.prompt.md",
    "cursor": ".cursor/commands/bounds.md",
    "windsurf": ".windsurf/workflows/bounds.md",
}


def test_every_capable_agent_gets_its_native_artifact(tmp_path):
    """A full sync must land each capable agent's native command/skill at its exact discovery path and stamp it, or the tool silently ignores the file."""
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync")  # all agents
    for key, rel in _ARTIFACTS.items():
        assert (root / rel).is_file(), f"{key}: missing {rel}"
        text = (root / rel).read_text(encoding="utf-8")
        assert "BOUNDS:GENERATED" in text, f"{key}: artifact not stamped"


def test_aider_has_no_faked_command_artifact(tmp_path):
    """Aider has no committable command mechanism, so it must get no native artifact at all — fabricating one would write a file the tool never loads."""
    # Aider has no committable command mechanism — it must get only its pointer config, never a
    # fabricated command file.
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync", only={"aider"})
    assert "aider" not in agentsync._AGENT_ARTIFACTS
    assert not (root / ".aider").exists()


def test_gemini_command_is_valid_toml_with_args(tmp_path):
    """Gemini's command file must be valid TOML carrying prompt/description and the {{args}} placeholder — malformed TOML or a missing placeholder breaks /bounds arg forwarding."""
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync", only={"gemini"})
    tomllib = pytest.importorskip("tomllib")  # stdlib 3.11+; skip the TOML-parse assert on 3.10
    data = tomllib.loads((root / ".gemini/commands/bounds.toml").read_text(encoding="utf-8"))
    assert "prompt" in data and "description" in data
    assert "{{args}}" in data["prompt"]  # Gemini argument placeholder


def test_skill_files_have_valid_autotrigger_frontmatter(tmp_path):
    """Claude/codex SKILL.md front-matter must name the skill 'bounds' and describe WHEN to use it (the auto-trigger matcher) — without it the skill never fires."""
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync", only={"claude", "codex"})
    for rel in (".claude/skills/bounds/SKILL.md", ".agents/skills/bounds/SKILL.md"):
        fm = _frontmatter((root / rel).read_text(encoding="utf-8"))
        assert fm.get("name") == "bounds"
        # The description is the auto-trigger matcher — it must describe WHEN to use bounds.
        assert "Bounds CLI" in fm.get("description", "")
        assert "blast radius" in fm["description"]


def test_copilot_and_opencode_frontmatter_parse(tmp_path):
    """Copilot/opencode artifacts must carry the activating front-matter each tool requires (mode: agent / agent + $ARGUMENTS) or the command sits dormant."""
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync", only={"copilot", "opencode"})
    cop = _frontmatter((root / ".github/prompts/bounds.prompt.md").read_text(encoding="utf-8"))
    assert cop.get("mode") == "agent" and "description" in cop
    oc = _frontmatter((root / ".opencode/commands/bounds.md").read_text(encoding="utf-8"))
    assert oc.get("agent") and "description" in oc
    assert "$ARGUMENTS" in (root / ".opencode/commands/bounds.md").read_text(encoding="utf-8")


def test_opencode_command_lands_in_plural_commands_dir(tmp_path):
    """OpenCode loads commands only from the plural .opencode/commands/ dir; the singular command/ is never read, so landing the file there exactly is load-bearing."""
    # OpenCode discovers project commands in `.opencode/commands/` (plural) — the singular
    # `command/` dir is never loaded, so the exact path is load-bearing for the /bounds command.
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync", only={"opencode"})
    assert agentsync._AGENT_ARTIFACTS["opencode"][0].path == ".opencode/commands/bounds.md"
    assert (root / ".opencode/commands/bounds.md").is_file()
    assert not (root / ".opencode/command/bounds.md").exists()


def test_artifacts_are_idempotent_on_resync(tmp_path):
    """A no-op re-sync must rewrite nothing — every artifact reports `unchanged`, not created/updated — so repeated syncs never churn the working tree (determinism)."""
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync")
    report = agentsync.run_agent(root, mode="sync")
    assert report["created"] == [] and report["updated"] == []
    for rel in _ARTIFACTS.values():
        assert rel in report["unchanged"], f"{rel} not reported already-current"


def test_artifact_hand_edit_inside_block_is_not_clobbered(tmp_path):
    """A human edit inside a managed artifact must survive re-sync untouched and be reported skipped_custom with reason 'hand-edited' — sync never destroys human work."""
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync", only={"gemini"})
    path = root / ".gemini/commands/bounds.toml"
    edited = path.read_text(encoding="utf-8").replace("{{args}}", "MY HAND EDIT")
    path.write_text(edited, encoding="utf-8")
    report = agentsync.run_agent(root, mode="sync", only={"gemini"})
    assert path.read_text(encoding="utf-8") == edited  # untouched
    assert ".gemini/commands/bounds.toml" in report["skipped_custom"]
    assert report["skip_reasons"][".gemini/commands/bounds.toml"] == "hand-edited"


def test_skill_frontmatter_restored_if_removed(tmp_path):
    """Deleting a skill's activating front-matter must be restored on re-sync (not mistaken for a body hand-edit), else the skill silently de-registers while looking healthy."""
    # A dedicated file's tool-activating front-matter is restored on re-sync (else the skill
    # silently de-registers). Deleting it must not be mistaken for a hand-edit of the body.
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync", only={"claude"})
    path = root / ".claude/skills/bounds/SKILL.md"
    body_only = path.read_text(encoding="utf-8").split("\n---\n", 1)[1]  # drop front-matter
    path.write_text(body_only, encoding="utf-8")
    agentsync.run_agent(root, mode="sync", only={"claude"})
    restored = path.read_text(encoding="utf-8")
    assert restored.startswith("---\nname: bounds\n")  # front-matter back


def test_check_flags_corrupted_artifact_as_stale(tmp_path):
    """A present-but-corrupted native artifact must make --check report the agent stale (not configured) even when its pointer is current — existence alone is not health."""
    # A present-but-hand-edited native artifact is a real wiring risk (the tool loads a broken
    # file), so --check reports the agent stale even though its pointer is current.
    root = _mk_root(tmp_path)
    (root / ".claude").mkdir()
    agentsync.run_agent(root, mode="sync", only={"claude"})
    assert agentsync.run_agent(root, mode="check")["ok"] is True
    skill = root / ".claude/skills/bounds/SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8").replace("bounds list", "HACKED"),
                     encoding="utf-8")
    result = agentsync.run_agent(root, mode="check")
    assert "claude" in result["stale"] and "claude" not in result["configured"]


def test_check_ignores_absent_optional_artifact(tmp_path):
    """An absent OPTIONAL native skill must NOT flag codex stale when AGENTS.md already wires it — only present-but-broken artifacts count, so --check stays low-noise."""
    # codex is fully wired by AGENTS.md; an absent optional skill must NOT flag it (a re-sync
    # would add it non-destructively).
    import shutil
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync", only={"codex"})
    shutil.rmtree(root / ".agents")  # drop the optional native skill (cross-tool .agents/skills/)
    result = agentsync.run_agent(root, mode="check")
    assert "codex" in result["configured"]


def test_artifacts_only_for_selected_agents(tmp_path):
    """`only` must scope native artifacts to the selected agent — syncing gemini alone must not write claude/windsurf files, so users wire exactly what they asked for."""
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync", only={"gemini"})
    assert (root / ".gemini/commands/bounds.toml").is_file()
    assert not (root / ".claude/skills/bounds/SKILL.md").exists()
    assert not (root / ".windsurf/workflows/bounds.md").exists()
