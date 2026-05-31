"""Validation engine: mode dispatch, reference propagation, the 6 structural checks, and the schema-health advisory."""

from .checks import CheckContext, CHECKS_BY_MODE
from .engine import run

__all__ = ["run", "CheckContext", "CHECKS_BY_MODE"]
