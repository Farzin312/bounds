"""Tests for the per-agent native command/skill artifacts written by `bounds agent --sync`.

These files are how each AI actually invokes Bounds, so format correctness is load-bearing: a
malformed TOML/frontmatter file is silently ignored by the tool. We assert each is generated in the
right place, parses in its native format, carries a stamp, is idempotent, and is hand-edit safe —
and that an agent with no committable command mechanism (aider) gets none rather than a faked file.
"""

from __future__ import annotations

import tomllib

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
    "codex": ".codex/skills/bounds/SKILL.md",
    "gemini": ".gemini/commands/bounds.toml",
    "opencode": ".opencode/command/bounds.md",
    "copilot": ".github/prompts/bounds.prompt.md",
    "cursor": ".cursor/commands/bounds.md",
    "windsurf": ".windsurf/workflows/bounds.md",
}


def test_every_capable_agent_gets_its_native_artifact(tmp_path):
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync")  # all agents
    for key, rel in _ARTIFACTS.items():
        assert (root / rel).is_file(), f"{key}: missing {rel}"
        text = (root / rel).read_text(encoding="utf-8")
        assert "BOUNDS:GENERATED" in text, f"{key}: artifact not stamped"


def test_aider_has_no_faked_command_artifact(tmp_path):
    # Aider has no committable command mechanism — it must get only its pointer config, never a
    # fabricated command file.
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync", only={"aider"})
    assert "aider" not in agentsync._AGENT_ARTIFACTS
    assert not (root / ".aider").exists()


def test_gemini_command_is_valid_toml_with_args(tmp_path):
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync", only={"gemini"})
    data = tomllib.loads((root / ".gemini/commands/bounds.toml").read_text(encoding="utf-8"))
    assert "prompt" in data and "description" in data
    assert "{{args}}" in data["prompt"]  # Gemini argument placeholder


def test_skill_files_have_valid_autotrigger_frontmatter(tmp_path):
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync", only={"claude", "codex"})
    for rel in (".claude/skills/bounds/SKILL.md", ".codex/skills/bounds/SKILL.md"):
        fm = _frontmatter((root / rel).read_text(encoding="utf-8"))
        assert fm.get("name") == "bounds"
        # The description is the auto-trigger matcher — it must describe WHEN to use bounds.
        assert "Bounds CLI" in fm.get("description", "")
        assert "blast radius" in fm["description"]


def test_copilot_and_opencode_frontmatter_parse(tmp_path):
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync", only={"copilot", "opencode"})
    cop = _frontmatter((root / ".github/prompts/bounds.prompt.md").read_text(encoding="utf-8"))
    assert cop.get("mode") == "agent" and "description" in cop
    oc = _frontmatter((root / ".opencode/command/bounds.md").read_text(encoding="utf-8"))
    assert oc.get("agent") and "description" in oc
    assert "$ARGUMENTS" in (root / ".opencode/command/bounds.md").read_text(encoding="utf-8")


def test_artifacts_are_idempotent_on_resync(tmp_path):
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync")
    report = agentsync.run_agent(root, mode="sync")
    assert report["created"] == [] and report["updated"] == []
    for rel in _ARTIFACTS.values():
        assert rel in report["unchanged"], f"{rel} not reported already-current"


def test_artifact_hand_edit_inside_block_is_not_clobbered(tmp_path):
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


def test_artifacts_only_for_selected_agents(tmp_path):
    root = _mk_root(tmp_path)
    agentsync.run_agent(root, mode="sync", only={"gemini"})
    assert (root / ".gemini/commands/bounds.toml").is_file()
    assert not (root / ".claude/skills/bounds/SKILL.md").exists()
    assert not (root / ".windsurf/workflows/bounds.md").exists()
