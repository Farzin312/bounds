"""Deterministic, zero-LLM extraction of a file's interface surface.

Tree-sitter adapters turn source bytes into exported symbols and import
references (the :class:`~compact.models.ExtractResult` tier). The registry
resolves an adapter by file extension or language name.

Public API:
  * ``get_adapter`` / ``adapter_for_language`` / ``supported_extensions`` -- registry
  * ``LanguageAdapter`` -- adapter base class
  * ``content_hash`` / ``structure_hash`` / ``make_result`` -- hashing helpers
"""

from __future__ import annotations

from .base import LanguageAdapter, content_hash, make_result, structure_hash
from .registry import adapter_for_language, get_adapter, supported_extensions

__all__ = [
    "get_adapter",
    "adapter_for_language",
    "supported_extensions",
    "LanguageAdapter",
    "content_hash",
    "structure_hash",
    "make_result",
]
