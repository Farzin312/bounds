"""Cross-agent protocol (s-18): teach coding agents to read architecture via the CLI.

The problem this solves: AI coding agents (Claude Code, Cursor, Copilot, Gemini, …) each
have their own out-of-band convention for project instructions, and when left to their own
devices they read the raw `.bounds/` files — the binary cache and the unverified YAML
manifests — which both defeats tree-sitter verification *and* burns context on data the CLI
already compresses.

This module writes a single canonical contract in ``AGENTS.md`` — the cross-ecosystem
standard file that agents already read — plus a small per-agent *pointer* file in each
agent's native config location, all conveying the same five essentials: use ``bounds list`` /
``bounds describe`` / ``bounds validate --quick``, never read the raw files, and treat the CLI
as the API. ``AGENTS.md`` is the source of truth; every per-tool pointer references it, so
there is exactly one place the contract lives.

Design constraints (enforced repo-wide):
  * Pure stdlib, no new dependencies.
  * Deterministic output: every returned path list is sorted and POSIX-relative; file bodies
    contain no timestamps, randomness, or wall-clock — re-running ``sync`` is idempotent.
  * Fail soft: the only fatal condition is genuine misuse (bad ``mode`` / bad agent key),
    which raises ``BoundsError(E_USAGE)``. A pre-existing hand-written config is never
    clobbered; it is reported and left untouched.

The three modes:
  * ``sync``   — write ``AGENTS.md`` and the selected agents' pointer files; return what changed.
  * ``detect`` — heuristically report which agents' tool footprints are present.
  * ``check``  — for detected agents, report whether each has an up-to-date bounds config.
"""

from __future__ import annotations

from pathlib import Path

from . import errors

# ---------------------------------------------------------------------------
# Canonical contract content (committed in AGENTS.md; the single source of truth)
# ---------------------------------------------------------------------------
CANONICAL_NAME = "AGENTS.md"

# The full contract. Lives inside AGENTS.md's marked block so it coexists with any other
# AGENTS.md content a project already keeps.
CANONICAL_BODY = """\
## Bounds — architecture contract for agents

Bounds models this codebase as subsystem boundary manifests. Query them through the CLI — never read the raw files.

### Commands
- `bounds list` — all subsystems (the map; roles + dependency counts)
- `bounds describe <name>` — one subsystem's public surface (~300 tokens, tree-sitter-verified)
- `bounds describe --namespace <ns>` — every subsystem in a namespace
- `bounds validate --quick` — catch drift after a change
- `bounds impact <name>` — transitive blast radius before a risky change

### Hard rules
- NEVER read `.bounds/cache.db`, `.bounds/*.json`, `.bounds/manifests/*.yaml`, or `.bounds/root.yaml` directly. The cache is binary; the manifests bypass tree-sitter verification.
- The CLI is the API. Always use `bounds` commands to read architecture.
"""

# The token-lean body shared by every per-agent pointer file. Kept short on purpose:
# its only job is to redirect the agent to AGENTS.md and the five essentials.
_AGENT_POINTER_BODY = """\
This project uses **Bounds** to model its architecture as subsystem boundary manifests.
Read the architecture through the Bounds CLI, never by opening the raw files.

- `bounds list` — map of all subsystems (roles + dependency counts)
- `bounds describe <name>` — one subsystem's verified public surface (~300 tokens)
- `bounds validate --quick` — catch structural drift after a change

**Never** read `.bounds/cache.db`, `.bounds/*.json`, or `.bounds/manifests/*.yaml` directly —
the cache is binary and the manifests bypass tree-sitter verification. The CLI is the API.
See `AGENTS.md` for the full contract.
"""

# Marker delimiters for shared files (other content may live around our block).
_MD_START = "<!-- BOUNDS:START -->"
_MD_END = "<!-- BOUNDS:END -->"
_YAML_START = "# BOUNDS:START"
_YAML_END = "# BOUNDS:END"

_MARKDOWN = "markdown"
_YAML = "yaml"

VALID_MODES = {"sync", "detect", "check"}

# Stable, documented ordering of the eight supported agents.
AGENT_KEYS = (
    "claude",
    "codex",
    "opencode",
    "gemini",
    "copilot",
    "cursor",
    "aider",
    "windsurf",
)


