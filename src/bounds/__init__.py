"""Bounds — AI-first architecture context for coding agents.

Deterministic structural validation, verified contracts, and CI drift gates.
Zero LLM inside.
"""

import importlib
import sys

from .shared.version import VERSION

__version__ = VERSION

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
    describe, locate, discover, calibrate, coverage, sdd, ciconfig,
    extract, manifest, validate
)
from .core.manifest.loader import find_root, load_all
from .core.validate.engine import run as run_validation

from .agents import guide, sync as agentsync, hook as agenthook


_LEGACY_MODULE_ALIASES = {
    "agenthook": "bounds.agents.hook",
    "agentsync": "bounds.agents.sync",
    "calibrate": "bounds.core.calibrate",
    "ciconfig": "bounds.core.ciconfig",
    "coverage": "bounds.core.coverage",
    "describe": "bounds.core.describe",
    "discover": "bounds.core.discover",
    "extract": "bounds.core.extract",
    "extract.base": "bounds.core.extract.base",
    "extract.prisma": "bounds.core.extract.prisma",
    "extract.python": "bounds.core.extract.python",
    "extract.rawquery": "bounds.core.extract.rawquery",
    "extract.registry": "bounds.core.extract.registry",
    "extract.scan": "bounds.core.extract.scan",
    "extract.shell": "bounds.core.extract.shell",
    "extract.sql": "bounds.core.extract.sql",
    "extract.typescript": "bounds.core.extract.typescript",
    "guide": "bounds.agents.guide",
    "locate": "bounds.core.locate",
    "manifest": "bounds.core.manifest",
    "manifest.loader": "bounds.core.manifest.loader",
    "manifest.schema": "bounds.core.manifest.schema",
    "sdd": "bounds.core.sdd",
    "validate": "bounds.core.validate",
    "validate.checks": "bounds.core.validate.checks",
    "validate.engine": "bounds.core.validate.engine",
    "validate.propagation": "bounds.core.validate.propagation",
    "validate.schema": "bounds.core.validate.schema",
    "cache": "bounds.shared.cache",
    "cache.store": "bounds.shared.cache.store",
    "config": "bounds.shared.config",
    "errors": "bounds.shared.errors",
    "gitutil": "bounds.shared.gitutil",
    "ignore": "bounds.shared.ignore",
    "models": "bounds.shared.models",
    "output": "bounds.shared.output",
    "surface": "bounds.shared.surface",
    "tsconfig": "bounds.shared.tsconfig",
    "version": "bounds.shared.version",
    "_io": "bounds.shared.io",
    "update_check": "bounds.maintenance.update",
    "upgrade": "bounds.maintenance.upgrade",
}


def _install_legacy_module_aliases() -> None:
    """Keep documented pre-refactor module imports working during migration."""
    for legacy, canonical in _LEGACY_MODULE_ALIASES.items():
        sys.modules.setdefault(f"{__name__}.{legacy}", importlib.import_module(canonical))


_install_legacy_module_aliases()

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
