"""The Bounds extraction cache package.

Exposes the content-addressable per-file cache used by the validation engine to
skip re-extraction of unchanged files. See :mod:`bounds.cache.store`.
"""

from __future__ import annotations

from .store import FileRecord, State, load_state, save_state

__all__ = ["FileRecord", "State", "load_state", "save_state"]