class _Agent:
    """Static description of one agent's bounds-config target.

    Attributes:
        key: The agent identifier used in ``only`` / reports.
        path: Config file path, relative to the project root (POSIX components).
        dedicated: True when the whole file is ours (overwrite wholesale); False when the
            file is shared and we must edit only the marked block.
        fmt: ``markdown`` or ``yaml`` — selects the marker/comment style for shared files.
        canonical: True for the agents whose native file *is* ``AGENTS.md`` (codex, opencode);
            ``AGENTS.md`` is written once as the canonical contract, so these are no-ops in the
            per-agent loop and report against ``AGENTS.md``.
    """

    __slots__ = ("key", "path", "dedicated", "fmt", "canonical")

    def __init__(self, key, path, dedicated, fmt, canonical=False):
        self.key = key
        self.path = path
        self.dedicated = dedicated
        self.fmt = fmt
        self.canonical = canonical


_AGENTS = {
    "claude": _Agent("claude", ".claude/commands/bounds.md", True, _MARKDOWN),
    # codex + opencode read AGENTS.md natively — that IS the canonical contract.
    "codex": _Agent("codex", CANONICAL_NAME, False, _MARKDOWN, canonical=True),
    "opencode": _Agent("opencode", CANONICAL_NAME, False, _MARKDOWN, canonical=True),
    "gemini": _Agent("gemini", "GEMINI.md", False, _MARKDOWN),
    "copilot": _Agent("copilot", ".github/copilot-instructions.md", False, _MARKDOWN),
    "cursor": _Agent("cursor", ".cursor/rules/bounds.mdc", True, _MARKDOWN),
    "aider": _Agent("aider", ".aider.conf.yml", False, _YAML),
    "windsurf": _Agent("windsurf", ".windsurf/rules/bounds.md", True, _MARKDOWN),
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def run_agent(project_root: Path, *, mode: str, only: set[str] | None = None) -> dict:
    """Run the cross-agent protocol and return a JSON-serializable payload.

    Args:
        project_root: Repository root. Config files are written relative to it.
        mode: One of ``"sync"``, ``"detect"``, ``"check"``.
        only: Optional set of agent keys (from :data:`AGENT_KEYS`) to restrict the operation
            to. ``None`` means all eight agents. ``AGENTS.md`` (the canonical contract) is
            always written on ``sync`` regardless of selection, since every pointer references it.

    Returns:
        A dict whose shape depends on ``mode``. All path lists are sorted and POSIX-relative.

    Raises:
        BoundsError: ``E_USAGE`` for an unknown ``mode`` or an unknown agent key in ``only``.
    """
    root = Path(project_root)
    if mode not in VALID_MODES:
        raise errors.BoundsError(
            errors.E_USAGE,
            f"unknown agent mode {mode!r}",
            fix=f"use one of: {', '.join(sorted(VALID_MODES))}",
        )
    selected = _resolve_selection(only)

    if mode == "detect":
        return _detect(root)
    if mode == "check":
        return _check(root, selected)
    return _sync(root, selected)


# ---------------------------------------------------------------------------
# Selection helpers
# ---------------------------------------------------------------------------
def _resolve_selection(only: set[str] | None) -> list[str]:
    """Validate ``only`` and return the agent keys to operate on, in stable order.

    Raises ``BoundsError(E_USAGE)`` for any unknown key so a typo fails loudly rather than
    silently writing nothing.
    """
    if only is None:
        return list(AGENT_KEYS)
    unknown = sorted(only - set(AGENT_KEYS))
    if unknown:
        raise errors.BoundsError(
            errors.E_USAGE,
            f"unknown agent key(s): {', '.join(unknown)}",
            fix=f"valid keys: {', '.join(AGENT_KEYS)}",
        )
    return [k for k in AGENT_KEYS if k in only]


# ---------------------------------------------------------------------------
# mode = sync
# ---------------------------------------------------------------------------
def _sync(root: Path, selected: list[str]) -> dict:
    """Write the canonical ``AGENTS.md`` and the selected agents' pointer files.

    ``AGENTS.md`` is always (re)written first — it is the source of truth every pointer
    references — then each selected agent that has its *own* file gets a short pointer.
    Codex/OpenCode are satisfied by ``AGENTS.md`` itself, so selecting them is a no-op beyond
    the canonical write.

    Returns sorted, POSIX-relative ``created`` / ``updated`` / ``skipped_custom`` path lists
    plus ``canonical`` (always ``"AGENTS.md"``). ``skipped_custom`` holds shared files that
    already carried hand-written bounds content with no markers — never clobbered.
    """
    created: set[str] = set()
    updated: set[str] = set()
    skipped: set[str] = set()

    # 1. Canonical AGENTS.md — always written, as a marked block so any other AGENTS.md
    #    content the project keeps is preserved.
    canonical_path = root / CANONICAL_NAME
    rel_canonical = CANONICAL_NAME
    outcome = _upsert_shared_block(canonical_path, _MARKDOWN, CANONICAL_BODY)
    _record(outcome, rel_canonical, created, updated, skipped)

    # 2. Per-tool pointer files for the selected agents (AGENTS.md already handled above).
    done: set[str] = {CANONICAL_NAME}
    for key in selected:
        agent = _AGENTS[key]
        if agent.canonical or agent.path in done:
            continue  # covered by the canonical AGENTS.md write
        done.add(agent.path)
        outcome = _write_agent_file(root / Path(agent.path), agent)
        _record(outcome, Path(agent.path).as_posix(), created, updated, skipped)

    return {
        "created": sorted(created),
        "updated": sorted(updated),
        "skipped_custom": sorted(skipped),
        "canonical": rel_canonical,
    }


def _record(outcome: str, rel: str, created: set, updated: set, skipped: set) -> None:
    """Bucket a write outcome into the right report set (``unchanged`` reports nothing)."""
    if outcome == "created":
        created.add(rel)
    elif outcome == "updated":
        updated.add(rel)
    elif outcome == "skipped_custom":
        skipped.add(rel)


def _write_agent_file(target: Path, agent: "_Agent") -> str:
    """Write one agent's pointer file; return created/updated/unchanged/skipped_custom.

    Dedicated files (``.claude/commands/bounds.md`` etc.) are ours, so we overwrite wholesale
    (only touching disk when content differs). Shared files insert or replace a marked pointer
    block, preserving everything around it; a shared file that already mentions bounds but has
    *no* markers is treated as hand-written and left untouched (``skipped_custom``).
    """
    if agent.dedicated:
        body = _dedicated_body(agent)
        if target.exists() and _read_text(target) == body:
            return "unchanged"
        existed = target.exists()
        _write_text(target, body)
        return "updated" if existed else "created"

    return _upsert_shared_block(target, agent.fmt, _pointer_block_body(agent.fmt))


def _dedicated_body(agent: "_Agent") -> str:
    """Build the full body for a dedicated (bounds-only) pointer file.

    Each tool needs its own front-matter for the rule to actually *activate*, not sit dormant:
      * Claude — front-matter ``description`` surfaces it as a usable ``/bounds`` slash command.
      * Cursor — a ``.mdc`` with no front-matter is a "Manual" rule (never auto-applied);
        ``alwaysApply: true`` makes it an always-on rule.
      * Windsurf — a rule needs ``trigger: always_on`` to apply universally.
    All share the same short pointer body so the five essentials stay consistent (and stay well
    under Cursor/Windsurf per-rule character limits).
    """
    _desc = "Read this project's architecture via the Bounds CLI, not the raw files"
    headers = {
        "claude": f"---\ndescription: {_desc}\n---\n\n# Bounds\n\n",
        "cursor": f"---\ndescription: {_desc}\nalwaysApply: true\n---\n\n# Bounds\n\n",
        "windsurf": f"---\ntrigger: always_on\ndescription: {_desc}\n---\n\n# Bounds\n\n",
    }
    return headers.get(agent.key, "# Bounds\n\n") + _AGENT_POINTER_BODY


def _pointer_block_body(fmt: str) -> str:
    """The inner body (no markers) for a shared *pointer* file in the requested format."""
    if fmt == _YAML:
        return "\n".join(
            [
                "# Bounds models this codebase as subsystem boundary manifests.",
                "# Read architecture via the CLI, never the raw files:",
                "#   bounds list / bounds describe <name> / bounds validate --quick",
                "# Never read .bounds/cache.db, .bounds/*.json, or .bounds/manifests/*.yaml.",
                "# The CLI is the API. See AGENTS.md for the full contract.",
                "read: [AGENTS.md]",
            ]
        )
    return "## Bounds\n\n" + _AGENT_POINTER_BODY.rstrip("\n")


def _upsert_shared_block(target: Path, fmt: str, body: str) -> str:
    """Insert or replace our marked block in a shared file, preserving other content.

    ``body`` is the inner content (no markers); this wraps it in the format's markers.

    Returns:
        ``created``       — file did not exist; created with just our block.
        ``updated``       — our marked block was inserted into / refreshed in an existing file.
        ``unchanged``     — file already contained an identical marked block.
        ``skipped_custom``— file exists, mentions bounds, but has no markers (hand-written).
    """
    start, end = (_YAML_START, _YAML_END) if fmt == _YAML else (_MD_START, _MD_END)
    block = f"{start}\n{body.rstrip(chr(10))}\n{end}"

    if not target.exists():
        _write_text(target, block + "\n")
        return "created"

    existing = _read_text(target)

    if start in existing and end in existing:
        new = _replace_block(existing, start, end, block)
        if new == existing:
            return "unchanged"
        _write_text(target, new)
        return "updated"

    # No markers. If the file already talks about bounds, a human wrote it — don't clobber.
    if _looks_bounds_authored(existing):
        return "skipped_custom"

    # Unrelated content with no markers: append our block, preserving theirs.
    sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    _write_text(target, existing + sep + block + "\n")
    return "updated"


def _replace_block(text: str, start: str, end: str, block: str) -> str:
    """Replace the first ``start``…``end`` marked region (inclusive) with ``block``.

    Operates on the raw text so surrounding content — before the start marker and after the
    end marker — is preserved byte-for-byte.
    """
    i = text.index(start)
    j = text.index(end, i) + len(end)
    return text[:i] + block + text[j:]


def _looks_bounds_authored(text: str) -> bool:
    """Heuristic: does an unmarked shared file already contain hand-written bounds content?

    Used only to decide ``skipped_custom``. We look for clear signals that a human wrote
    bounds guidance (the CLI name as a command, or a heading naming Bounds) so we err toward
    *not* clobbering. Plain unrelated content (no bounds mention) is safe to append to.
    """
    lowered = text.lower()
    return "bounds " in lowered or "`bounds`" in lowered or "bounds list" in lowered


# ---------------------------------------------------------------------------
# mode = detect
# ---------------------------------------------------------------------------
def _detect(root: Path) -> dict:
    """Heuristically report which agents' tool footprints are present in ``root``.

    Heuristic (presence of the agent's native footprint, independent of whether bounds has
    been synced yet):
      * claude   — ``.claude/`` directory exists.
      * codex    — ``AGENTS.md`` exists (Codex's instruction file).
      * opencode — ``AGENTS.md`` exists (OpenCode shares the same file).
      * gemini   — ``GEMINI.md`` exists.
      * copilot  — ``.github/copilot-instructions.md`` exists (its actual instruction file — a
        bare ``.github/`` directory is universal and not a Copilot signal).
      * cursor   — ``.cursor/`` directory exists.
      * aider    — ``.aider.conf.yml`` exists.
      * windsurf — ``.windsurf/`` directory exists.

    Returns ``{"detected": [sorted agent keys]}``.
    """
    detected = [k for k in AGENT_KEYS if _footprint_present(root, k)]
    return {"detected": sorted(detected)}


def _footprint_present(root: Path, key: str) -> bool:
    """True when ``key``'s native tool footprint exists under ``root`` (see :func:`_detect`)."""
    checks = {
        "claude": (root / ".claude").is_dir,
        "codex": (root / CANONICAL_NAME).is_file,
        "opencode": (root / CANONICAL_NAME).is_file,
        "gemini": (root / "GEMINI.md").is_file,
        "copilot": (root / ".github" / "copilot-instructions.md").is_file,
        "cursor": (root / ".cursor").is_dir,
        "aider": (root / ".aider.conf.yml").is_file,
        "windsurf": (root / ".windsurf").is_dir,
    }
    return checks[key]()


# ---------------------------------------------------------------------------
# mode = check
# ---------------------------------------------------------------------------
def _check(root: Path, selected: list[str]) -> dict:
    """Report, for each detected-and-selected agent, whether its bounds config is in place.

    "Configured" means: the agent's config file exists and either is our dedicated file or
    contains our marker block (shared files). We intersect with detection so a check reports
    only on agents actually in use — selecting all eight on a Claude-only repo won't flag
    cursor/gemini/etc. as missing.

    Returns ``{"ok": bool, "missing": [...], "configured": [...]}`` with sorted key lists;
    ``ok`` is True iff nothing is missing.
    """
    detected = set(_detect(root)["detected"])
    targets = [k for k in selected if k in detected]

    configured: list[str] = []
    missing: list[str] = []
    for key in targets:
        if _is_configured(root, _AGENTS[key]):
            configured.append(key)
        else:
            missing.append(key)

    return {
        "ok": not missing,
        "missing": sorted(missing),
        "configured": sorted(configured),
    }


def _is_configured(root: Path, agent: "_Agent") -> bool:
    """True when ``agent``'s bounds config exists and carries our content/marker."""
    target = root / Path(agent.path)
    if not target.exists():
        return False
    if agent.dedicated:
        return True  # dedicated files are ours; existence == configured.
    start = _YAML_START if agent.fmt == _YAML else _MD_START
    end = _YAML_END if agent.fmt == _YAML else _MD_END
    text = _read_text(target)
    return start in text and end in text


# ---------------------------------------------------------------------------
# Filesystem helpers (centralized so encoding/newlines stay consistent)
# ---------------------------------------------------------------------------
def _read_text(path: Path) -> str:
    """Read a file as UTF-8 text (no surprises across platforms)."""
    return path.read_text(encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    """Write UTF-8 text, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
