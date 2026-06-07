"""AI Coding Agent integration sub-package.

Provides sync protocols for wiring agents and harness hooks for runtime nudging/gating.
"""

from . import guide
from .hook import run_hook
from .sync import run_agent

__all__ = ["guide", "run_agent", "run_hook"]
