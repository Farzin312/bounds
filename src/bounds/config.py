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
STATE_FILE = "state.json"

# ---- Schema / versioning ----
SCHEMA_VERSION = "1"
STATE_VERSION = "1"

# ---- Enumerations ----
VALID_ROLES = {"service", "platform", "connector", "library"}
VALID_CRITICALITY = {"core", "connector", "leaf"}
VALID_MODES = {"quick", "full", "preflight", "hotfix", "audit"}
VALID_ENFORCE = {"on", "off"}

# ---- Extraction ----
# Directories never descended into when globbing subsystem files.
DEFAULT_IGNORES = {
    "node_modules",
    ".git",
    "dist",
    "build",
    "__pycache__",
    ".venv",
    "venv",
    ".bounds",
    ".compact",  # legacy config dir
    ".mypy_cache",
    ".pytest_cache",
}

# ---- Propagation ----
# Depth of consumer propagation, keyed by the *changed provider's* criticality.
#   -1 = unbounded (transitive closure), 0 = none, N = N hops.
PROPAGATION_DEPTH = {"core": -1, "connector": 1, "leaf": 0}

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
