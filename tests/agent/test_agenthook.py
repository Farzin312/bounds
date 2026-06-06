"""Tests for the agent-invocation hook: bounds.agenthook + its agentsync/CLI wiring.

Covers the config knob (off|nudge|strict), the settings.json merge (non-destructive, idempotent,
malformed-safe), and — most importantly — the NON-REGRESSION / fail-open ladder of the runtime
hook: enforcement may only ever redirect the agent to an answer Bounds actually has, so every
"Bounds can't answer" path (no .bounds, miss, unmapped, already-consulted, wrong level, garbage,
error) must resolve to allow/no-op. A regression here would turn a Bounds gap into a dead end, which
is exactly the adoption failure this feature exists to prevent.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from click.testing import CliRunner

from bounds import agenthook, agentsync, config
from bounds.cli import main
from bounds.manifest import schema
from bounds.models import RootManifest


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
def _mk_project(tmp_path, invocation: str | None = None, *, with_manifest: bool = True):
    """A minimal initialized Bounds project (root.yaml + one subsystem) under ``tmp_path``."""
    bounds = tmp_path / ".bounds"
    (bounds / "manifests").mkdir(parents=True)
    root_yaml = (
        'version: "1"\n'
        "project: testproj\n"
        "languages: [python]\n"
        'enforce: "off"\n'
        "entry_points: []\n"
        "subsystems: [auth]\n"
    )
    if invocation is not None:
        root_yaml += f"agentsync:\n  invocation: {invocation}\n"
    (bounds / "root.yaml").write_text(root_yaml, encoding="utf-8")
    if with_manifest:
        (bounds / "manifests" / "auth.yaml").write_text(
            "name: auth\nrole: library\ncriticality: leaf\n"
            "description: authentication\npaths: [src/auth]\n"
            "exposes:\n  - {name: loginUser, kind: function}\nconsumes: []\n",
            encoding="utf-8",
        )
    return tmp_path


@pytest.fixture(autouse=True)
def _isolated_marker_dir(tmp_path, monkeypatch):
    """Point session markers at a throwaway dir so once-per-session state never leaks across tests."""
    monkeypatch.setenv("BOUNDS_HOOK_STATE_DIR", str(tmp_path / "_hookstate"))


# ===========================================================================
# config layer: invocation_level resolution + schema validation
# ===========================================================================
def test_invocation_level_absent_defaults_to_nudge():
    """An unconfigured project resolves to the default (nudge) so it still gets the gentle reminder — the whole point is to not silently fail to adopt Bounds."""
    assert RootManifest().invocation_level() == "nudge"
    assert config.DEFAULT_INVOCATION == "nudge"


def test_invocation_level_explicit_values():
    """Each valid level round-trips through from_dict → invocation_level()."""
    for level in ("off", "nudge", "strict"):
        m = RootManifest.from_dict({"version": "1", "project": "x", "agentsync": {"invocation": level}})
        assert m.invocation_level() == level


def test_invocation_level_invalid_falls_back_without_crashing():
    """A typo'd level resolves to the default (never crashes the hook/writer path); schema flags it separately."""
    m = RootManifest.from_dict({"version": "1", "project": "x", "agentsync": {"invocation": "STRIKT"}})
    assert m.invocation_level() == "nudge"
    issues = schema.validate_root({"version": "1", "project": "x", "agentsync": {"invocation": "STRIKT"}})
    assert any(i.code == "E_SCHEMA_INVALID" and "invocation" in i.message for i in issues)


def test_invocation_unquoted_off_is_not_silently_dropped():
    """YAML parses unquoted `off` as boolean False; it must still resolve to "off", not the default — otherwise a user who turned it off would silently keep getting nudged and never know why."""
    import yaml
    parsed = yaml.safe_load("agentsync:\n  invocation: off\n")
    assert parsed["agentsync"]["invocation"] is False  # the trap
    m = RootManifest.from_dict({"version": "1", "project": "x", **parsed})
    assert m.invocation_level() == "off"
    # And `validate` does not false-flag it as an invalid level.
    assert schema.validate_root({"version": "1", "project": "x", **parsed}) == []


