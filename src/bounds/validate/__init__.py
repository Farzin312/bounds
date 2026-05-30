"""Validation engine: mode dispatch, reference propagation, and the 6 structural checks."""

from .checks import CheckContext, CHECKS_BY_MODE
from .engine import run

__all__ = ["run", "CheckContext", "CHECKS_BY_MODE"]
