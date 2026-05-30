"""Global constants and defaults. Pure data — no imports from other compact modules."""

from __future__ import annotations

# ---- Filesystem layout ----
COMPACT_DIR = ".compact"
ROOT_FILE = "root.yaml"
SUBSYS_DIR = "subsystems"
SUBSYS_FILE = "compact.yaml"
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
    ".compact",
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