def test_valid_agentsync_block_has_no_schema_issues():
    """A well-formed agentsync block validates clean."""
    assert schema.validate_root({"version": "1", "project": "x", "agentsync": {"invocation": "strict"}}) == []


def test_agentsync_must_be_a_mapping():
    """A non-mapping agentsync value is a schema error (fail-soft Issue, not a crash)."""
    issues = schema.validate_root({"version": "1", "project": "x", "agentsync": "strict"})
    assert any(i.code == "E_SCHEMA_INVALID" and "must be a mapping" in i.message for i in issues)


def test_malformed_agentsync_does_not_crash_the_loader():
    """A bare `agentsync: strict` string must NOT raise in from_dict (dict('strict') would) — the
    manifest has to load so validate_root can report the actionable E_SCHEMA_INVALID instead of a
    generic E_INTERNAL. The same defensive coercion guards sdd/roles/criticality."""
    for block in ("agentsync", "sdd", "roles", "criticality"):
        m = RootManifest.from_dict({"version": "1", "project": "x", block: "garbage"})  # must not raise
        assert getattr(m, block) == {}
    # ...and the resolver still works on the degraded value.
    assert RootManifest.from_dict(
        {"version": "1", "project": "x", "agentsync": "strict"}
    ).invocation_level() == "nudge"


# ===========================================================================
# merge_settings_hooks — pure, non-destructive, idempotent
# ===========================================================================
def test_merge_nudge_adds_only_userpromptsubmit():
    """Nudge mode installs only the UserPromptSubmit reminder hook, not the strict search gate."""
    new, changed = agenthook.merge_settings_hooks({}, "nudge")
    assert changed
    assert set(new["hooks"]) == {"UserPromptSubmit"}


def test_merge_strict_adds_both_events_with_tool_matcher():
    """Strict mode installs both hook events and scopes PreToolUse to the broad-search tools Bounds can redirect."""
    new, _ = agenthook.merge_settings_hooks({}, "strict")
    assert set(new["hooks"]) == {"UserPromptSubmit", "PreToolUse"}
    assert new["hooks"]["PreToolUse"][0]["matcher"] == "Bash|Grep|Task"


def test_merge_preserves_foreign_hooks_and_keys_and_removes_on_off():
    """A user's own hook and top-level keys survive; only Bounds-owned entries are managed/removed."""
    user = {
        "model": "opus",
        "hooks": {
            "PreToolUse": [
                {"matcher": "Edit", "hooks": [{"type": "command", "command": "my-linter"}]},
                {"matcher": "Bash|Grep|Task", "hooks": [{"type": "command", "command": "bounds agent-hook"}]},
            ]
        },
    }
    off, changed = agenthook.merge_settings_hooks(user, "off")
    assert changed
    cmds = [h["command"] for e in off["hooks"]["PreToolUse"] for h in e["hooks"]]
    assert cmds == ["my-linter"]  # foreign kept, bounds removed
    assert off["model"] == "opus"  # foreign top-level key untouched


def test_merge_is_idempotent():
    """Merging the same Bounds-owned strict hook twice reports unchanged on the second pass."""
    once, _ = agenthook.merge_settings_hooks({}, "strict")
    _twice, changed = agenthook.merge_settings_hooks(once, "strict")
    assert changed is False


def test_merge_off_on_empty_is_noop():
    """Turning hooks off against an empty settings object is a no-op, not a synthetic file write."""
    new, changed = agenthook.merge_settings_hooks({}, "off")
    assert changed is False and new == {}


# ===========================================================================
# sync_claude_hooks — file I/O, malformed-safe
# ===========================================================================
def test_sync_hooks_creates_file_at_nudge(tmp_path):
    """Syncing hooks at nudge creates settings.json with the reminder hook."""
    assert agenthook.sync_claude_hooks(tmp_path, "nudge") == "created"
    data = json.loads(agenthook.settings_path(tmp_path).read_text())
    assert "UserPromptSubmit" in data["hooks"]


