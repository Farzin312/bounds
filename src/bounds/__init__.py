"""Bounds — AI-first architecture context for coding agents.

Deterministic structural validation, verified contracts, and CI drift gates.
Zero LLM inside.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

# Published on PyPI as "bounds-cli" (the bare "bounds" name belongs to an unrelated
# project); the import package and the CLI command stay "bounds". Resolve across both
# names so the version works whether installed from PyPI or as an editable checkout.
__version__ = "unknown"
for _dist in ("bounds-cli", "bounds"):
    try:
        __version__ = _version(_dist)
        break
    except PackageNotFoundError:
        continue

# ---------------------------------------------------------------------------
# Backward-compatible API (Tier 2)
# ---------------------------------------------------------------------------
# The internal structure is now modular (shared/, core/, agents/, etc.), but
# we re-export key modules and names here to avoid breaking existing tests
# and programmatic consumers.

from .shared import config, errors, models, output, gitutil, ignore, tsconfig, surface
from .shared.models import Issue, ValidationReport
from .shared.errors import BoundsError

from .core import (
    describe, locate, discover, calibrate, coverage, sdd, guide, ciconfig,
    extract, manifest, validate
)
from .core.manifest.loader import find_root, load_all
from .core.validate.engine import run as run_validation

from .agents import sync as agentsync, hook as agenthook

__all__ = [
    "__version__",
    "config",
    "errors",
    "models",
    "output",
    "gitutil",
    "ignore",
    "tsconfig",
    "surface",
    "Issue",
    "ValidationReport",
    "BoundsError",
    "describe",
    "locate",
    "discover",
    "calibrate",
    "coverage",
    "sdd",
    "guide",
    "ciconfig",
    "extract",
    "manifest",
    "validate",
    "find_root",
    "load_all",
    "run_validation",
    "agentsync",
    "agenthook",
]
