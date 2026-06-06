"""Validation engine: mode dispatch, propagation, and eight deterministic checks."""

from .checks import CheckContext, CHECKS_BY_MODE
from .engine import run

__all__ = ["run", "CheckContext", "CHECKS_BY_MODE"]