def test_sync_hooks_malformed_json_is_left_untouched(tmp_path):
    """A settings.json that isn't valid JSON is reported, never clobbered (never destroy a user's file)."""
    path = agenthook.settings_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{ not valid json", encoding="utf-8")
    assert agenthook.sync_claude_hooks(tmp_path, "strict") == "skipped_malformed"
    assert path.read_text(encoding="utf-8") == "{ not valid json"  # byte-for-byte intact


def test_sync_hooks_off_with_no_file_writes_nothing(tmp_path):
    """At off, sync leaves a missing settings.json missing instead of creating an empty file."""
    assert agenthook.sync_claude_hooks(tmp_path, "off") == "unchanged"
    assert not agenthook.settings_path(tmp_path).exists()


def test_sync_hooks_deterministic(tmp_path):
    """Two syncs of the same level produce byte-identical settings.json (determinism invariant)."""
    agenthook.sync_claude_hooks(tmp_path, "strict")
    first = agenthook.settings_path(tmp_path).read_text(encoding="utf-8")
    assert agenthook.sync_claude_hooks(tmp_path, "strict") == "unchanged"
    assert agenthook.settings_path(tmp_path).read_text(encoding="utf-8") == first


# ===========================================================================
# run_hook — the fail-open / non-regression ladder
# ===========================================================================
def test_hook_no_bounds_dir_fails_open(tmp_path):
    """No .bounds/ anywhere → no-op: never nudge/gate when Bounds can't answer anything."""
    assert agenthook.run_hook(
        {"hook_event_name": "UserPromptSubmit", "prompt": "what files handle auth?", "cwd": str(tmp_path)}
    ) == {}


def test_hook_garbage_payload_fails_open():
    """A malformed hook payload returns allow/no-op so a bad event never blocks the agent."""
    assert agenthook.run_hook("not a dict") == {}
    assert agenthook.run_hook({}) == {}


def test_hook_nudge_injects_on_architectural_prompt_once_per_session(tmp_path):
    """Nudge mode injects Bounds context once per session for architecture-shaped prompts."""
    proj = _mk_project(tmp_path, "nudge")
    ev = {"hook_event_name": "UserPromptSubmit", "prompt": "what files do I need to touch for the profile?",
          "session_id": "s1", "cwd": str(proj)}
    first = agenthook.run_hook(ev)
    assert first["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "additionalContext" in first["hookSpecificOutput"]
    assert "bounds agent --invocation off" in first["hookSpecificOutput"]["additionalContext"]  # opt-out surfaced
    # Same session again → silent (say it once).
    assert agenthook.run_hook(ev) == {}


def test_hook_nudge_silent_on_non_architectural_prompt(tmp_path):
    """Non-architectural prompts do not get Bounds context, keeping the hook quiet for trivial edits."""
    proj = _mk_project(tmp_path, "nudge")
    assert agenthook.run_hook(
        {"hook_event_name": "UserPromptSubmit", "prompt": "fix this typo", "session_id": "s2", "cwd": str(proj)}
    ) == {}


def test_hook_off_level_never_fires(tmp_path):
    """At off, a prior/stale hook no-ops: the config is the source of truth at runtime too."""
    proj = _mk_project(tmp_path, "off")
    assert agenthook.run_hook(
        {"hook_event_name": "UserPromptSubmit", "prompt": "what depends on auth?", "session_id": "s3", "cwd": str(proj)}
    ) == {}


def test_hook_strict_asks_on_mapped_grep(tmp_path):
    """strict pauses before a Grep whose target is in Bounds' declared surface (a subsystem name)."""
    proj = _mk_project(tmp_path, "strict")
    out = agenthook.run_hook(
        {"hook_event_name": "PreToolUse", "tool_name": "Grep", "tool_input": {"pattern": "auth"},
         "session_id": "s4", "cwd": str(proj)}
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert "bounds" in out["hookSpecificOutput"]["permissionDecisionReason"].lower()


def test_hook_strict_asks_on_mapped_exposed_symbol(tmp_path):
    """A declared exposes symbol (loginUser) is 'mapped' → ask."""
    proj = _mk_project(tmp_path, "strict")
    out = agenthook.run_hook(
        {"hook_event_name": "PreToolUse", "tool_name": "Grep", "tool_input": {"pattern": "loginUser"},
         "session_id": "s5", "cwd": str(proj)}
    )
    assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "ask"


def test_hook_strict_allows_unmapped_grep(tmp_path):
    """The crux of non-regression: an unmapped term is NEVER gated — the agent greps freely."""
    proj = _mk_project(tmp_path, "strict")
    assert agenthook.run_hook(
        {"hook_event_name": "PreToolUse", "tool_name": "Grep", "tool_input": {"pattern": "zxqwvgibberish"},
         "session_id": "s6", "cwd": str(proj)}
    ) == {}


def test_hook_strict_allows_after_bounds_consulted(tmp_path):
    """Once the agent ran a `bounds` command this session, its follow-up mapped search is allowed."""
    proj = _mk_project(tmp_path, "strict")
    # Bash running bounds records the breadcrumb (and is itself never gated).
    assert agenthook.run_hook(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "bounds describe auth"},
         "session_id": "s7", "cwd": str(proj)}
    ) == {}
    # Now a mapped grep in the same session is allowed (homework done).
    assert agenthook.run_hook(
        {"hook_event_name": "PreToolUse", "tool_name": "Grep", "tool_input": {"pattern": "auth"},
         "session_id": "s7", "cwd": str(proj)}
    ) == {}


def test_hook_nudge_level_does_not_gate_pretool(tmp_path):
    """PreToolUse gating only happens at strict; at nudge a (stale) PreToolUse event no-ops."""
    proj = _mk_project(tmp_path, "nudge")
    assert agenthook.run_hook(
        {"hook_event_name": "PreToolUse", "tool_name": "Grep", "tool_input": {"pattern": "auth"},
         "session_id": "s8", "cwd": str(proj)}
    ) == {}


def test_hook_strict_ignores_unwatched_tools(tmp_path):
    """A tool outside the gated set (e.g. Glob/Read) is never gated."""
    proj = _mk_project(tmp_path, "strict")
    assert agenthook.run_hook(
        {"hook_event_name": "PreToolUse", "tool_name": "Glob", "tool_input": {"pattern": "auth"},
         "session_id": "s9", "cwd": str(proj)}
    ) == {}


def test_hook_strict_allows_when_manifests_absent(tmp_path):
    """A .bounds/ that declares a subsystem with no manifest file must not trap a search (load fails → allow)."""
    proj = _mk_project(tmp_path, "strict", with_manifest=False)
    assert agenthook.run_hook(
        {"hook_event_name": "PreToolUse", "tool_name": "Grep", "tool_input": {"pattern": "auth"},
         "session_id": "s10", "cwd": str(proj)}
    ) == {}


# ===========================================================================
# end-to-end via agentsync.run_agent (sync writes the hook + directive)
# ===========================================================================
def test_sync_nudge_writes_userpromptsubmit_hook_and_directive(tmp_path):
    """Agent sync at nudge writes both the Claude hook and the always-read invocation directive."""
    proj = _mk_project(tmp_path, "nudge")
    report = agentsync.run_agent(proj, mode="sync", only={"claude"})
    assert report["invocation"] == "nudge"
    settings = json.loads((proj / ".claude/settings.json").read_text())
    assert set(settings["hooks"]) == {"UserPromptSubmit"}
    assert "Invocation policy" in (proj / "AGENTS.md").read_text(encoding="utf-8")


def test_sync_strict_writes_pretooluse_gate(tmp_path):
    """Agent sync at strict writes the PreToolUse gate in addition to the reminder hook."""
    proj = _mk_project(tmp_path, "strict")
    agentsync.run_agent(proj, mode="sync", only={"claude"})
    settings = json.loads((proj / ".claude/settings.json").read_text())
    assert set(settings["hooks"]) == {"UserPromptSubmit", "PreToolUse"}


def test_sync_off_writes_no_hook_and_no_directive(tmp_path):
    """Agent sync at off removes active enforcement artifacts while leaving advisory files usable."""
    proj = _mk_project(tmp_path, "off")
    agentsync.run_agent(proj, mode="sync", only={"claude"})
    assert not (proj / ".claude/settings.json").exists()
    assert "Invocation policy" not in (proj / "AGENTS.md").read_text(encoding="utf-8")


def test_sync_directive_lands_in_every_harness_always_read_file(tmp_path):
    """At nudge, every harness's always-read file carries the imperative directive (their strongest lever)."""
    proj = _mk_project(tmp_path, "nudge")
    (proj / ".aider.conf.yml").write_text("model: gpt-4\n", encoding="utf-8")
    agentsync.run_agent(proj, mode="sync")  # all agents
    for rel in ("AGENTS.md", "GEMINI.md", ".github/copilot-instructions.md",
                ".cursor/rules/bounds.mdc", ".windsurf/rules/bounds.md", "CLAUDE.md"):
        assert "Invocation policy" in (proj / rel).read_text(encoding="utf-8"), rel
    # Aider's YAML pointer carries the directive as comments and stays valid YAML.
    import yaml
    aider = (proj / ".aider.conf.yml").read_text(encoding="utf-8")
    assert "# **Invocation policy" in aider
    assert yaml.safe_load(aider)["model"] == "gpt-4"


def test_check_flags_a_missing_claude_hook_at_strict(tmp_path):
    """`--check` must catch a deleted/never-written hook at nudge/strict — the hook is part of
    Claude's wiring there, so its absence is real drift CI should fail on (not silently 'configured')."""
    proj = _mk_project(tmp_path, "strict")
    agentsync.run_agent(proj, mode="sync", only={"claude"})  # writes pointer files + the hook
    assert (proj / ".claude/settings.json").exists()
    # delete the hook — the pointer/skill/command files are still intact
    (proj / ".claude/settings.json").unlink()
    report = agentsync.run_agent(proj, mode="check", only={"claude"})
    assert "claude" in report["stale"] and not report["ok"]


def test_check_passes_when_claude_hook_is_current(tmp_path):
    """A freshly-synced strict project is 'configured' (the hook check doesn't false-flag a current hook)."""
    proj = _mk_project(tmp_path, "strict")
    agentsync.run_agent(proj, mode="sync", only={"claude"})
    report = agentsync.run_agent(proj, mode="check", only={"claude"})
    assert report["ok"] and "claude" in report["configured"]


def test_check_corrupt_settings_json_flags_stale(tmp_path):
    """A corrupted settings.json can't be confirmed → the gate surfaces it as stale, never silently ok."""
    proj = _mk_project(tmp_path, "nudge")
    agentsync.run_agent(proj, mode="sync", only={"claude"})
    (proj / ".claude/settings.json").write_text("{ corrupt", encoding="utf-8")
    report = agentsync.run_agent(proj, mode="check", only={"claude"})
    assert "claude" in report["stale"]


def test_check_off_level_does_not_require_a_hook(tmp_path):
    """At off, no hook is owed — a project with no settings.json is 'configured', not stale."""
    proj = _mk_project(tmp_path, "off")
    agentsync.run_agent(proj, mode="sync", only={"claude"})
    assert not (proj / ".claude/settings.json").exists()
    report = agentsync.run_agent(proj, mode="check", only={"claude"})
    assert report["ok"] and "claude" in report["configured"]


def test_sync_off_removes_a_previously_written_hook_but_keeps_foreign(tmp_path):
    """Flipping to off cleans Bounds' own hook while preserving the user's hooks (round-trip safety)."""
    proj = _mk_project(tmp_path, "strict")
    agentsync.run_agent(proj, mode="sync", only={"claude"})
    # user adds their own hook
    path = proj / ".claude/settings.json"
    data = json.loads(path.read_text())
    data.setdefault("hooks", {}).setdefault("PreToolUse", []).append(
        {"matcher": "Edit", "hooks": [{"type": "command", "command": "my-linter"}]}
    )
    path.write_text(json.dumps(data))
    # flip to off via a re-sync at off level
    (proj / ".bounds/root.yaml").write_text(
        (proj / ".bounds/root.yaml").read_text().replace("strict", "off"), encoding="utf-8"
    )
    agentsync.run_agent(proj, mode="sync", only={"claude"})
    after = json.loads(path.read_text())
    cmds = [h["command"] for e in after.get("hooks", {}).get("PreToolUse", []) for h in e["hooks"]]
    assert "my-linter" in cmds and "bounds agent-hook" not in cmds


# ===========================================================================
# CLI: agent-hook entry point + agent --invocation round-trip
# ===========================================================================
def _invoke(monkeypatch, cwd, args, stdin=None):
    monkeypatch.chdir(cwd)
    return CliRunner().invoke(main, args, input=stdin)


def test_cli_agent_hook_reads_stdin_emits_json_exit_0(tmp_path, monkeypatch):
    """The hidden CLI hook reads one JSON event from stdin and emits a JSON decision at exit 0."""
    proj = _mk_project(tmp_path, "nudge")
    monkeypatch.setenv("BOUNDS_HOOK_STATE_DIR", str(tmp_path / "_st"))
    payload = json.dumps({"hook_event_name": "UserPromptSubmit",
                          "prompt": "what depends on auth?", "session_id": "z1", "cwd": str(proj)})
    res = _invoke(monkeypatch, proj, ["agent-hook"], stdin=payload)
    assert res.exit_code == 0
    assert "additionalContext" in res.output


def test_cli_agent_hook_empty_stdin_is_silent_exit_0(tmp_path, monkeypatch):
    """Empty stdin to the hidden hook is treated as no event: silent success."""
    proj = _mk_project(tmp_path, "nudge")
    res = _invoke(monkeypatch, proj, ["agent-hook"], stdin="")
    assert res.exit_code == 0
    assert res.output == ""


def test_cli_agent_hook_garbage_stdin_never_errors(tmp_path, monkeypatch):
    """Garbage stdin fails open on stdout but reports the degraded read on stderr."""
    proj = _mk_project(tmp_path, "nudge")
    res = _invoke(monkeypatch, proj, ["agent-hook"], stdin="}{not json")
    assert res.exit_code == 0
    assert res.stdout == ""
    assert "bounds: stdin was not valid JSON" in res.stderr


def test_cli_agent_hook_real_pipe_does_not_wait_for_eof(tmp_path):
    """A complete hook event exits while the parent intentionally keeps the stdin pipe open."""
    proj = _mk_project(tmp_path, "nudge")
    read_fd, write_fd = os.pipe()
    env = dict(os.environ)
    env["BOUNDS_HOOK_STATE_DIR"] = str(tmp_path / "_pipe_state")
    process = subprocess.Popen(
        [sys.executable, "-c", "from bounds.cli import main; main()", "agent-hook"],
        cwd=proj,
        env=env,
        stdin=read_fd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    os.close(read_fd)
    payload = json.dumps(
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "what depends on auth?",
            "session_id": "pipe",
            "cwd": str(proj),
        }
    )
    os.write(write_fd, payload.encode("utf-8"))
    try:
        stdout, stderr = process.communicate(timeout=2)
    finally:
        os.close(write_fd)
        if process.poll() is None:
            process.kill()
            process.wait()

    assert process.returncode == 0
    assert "additionalContext" in stdout
    assert stderr == ""


def test_cli_agent_invocation_sets_root_yaml_and_syncs(tmp_path, monkeypatch):
    """`agent --invocation strict --claude` persists the root config and syncs the matching hook."""
    proj = _mk_project(tmp_path, None)  # no agentsync block initially
    res = _invoke(monkeypatch, proj, ["agent", "--invocation", "strict", "--claude"])
    assert res.exit_code == 0
    assert "invocation: strict" in (proj / ".bounds/root.yaml").read_text(encoding="utf-8")
    settings = json.loads((proj / ".claude/settings.json").read_text())
    assert "PreToolUse" in settings["hooks"]


def test_cli_agent_invocation_rejects_combo_with_check(tmp_path, monkeypatch):
    """Changing invocation and checking wiring are mutually exclusive CLI modes."""
    proj = _mk_project(tmp_path, None)
    res = _invoke(monkeypatch, proj, ["agent", "--invocation", "nudge", "--check"])
    assert res.exit_code == config.EXIT_FATAL


def test_cli_agent_invocation_without_bounds_errors(tmp_path, monkeypatch):
    """--invocation needs an initialized project; without one it fails with the actionable fix."""
    res = _invoke(monkeypatch, tmp_path, ["agent", "--invocation", "nudge"])
    assert res.exit_code == config.EXIT_FATAL
    assert "E_MANIFEST_NOT_FOUND" in res.output or "init" in res.output
