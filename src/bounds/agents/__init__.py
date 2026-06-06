"""AI Coding Agent integration sub-package.

Provides sync protocols for wiring agents and harness hooks for runtime nudging/gating.
"""

from .sync import run_agent
from .hook import run_hook

__all__ = ["run_agent", "run_hook"]
