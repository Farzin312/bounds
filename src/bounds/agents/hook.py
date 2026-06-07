"""Claude Code harness hook: UserPromptSubmit (nudge) and PreToolUse (strict).

Wired into ``.claude/settings.json`` by ``bounds agent --invocation [nudge|strict]``.
This harness-run code executes on every turn (even when the agent isn't calling Bounds),
so it must be lightweight (no tree walk, no heavy parsing).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from ..shared import config
from ..core.manifest import loader as manifest_loader

__all__ = [
    "HOOK_COMMAND",
    "merge_settings_hooks",
    "run_hook",
    "settings_path",
    "sync_claude_hooks",
]

# The command written into .claude/settings.json hook entries, and the substring that identifies a
# Bounds-owned entry on re-sync/removal. Keep these identical and stable: the signature is how a
# merge tells "ours" from a hook the user authored, so renaming it would orphan old entries.
HOOK_COMMAND = "bounds agent-hook"
HOOK_SIGNATURE = "bounds agent-hook"

# Tools the strict PreToolUse gate watches. Bash is watched ONLY to record that the agent ran
# `bounds` (the consulted breadcrumb) — it is never gated. Grep + Task (the Explore/subagent
# dispatch) are the architecture-search tools the gate actually considers asking on. Glob is
# deliberately omitted: globs are path lookups, rarely the "search the tree for a concept" move,
# and matching them against the surface is noisy.
_STRICT_MATCHER = "Bash|Grep|Task"
_GATED_TOOLS = ("Grep", "Task")

# ---------------------------------------------------------------------------
# Static message bodies (the agent/user reads these)
# ---------------------------------------------------------------------------
# The opt-out is surfaced inline in every message — the off-switch is always one copy-paste away,
# so a user who finds it noisy is never left wondering how to stop it.
_OPT_OUT = "disable: `bounds agent --invocation off`"

_REMINDER = (
    "This repo uses Bounds for architecture. Before grepping or dispatching a search agent, query "
    "the verified map: `bounds list`, `bounds describe <area>`, `bounds where <symbol>`, "
    "`bounds impact <subsystem>` — tree-sitter-verified and far leaner than scanning the tree. "
    "If `bounds where` returns `count: 0` it carries a `suggestions` block — follow it. If an area "
    "is unmapped or in a language Bounds doesn't parse, grep it directly. (" + _OPT_OUT + ")"
)


def _ask_reason(target: str) -> str:
    return (
        f"Bounds maps '{target}'. Try `bounds where {target}` / `bounds describe` first — verified "
        f"and far leaner than grepping. Approve to search anyway. ({_OPT_OUT})"
    )


# ===========================================================================
# Runtime decision (the `bounds agent-hook` command body)
# ===========================================================================
def run_hook(payload: dict, start: Path | None = None) -> dict:
    """Decide the hook response for one harness event. Never raises; fails open to ``{}``.

    ``payload`` is the harness hook event (Claude Code shape: ``hook_event_name``, ``session_id``,
    ``cwd``, ``tool_name``, ``tool_input``, ``prompt``). ``start`` overrides where to begin the
    walk for ``.bounds/`` (defaults to the payload ``cwd`` then the process CWD). Returns the
    hook-protocol dict to print on stdout; an empty dict means "print nothing / allow / no-op".
    """
    try:
        if not isinstance(payload, dict):
            return {}
        root = _resolve_root(payload, start)
        if root is None:
            return {}  # no .bounds/ anywhere above CWD — Bounds can't answer; stay out of the way.
        level = _resolve_level(root)
        if level == "off":
            return {}  # config says advisory-only; a stale hook from a prior level no-ops cleanly.
        event = payload.get("hook_event_name") or _infer_event(payload)
        if event == "UserPromptSubmit":
            return _on_prompt(payload, root)
        if event == "PreToolUse":
            return _on_pretool(payload, root, level)
        return {}
    except Exception:  # noqa: BLE001 - a hook must NEVER break the agent's turn; fail open.
        return {}


def _infer_event(payload: dict) -> str:
    """Best-effort event name when ``hook_event_name`` is absent (older/other harness)."""
    if "prompt" in payload:
        return "UserPromptSubmit"
    if "tool_name" in payload:
        return "PreToolUse"
    return ""


def _on_prompt(payload: dict, root: Path) -> dict:
    """Inject the one-line reminder into an architecture-shaped prompt, once per session."""
    prompt = str(payload.get("prompt") or "")
    if not _is_architectural(prompt):
        return {}
    session = str(payload.get("session_id") or "")
    if session and _marker_seen(root, session, "nudge"):
        return {}  # already reminded this session — say it once, then get out of the way.
    if session:
        _marker_set(root, session, "nudge")
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": _REMINDER,
        }
    }


def _on_pretool(payload: dict, root: Path, level: str) -> dict:
    """Coverage-gated pause before a broad search Bounds can answer. Fails open everywhere else."""
    if level != "strict":
        return {}  # PreToolUse only gates at strict; a stale strict hook under nudge no-ops.
    tool = str(payload.get("tool_name") or "")
    tin = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    session = str(payload.get("session_id") or "")

    # Bash is watched only to learn the agent consulted Bounds; it is never gated.
    if tool == "Bash":
        if session and _is_bounds_invocation(str(tin.get("command") or "")):
            _marker_set(root, session, "consulted")
        return {}

    if tool not in _GATED_TOOLS:
        return {}
    if session and _marker_seen(root, session, "consulted"):
        return {}  # the agent already ran Bounds this session — its follow-up search is legitimate.
    target = _extract_target(tool, tin)
    if not target:
        return {}  # can't tell what's being searched — allow.
    if not _maps_target(root, target):
        return {}  # Bounds doesn't map this — allow the search, never trap into a dead end.
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": _ask_reason(target),
        }
    }


# ---- prompt / target heuristics ------------------------------------------
# Architecture-shaped prompt signals. False negatives are harmless (no reminder = no regression);
# false positives are merely a once-per-session, non-blocking note — so this leans inclusive.
_ARCH_PATTERNS = re.compile(
    r"\b(what|which)\s+(files?|functions?|modules?|subsystems?|components?|parts?)\b"
    r"|\bwhat\s+(depends?\s+on|breaks?|calls?|uses?|imports?)\b"
    r"|\bwho\s+(calls?|uses?|depends?\s+on|imports?)\b"
    r"|\b(depends?\s+on|consumed?\s+by|blast\s+radius|impact\s+of)\b"
    r"|\bwhere\s+(is|are|does|do)\b"
    r"|\bhow\s+(does|do|is|are)\b.{0,40}\b(work|wired|connect|structured|architect)"
    r"|\b(architecture|data\s*flow|call\s*graph|dependency|dependencies)\b"
    r"|\bneed(s)?\s+to\s+(touch|change|modify|edit)\b"
    r"|\b(trace|map\s+out|understand)\b.{0,30}\b(code|flow|system|app|project|repo)",
    re.IGNORECASE,
)


def _is_architectural(prompt: str) -> bool:
    """True when a user prompt looks like an architecture / 'where does X live' question."""
    return bool(prompt) and bool(_ARCH_PATTERNS.search(prompt))


def _extract_target(tool: str, tin: dict) -> str:
    """The search subject from a tool call: the Grep pattern, or a Task's description+prompt."""
    if tool == "Grep":
        return str(tin.get("pattern") or "").strip()
    if tool == "Task":
        return f"{tin.get('description') or ''} {tin.get('prompt') or ''}".strip()
    return ""


