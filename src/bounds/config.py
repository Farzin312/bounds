"""Global constants, defaults, and config-directory resolution.

Imports nothing from other bounds modules (only the stdlib), so every module can depend
on it without risking an import cycle.
"""

from __future__ import annotations

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
# Committed drift baseline for `calibrate --check`: the set of manifest-vs-source
# discrepancies that already exist on the main branch. The check flags only NEW drift
# above this baseline, so a PR is never blocked by pre-existing rot it didn't introduce.
DRIFT_BASELINE_FILE = "drift-baseline.json"

# ---- Schema / versioning ----
SCHEMA_VERSION = "1"
# Bump whenever extraction OUTPUT changes for unchanged source (new/changed adapter symbols),
# so every existing binary cache.db is treated as version-mismatched and rebuilt rather than
# serving stale symbols. v2: SQL adapter recovers transaction-wrapped DDL + folds policies/RLS
# and canonicalises table names (see extract/sql.py, validate/schema.py).
STATE_VERSION = "2"

# The documented command to refresh a stale git/pipx install. Surfaced by `upgrade-check`
# and embedded in the generated agent contract; kept here as the single source so the two
# can never drift.
UPGRADE_INSTALL_CMD = "pipx install --force git+https://github.com/Farzin312/bounds.git"

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
    ".git",
    # Generic build output
    "dist",
    "build",
    "out",
    "target",  # Rust/Cargo, Maven/Gradle JVM output
    "coverage",
    # JS/TS framework build & tooling dirs
    ".next",
    ".turbo",
    ".svelte-kit",
    ".vercel",
    ".cache",
    # Python
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    # Bounds itself
    ".bounds",
    ".compact",  # legacy config dir
}

# Extension -> language label for *known source code*, language-agnostic. The denominator for
# mapping-coverage: how much of a repo's source Bounds actually mapped, and an honest, by-language
# breakdown of what it could not (so a polyglot repo can't look "fully mapped" while half of it is an
# unsupported language). The labels Bounds has an adapter for (python/typescript/javascript/sql/
# prisma) are *mappable*; the rest are *unsupported today* and surface as a loud coverage gap. Only
# code extensions belong here — docs/config/assets are deliberately excluded so they don't dilute %.
KNOWN_SOURCE_EXTS = {
    ".py": "python", ".pyi": "python",
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
