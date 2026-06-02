"""Adapter registry: resolve a language adapter by file extension or name.

Adapters are lazily constructed singletons (their tree-sitter Language objects
are expensive to build and are reused across files). The first lookup builds the
shared instances; subsequent lookups return them.
"""

from __future__ import annotations

from pathlib import Path

from .base import LanguageAdapter
from .prisma import PrismaAdapter
from .python import PythonAdapter
from .sql import SqlAdapter
from .typescript import TypeScriptAdapter

__all__ = ["adapter_for_language", "get_adapter", "supported_extensions"]

# extension -> singleton adapter instance; built on first use.
_ADAPTERS: dict[str, LanguageAdapter] | None = None
# language_name -> singleton adapter instance.
_BY_LANGUAGE: dict[str, LanguageAdapter] | None = None


def _ensure_built() -> None:
    """Construct the adapter singletons once and index them."""
    global _ADAPTERS, _BY_LANGUAGE
    if _ADAPTERS is not None:
        return
    by_ext: dict[str, LanguageAdapter] = {}
    by_lang: dict[str, LanguageAdapter] = {}
    for adapter in (PythonAdapter(), TypeScriptAdapter(), SqlAdapter(), PrismaAdapter()):
        by_lang[adapter.language_name] = adapter
        for ext in adapter.extensions:
            by_ext[ext] = adapter
    _ADAPTERS = by_ext
    _BY_LANGUAGE = by_lang


def get_adapter(rel_path: str) -> LanguageAdapter | None:
    """Return the adapter for ``rel_path``'s extension, or None if unsupported."""
    _ensure_built()
    assert _ADAPTERS is not None
    return _ADAPTERS.get(Path(rel_path).suffix)


def adapter_for_language(name: str) -> LanguageAdapter | None:
    """Return the adapter registered under language ``name``, or None."""
    _ensure_built()
    assert _BY_LANGUAGE is not None
    return _BY_LANGUAGE.get(name)


def supported_extensions() -> set[str]:
    """Return the set of file extensions any registered adapter handles."""
    _ensure_built()
    assert _ADAPTERS is not None
    return set(_ADAPTERS.keys())


def is_language_file(rel_path: str, language_name: str) -> bool:
    """True if ``rel_path``'s extension is handled by the adapter registered under ``language_name``.

    Single home for "does this file belong to language X?" so callers (e.g. the advisory
    raw-SQL scan) never re-hardcode an extension list that can drift from the adapter's own.
    """
    adapter = adapter_for_language(language_name)
    return adapter is not None and Path(rel_path).suffix in adapter.extensions