_BOUNDS_CMD_RE = re.compile(
    r"\bbounds\s+"
    r"(list|describe|where|impact|validate|preflight|overview|init|discover|calibrate|agent|ci|cache)\b"
)


def _is_bounds_invocation(command: str) -> bool:
    """True when a Bash command actually runs a Bounds read subcommand (not prose mentioning it)."""
    return bool(_BOUNDS_CMD_RE.search(command))


def _maps_target(root: Path, target: str) -> bool:
    """Does Bounds' *declared* surface plausibly cover ``target``? Manifest-only, fail-open.

    Deliberately cheap (loads YAML manifests, never a tree-sitter walk) so it stays well inside the
    perf budget on every gated call. Matches a target word against declared ``exposes`` symbol names
    or a subsystem name against the target. Conservative on purpose: a symbol Bounds maps but does
    not *declare* won't match, so the gate fails open (allow) rather than risk trapping a legitimate
    search. Any load error → ``False`` (allow).
    """
    try:
        _root, subs, _issues = manifest_loader.load_all(root)
    except Exception:  # noqa: BLE001 - a broken/absent manifest must never trap a search.
        return False
    if not subs:
        return False
    target_lc = target.lower()
    # A declared subsystem name appearing in the target (handles hyphenated names tokens miss).
    for name in subs:
        if len(name) >= 3 and name.lower() in target_lc:
            return True
    exposed = {iface.name.lower() for sub in subs.values() for iface in sub.exposes}
    tokens = {t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", target)}
    return bool(tokens & exposed)


# ---- session markers (transient, never committed, no wall-clock) ----------
# Once-per-session de-dup for the nudge, and the "already consulted Bounds this session" breadcrumb
# for the strict gate. Keyed by (root, session_id, kind) so sessions never collide. Stored under the
# OS temp dir (overridable via BOUNDS_HOOK_STATE_DIR for tests / relocation), so they are never
# committed and survive only as long as the OS keeps temp. Session-scoped existence — no timestamps —
# keeps the decision deterministic given a fixed marker state.
def _marker_dir() -> Path:
    override = os.environ.get("BOUNDS_HOOK_STATE_DIR")
    if override:
        return Path(override)
    import tempfile

    return Path(tempfile.gettempdir()) / "bounds-hooks"


def _marker_path(root: Path, session: str, kind: str) -> Path:
    key = hashlib.sha256(f"{root.resolve()}\x00{session}\x00{kind}".encode()).hexdigest()[:16]
    return _marker_dir() / key


def _marker_seen(root: Path, session: str, kind: str) -> bool:
    try:
        return _marker_path(root, session, kind).is_file()
    except OSError:
        return False


def _marker_set(root: Path, session: str, kind: str) -> None:
    try:
        path = _marker_path(root, session, kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    except OSError:
        pass  # unwritable temp → degrade (re-nudge / re-check); never raise.


# ---- shared resolution ----------------------------------------------------
def _resolve_root(payload: dict, start: Path | None) -> Path | None:
    """Find the project root holding ``.bounds/``, starting from ``start``/payload cwd/process cwd."""
    if start is None:
        cwd = payload.get("cwd")
        start = Path(cwd) if cwd else Path.cwd()
    try:
        return manifest_loader.find_root(Path(start))
    except Exception:  # noqa: BLE001
        return None


def agent_hook_cmd() -> None:
    """The body of the hidden `bounds agent-hook` command (moved from cli.py)."""
    from ..shared.io import emit_loud, read_stdin_json

    payload = read_stdin_json() or {}
    try:
        result = run_hook(payload if isinstance(payload, dict) else {})
    except Exception as exc:  # noqa: BLE001 - hook must fail open
        emit_loud(f"agent hook failed open ({type(exc).__name__}: {exc})")
        result = {}
    if result:
        sys.stdout.write(json.dumps(result))
    sys.exit(config.EXIT_OK)


def _resolve_level(root: Path) -> str:
    """The configured invocation level (off|nudge|strict), fail-soft to the default on any error."""
    try:
        return manifest_loader.load_root(root).invocation_level()
    except Exception:  # noqa: BLE001
        return config.DEFAULT_INVOCATION


# ===========================================================================
# Wiring: Bounds-owned hook entries in .claude/settings.json
# ===========================================================================
def settings_path(root: Path) -> Path:
    """The project (committed, team-shared) Claude settings file Bounds writes hooks into."""
    return Path(root) / ".claude" / "settings.json"


def _desired_hooks(level: str) -> dict:
    """The Bounds-owned hook entries for ``level`` (empty for ``off``)."""
    if level not in ("nudge", "strict"):
        return {}
    prompt_entry = {"hooks": [{"type": "command", "command": HOOK_COMMAND}]}
    desired: dict = {"UserPromptSubmit": [prompt_entry]}
    if level == "strict":
        desired["PreToolUse"] = [
            {"matcher": _STRICT_MATCHER, "hooks": [{"type": "command", "command": HOOK_COMMAND}]}
        ]
    return desired


def _is_bounds_entry(entry: object) -> bool:
    """True when a settings hook entry is one Bounds wrote (its command carries the signature)."""
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(
        isinstance(h, dict) and HOOK_SIGNATURE in str(h.get("command") or "") for h in hooks
    )


def merge_settings_hooks(settings: dict, level: str) -> tuple[dict, bool]:
    """Return ``(new_settings, changed)`` with Bounds-owned hooks set to ``level``'s desired state.

    Pure and non-destructive: every hook entry the user authored is preserved; only entries whose
    command carries :data:`HOOK_SIGNATURE` are removed and re-added per ``level``. ``off`` removes
    them entirely. Empty event lists and an emptied ``hooks`` object are pruned so the file stays
    tidy. Foreign top-level keys are untouched.
    """
    original = settings if isinstance(settings, dict) else {}
    result = copy.deepcopy(original)
    hooks = result.get("hooks")
    hooks = copy.deepcopy(hooks) if isinstance(hooks, dict) else {}
    desired = _desired_hooks(level)

    for event in ("UserPromptSubmit", "PreToolUse"):
        existing = hooks.get(event)
        kept = [e for e in existing if not _is_bounds_entry(e)] if isinstance(existing, list) else []
        kept.extend(desired.get(event, []))
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)

    if hooks:
        result["hooks"] = hooks
    else:
        result.pop("hooks", None)
    return result, result != original


def sync_claude_hooks(root: Path, level: str) -> str:
    """Apply :func:`merge_settings_hooks` to ``.claude/settings.json``; return an outcome string.

    Outcomes mirror the agentsync block writer: ``created`` / ``updated`` / ``unchanged`` (the file
    already held exactly the desired Bounds hooks, or ``off`` with nothing to remove), and
    ``skipped_malformed`` when the existing file isn't valid JSON (never clobber a user's settings —
    report it and leave it). Foreign hooks/keys always survive.
    """
    path = settings_path(root)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "skipped_malformed"
        if not isinstance(data, dict):
            return "skipped_malformed"
        existed = True
    else:
        data = {}
        existed = False

    new, changed = merge_settings_hooks(data, level)
    if not changed:
        return "unchanged"
    if not existed and level == "off":
        return "unchanged"  # nothing to write and nothing to remove.
    _write_json(path, new)
    return "updated" if existed else "created"


def _write_json(path: Path, data: dict) -> None:
    """Write pretty JSON (2-space, trailing newline), preserving key order, creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def claude_hooks_current(root: Path, level: str) -> bool:
    """True when ``.claude/settings.json`` already holds exactly the Bounds hooks ``level`` wants.

    The verification counterpart of :func:`sync_claude_hooks`, so ``bounds agent --check`` (and CI)
    can catch a hook that was deleted, never written, or left stale after a level change — at
    nudge/strict the hook is part of Claude's wiring, so its absence is real drift. Defined as "a
    sync would be a no-op": current iff merging the desired state produces no change. A malformed
    settings.json reads as not-current (the gate must surface it, not silently pass). At ``off`` with
    no Bounds hook present this is trivially true (nothing is owed).
    """
    path = settings_path(root)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False  # malformed → can't confirm the hook; surface it as drift.
        if not isinstance(data, dict):
            return False
    else:
        data = {}
    _new, changed = merge_settings_hooks(data, level)
    return not changed
