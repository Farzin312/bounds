"""CLI sub-package for Bounds.

Provides command implementations grouped by function (read, drift, setup, maintain)
and the main entry point.
"""

import click
import sys
from .main import main, _AGENT_FLAGS, _prompt_agent_selection
from ..shared.io import emit_loud
from ..core.manifest import loader as manifest_loader

def _interactive_human(explicit_human: bool) -> bool:
    """Whether an interactive action should announce in a terminal."""
    return explicit_human or sys.stdout.isatty()

__all__ = [
    "main", "sys", "click", "_AGENT_FLAGS", "_prompt_agent_selection",
    "_interactive_human", "manifest_loader", "emit_loud"
]
