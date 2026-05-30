"""Manifest package: discovery, loading, and schema validation of ``.compact/`` manifests.

Re-exports the loader entry points (``find_root``, ``load_root``, ``load_subsystem``,
``load_all``) and the schema validators (``validate_root``, ``validate_subsystem``).
"""

from __future__ import annotations

from .loader import find_root, load_all, load_root, load_subsystem
from .schema import validate_root, validate_subsystem

__all__ = [
    "find_root",
    "load_root",
    "load_subsystem",
    "load_all",
    "validate_root",
    "validate_subsystem",
]
