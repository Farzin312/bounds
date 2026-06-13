"""Global constants, defaults, and config-directory resolution.

Imports nothing from other bounds modules (only the stdlib), so every module can depend
on it without risking an import cycle.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# ---- Filesystem layout ----
BOUNDS_DIR = ".bounds"
LEGACY_DIR = ".compact"  # pre-rename layout; still read for backward compatibility
ROOT_FILE = "root.yaml"
SUBSYS_DIR = "subsystems"
SUBSYS_FILE = "bounds.yaml"
MANIFESTS_DIR = "manifests"
CACHE_FILE = "cache.db"  # binary SQLite extraction cache (context armor)
STATE_FILE = "state.json"  # legacy JSON cache; read once for auto-migration to cache.db
GITIGNORE_FILE = ".gitignore"  # written under .bounds/ to keep the regenerable cache uncommitted

# The .bounds/.gitignore body. Lists only the REGENERABLE, binary, context-armor cache artifacts —
# never the manifests or root.yaml, which are human-authored and MUST stay committed. Kept here as
# the single template both `init --root` and `discover --apply` write, so they can't drift.
GITIGNORE_TEMPLATE = """# Bounds cache artifacts — regenerable, binary (context armor), machine-only.
# Do NOT commit these; manifests and root.yaml ARE committed.
cache.db
cache.db-journal
state.json
"""
# The required ignore entries, derived from GITIGNORE_TEMPLATE so the append path (below) and the
# template can't drift: any non-blank, non-comment line in the template is a required entry. When an
# existing .bounds/.gitignore is found, only these that aren't already present as their own line are
# appended — existing content is never reordered or rewritten.
GITIGNORE_ENTRIES = tuple(
    line.strip()
    for line in GITIGNORE_TEMPLATE.splitlines()
    if line.strip() and not line.lstrip().startswith("#")
)
# Section comment placed above appended entries (deterministic, no wall-clock) — only when at
# least one entry is actually being added to an existing file.
GITIGNORE_APPEND_HEADER = "# Added by Bounds — regenerable cache artifacts"
# Committed drift baseline for `calibrate --check`: the set of manifest-vs-source
# discrepancies that already exist on the main branch. The check flags only NEW drift
# above this baseline, so a PR is never blocked by pre-existing rot it didn't introduce.
DRIFT_BASELINE_FILE = "drift-baseline.json"

# Committed snapshot of each subsystem's UNSUPPORTED-language source (per-file content hash), written
# by `calibrate --dump-baseline`. `validate` compares the live files against it and emits
# E_UNSUPPORTED_SURFACE_STALE when one changed since the hand-authored exposes were confirmed.
# Committed (unlike the gitignored cache.db) so the signal survives a fresh clone and works in CI.
SURFACE_BASELINE_FILE = "surface-baseline.json"

# Committed baseline of accepted subsystem-level dependency cycles, written by
# `calibrate --dump-baseline`. `validate`/`preflight` suppress these known cycles so the gate
# fails only on NEW cycles a branch introduces — parity with the drift baseline, but for
# E_CYCLE_DETECTED. Each entry is a canonical (min-rotated) tab-joined cycle key.
CYCLE_BASELINE_FILE = "cycle-baseline.json"

# Optional committed governance policy (`.bounds/policy.yaml`): per-code severity overrides,
# a hard `fail_on` code list, and per-finding suppressions with a justification. Lets a repo
# hard-gate the findings it can fix while demoting known library-gap classes, instead of
# disabling the whole gate. Human-readable YAML (like manifests); absent ⇒ no-op.
POLICY_FILE = "policy.yaml"

# A subsystem name is a single path segment used to build `<MANIFESTS_DIR>/<name>.yaml`. Only
# letters, digits, '-' and '_' are allowed so a name can never traverse out of the manifests dir
# (e.g. `../../tmp/x`) or carry a path separator. The single source both cli.init and the loader
# validate against, so a write path and a read path can never disagree on what's safe.
SUBSYSTEM_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def is_valid_subsystem_name(name: str) -> bool:
    """True when ``name`` is a safe single-segment subsystem name (no traversal/separators)."""
    return bool(SUBSYSTEM_NAME_RE.match(name))


# ---- Schema / versioning ----
SCHEMA_VERSION = "1"
# Bump whenever extraction OUTPUT changes for unchanged source (new/changed adapter symbols),
# so every existing binary cache.db is treated as version-mismatched and rebuilt rather than
# serving stale symbols. v2: SQL adapter recovers transaction-wrapped DDL + folds policies/RLS
# and canonicalises table names (see extract/sql.py, validate/schema.py). v3: PythonAdapter honours
# a literal `__all__` for the export surface and resolves same-file Django-model inheritance
# transitively (see extract/python.py). v4: cache records persist the per-file generated-code flag
# so validate can skip generated exports without rereading source on the quick path. v5: shell (bash)
# adapter added — `.sh`/`.bash`/`.zsh` now extract function symbols instead of being skipped, so a
# cache built before it must rebuild to pick up the new surface (see extract/shell.py).
STATE_VERSION = "5"

# The documented command to refresh a stale git/pipx install. Surfaced by `upgrade-check`
# and embedded in the generated agent contract; kept here as the single source so the two
# can never drift.
UPGRADE_INSTALL_CMD = "pipx install --force git+https://github.com/Farzin312/bounds.git"

# Optional Spec-Driven Development integration. These are guidance/config labels only; Bounds
# still provides deterministic structural facts and gates, while the user's agent runs any prose
# SDD workflow. Keep the phases in canonical order for stable guide/agent rendering.
SDD_PHASES = ("specify", "clarify", "plan", "tasks", "analyze", "implement", "verify")
SDD_AGENTS = (
    "claude",
    "codex",
    "gemini",
    "opencode",
    "copilot",
    "cursor",
    "windsurf",
    "aider",
    "generic",
)

__all__ = [
    "BOUNDS_DIR",
    "BUILTIN_CRITICALITY",
    "BUILTIN_CRITICALITY_DEPTH",
    "BUILTIN_ROLES",
    "CACHE_FILE",
    "CONFIG_FILE_STEMS",
    "CYCLE_BASELINE_FILE",
    "DEFAULT_IGNORES",
    "DEFAULT_INVOCATION",
    "DOC_EXTS",
    "DRIFT_BASELINE_FILE",
    "EXIT_BLOCKED",
    "EXIT_FATAL",
    "EXIT_OK",
    "KNOWN_SOURCE_EXTS",
    "LEGACY_DIR",
    "MANIFESTS_DIR",
    "MAX_FILE_BYTES",
    "POLICY_FILE",
    "PROPAGATION_DEPTH",
    "ROLE_BASE_BEHAVIOR",
    "ROOT_FILE",
    "SCHEMA_VERSION",
    "SDD_AGENTS",
    "SDD_PHASES",
    "STATE_FILE",
    "STATE_VERSION",
    "SUBSYS_DIR",
    "SUBSYS_FILE",
    "SURFACE_BASELINE_FILE",
    "UPGRADE_INSTALL_CMD",
    "VALID_CRITICALITY",
    "VALID_ENFORCE",
    "VALID_INVOCATION",
    "VALID_MODES",
    "VALID_ROLES",
    "config_dir",
    "uses_legacy_layout",
]

# ---- Enumerations ----
# The four built-in roles are *behavior classes*, not just labels. Developers may define
# custom role names in root.yaml that `extends`/`type` one of these bases; when no
# custom roles are declared, these built-ins are the valid set (backward compatible).
BUILTIN_ROLES = {"service", "platform", "connector", "library"}
BUILTIN_CRITICALITY = {"core", "connector", "leaf"}
# Back-compat aliases (older code/tests referenced these names).
VALID_ROLES = BUILTIN_ROLES
VALID_CRITICALITY = BUILTIN_CRITICALITY
VALID_MODES = {"quick", "full", "preflight", "hotfix", "audit"}
# enforce tri-state: "on" blocks on structural errors, "off" runs purely advisory, and
# "warn" reports every issue (so CI surfaces drift) but never blocks the pipeline — the
# middle ground that lets a team see structural signal before committing to a hard gate.
VALID_ENFORCE = {"on", "off", "warn"}

# Agent-invocation enforcement (root.yaml `agentsync.invocation`): how hard the generated agent
# wiring pushes a coding agent to query Bounds *before* grepping. Distinct from `enforce`, which
# gates *validation* — this gates *invocation*.
#   "off"    -- advisory files only (the pre-feature behavior): AGENTS.md/skills/rules, no hook.
#   "nudge"  -- also write a harness hook (Claude `UserPromptSubmit`) that injects a one-line
#               reminder on architecture-shaped prompts. Never blocks a tool.
#   "strict" -- a `PreToolUse` hook that pauses (asks, never denies) before a broad source search
#               that Bounds could answer; fails open the instant Bounds can't answer.
# Default is "nudge": a project that never configured this still gets the gentle reminder rather
# than silently failing to adopt Bounds (the off-switch is surfaced in every reminder). The hook
# fails open at every layer, so even "strict" can never trap an agent into a dead end.
VALID_INVOCATION = {"off", "nudge", "strict"}
DEFAULT_INVOCATION = "nudge"


def normalize_invocation(value) -> str | None:
    """Map a raw root.yaml ``agentsync.invocation`` value to a valid level, or None if unrecognized.

    Hardens against YAML's unquoted-reserved-word trap: a user who writes ``invocation: off`` (no
    quotes) has it parsed as the boolean ``False``, not the string ``"off"`` — without this, the
    level would silently fall back to the default and the user would never know why "off" didn't
    take. ``False`` → ``"off"`` (their clear intent); ``True`` (``on``) is not a level → None. The
    single seam both the resolver (models.RootManifest.invocation_level) and the schema validator
    use, so they can never disagree about what a raw value means.
    """
    if value is False:
        return "off"
    if value is True:
        return None
    s = str(value or "").strip().lower()
    return s if s in VALID_INVOCATION else None

# Behavior each base role encodes (what the structural checks key off, never the label):
#   orphan_exposes -- True if exposes may legitimately have zero consumers (entrypoints).
# Custom roles inherit their base's behavior unless they override a flag explicitly.
ROLE_BASE_BEHAVIOR = {
    "service": {"orphan_exposes": True},
    "platform": {"orphan_exposes": False},
    "connector": {"orphan_exposes": False},
    "library": {"orphan_exposes": False},
}

# ---- Extraction ----
# Hard ceiling on a single source file's size (bytes). A file larger than this is skipped with
# an E_EXTRACTION_FAILED warning rather than read into memory (resource bound) — guards
# against a giant minified/generated blob or a runaway file exhausting memory. 1 MB is far above
# any hand-written source file; legitimately-huge generated files should be gitignored anyway.
MAX_FILE_BYTES = 1_000_000

# Directories never descended into when globbing subsystem files. Kept here so `discover`
# never mines build/generated output into garbage manifests. Grouped by ecosystem for
# readability; this is a set, so iteration order is irrelevant to determinism (callers sort
# at serialization boundaries).
DEFAULT_IGNORES = {
    # Dependencies & VCS
    "node_modules",
    "vendor",  # Go vendored deps (also PHP Composer)
    ".git",
    # Generic build output
    "dist",
    "build",
    "out",
    "target",  # Rust/Cargo, Maven/Gradle JVM output
    ".gradle",  # Java/Gradle build cache
    "coverage",
    # JS/TS framework build & tooling dirs
    ".next",
    ".turbo",
    ".svelte-kit",
    ".vercel",
    ".cache",
    # JS/TS test/build artifact conventions
    "__snapshots__",  # Jest/Vitest snapshot dirs
    ".parcel-cache",
    ".eslintcache",
    # Python
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    # iOS / Apple
    "Pods",  # CocoaPods
    "DerivedData",
    # Misc build / tool caches
    ".terraform",  # Terraform state/providers
    "obj",
    # Bounds itself
    ".bounds",
    ".compact",  # legacy config dir
}

# Build/tool configuration stems whose final suffix is a known source extension.
# ``is_config_file`` removes the final suffix before matching, so
# ``vite.config.ts`` becomes ``vite.config`` and ``setup.py`` becomes ``setup``.
# Non-source files (for example ``pyproject.toml``) do not belong here because
# they never enter mapping coverage's source denominator.
CONFIG_FILE_STEMS = frozenset({
    "vite.config",
    "next.config",
    "nuxt.config",
    "remix.config",
    "astro.config",
    "svelte.config",
    "rollup.config",
    "webpack.config",
    "parcel.config",
    "esbuild.config",
    "tsup.config",
    "vitest.config",
    "jest.config",
    "playwright.config",
    "cypress.config",
    "tailwind.config",
    "postcss.config",
    "babel.config",
    ".babelrc",
    "prettier.config",
    "eslint.config",
    ".eslintrc",
    "lint-staged.config",
    "commitlint.config",
    "stylelint.config",
    # Python executable configuration modules
    "setup",
    "noxfile",
    "conftest",
})

# Extension -> language label for *known source code*, language-agnostic. The denominator for
# mapping-coverage: how much of a repo's source Bounds actually mapped, and an honest, by-language
# breakdown of what it could not (so a polyglot repo can't look "fully mapped" while half of it is an
# unsupported language). The labels Bounds has an adapter for (python/typescript/javascript/sql/
# prisma) are *mappable*; the rest are *unsupported today* and surface as a loud coverage gap. Only
# code extensions belong here — docs/config/assets are deliberately excluded so they don't dilute %.
KNOWN_SOURCE_EXTS = {
    # .pyi (stub files) is deliberately excluded: it has no symbol surface to map (type-only
    # declarations, no implementation), and PythonAdapter has no .pyi adapter — counting it would
    # make stubs read as mappable-but-unmapped and understate coverage.
    ".py": "python",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".sql": "sql", ".prisma": "prisma",
    # Unsupported today (no adapter) — counted so coverage is honest, not silently dropped.
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".rb": "ruby", ".php": "php", ".cs": "c#", ".swift": "swift", ".scala": "scala",
    ".c": "c", ".h": "c", ".cc": "c++", ".cpp": "c++", ".cxx": "c++", ".hpp": "c++", ".hh": "c++",
    ".m": "objective-c", ".mm": "objective-c++", ".dart": "dart", ".lua": "lua", ".ex": "elixir",
    ".exs": "elixir", ".erl": "erlang", ".clj": "clojure", ".hs": "haskell", ".ml": "ocaml",
    ".r": "r", ".jl": "julia", ".groovy": "groovy", ".vue": "vue", ".svelte": "svelte",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".ps1": "powershell",
}

# Documentation file extensions. Used by mapping-coverage to track which docs a subsystem links
# (informational only — docs are NEVER counted in the source denominator and never a coverage gap;
# they are deliberately excluded from KNOWN_SOURCE_EXTS so they can't dilute the mapped %).
DOC_EXTS = {".md", ".mdx", ".rst"}

# ---- Propagation ----
# Depth of consumer propagation, keyed by the *changed provider's* criticality.
#   -1 = unbounded (transitive closure), 0 = none, N = N hops.
# These are the built-in depths; custom criticality labels declare their own
# `depth:` in root.yaml. Used as the fallback when no custom criticality is defined.
PROPAGATION_DEPTH = {"core": -1, "connector": 1, "leaf": 0}
BUILTIN_CRITICALITY_DEPTH = PROPAGATION_DEPTH

# ---- Exit codes ----
EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_FATAL = 2


# ---- Config-directory resolution ----
# Guard so the legacy-layout deprecation notice is emitted at most once per process
# (config_dir is called many times per command; one warning is enough).
_legacy_warning_emitted = False


def _warn_legacy_layout() -> None:
    """Emit a one-time deprecation notice to stderr when a legacy ``.compact/`` is used."""
    global _legacy_warning_emitted
    if _legacy_warning_emitted:
        return
    _legacy_warning_emitted = True
    print(
        f"warning: the '{LEGACY_DIR}/' directory is deprecated; rename it to '{BOUNDS_DIR}/'. "
        "Backward-compatible support will be removed in a future release.",
        file=sys.stderr,
    )


def config_dir(project_root) -> Path:
    """Return the active config directory under ``project_root``.

    Prefers the canonical ``.bounds/``; falls back to the legacy ``.compact/`` only when
    ``.bounds/`` is absent and ``.compact/`` exists (backward compatibility), emitting a
    one-time deprecation notice to stderr in that case. When neither exists, returns the
    ``.bounds/`` path so fresh scaffolding creates the new layout.
    """
    root = Path(project_root)
    bounds = root / BOUNDS_DIR
    if bounds.is_dir():
        return bounds
    legacy = root / LEGACY_DIR
    if legacy.is_dir():
        _warn_legacy_layout()
        return legacy
    return bounds


def uses_legacy_layout(project_root) -> bool:
    """True when ``project_root`` is being read from the legacy ``.compact/`` directory."""
    root = Path(project_root)
    return not (root / BOUNDS_DIR).is_dir() and (root / LEGACY_DIR).is_dir()


def ensure_bounds_gitignore(bounds_dir) -> bool:
    """Idempotently ensure ``<bounds_dir>/.gitignore`` ignores the regenerable-cache entries.

    Never destroys or rewrites existing content. If the file is absent it is created from
    ``GITIGNORE_TEMPLATE``; if it exists, every existing line is preserved byte-for-byte and only
    the ``GITIGNORE_ENTRIES`` that aren't already present as their own line (whole-line match,
    ignoring surrounding whitespace; a commented ``# cache.db`` does NOT count) are appended under
    a single section comment. Returns ``True`` if the file was created or anything was appended,
    ``False`` if no change was needed.

    The single seam both ``init --root`` and ``discover --apply`` use so the .bounds/ cache
    (binary, regenerable) is never accidentally committed and the two paths can't diverge.
    """
    path = Path(bounds_dir) / GITIGNORE_FILE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(GITIGNORE_TEMPLATE, encoding="utf-8")
        return True

    existing = path.read_text(encoding="utf-8")
    # Whole-line presence check: a line is "present" only as its own uncommented entry, so a
    # commented `# cache.db` does not satisfy the requirement.
    present = {line.strip() for line in existing.splitlines()}
    missing = [entry for entry in GITIGNORE_ENTRIES if entry not in present]
    if not missing:
        return False

    # Append cleanly: guarantee a trailing newline before our section so we never glue onto a
    # missing-EOL last line, then add the header + each missing entry.
    appended = existing
    if appended and not appended.endswith("\n"):
        appended += "\n"
    appended += "\n" + GITIGNORE_APPEND_HEADER + "\n"
    appended += "".join(entry + "\n" for entry in missing)
    path.write_text(appended, encoding="utf-8")
    return True
