"""Shared filesystem-to-extraction helpers for the ``discover`` and ``calibrate`` commands.

Both commands need the same low-level moves the validation engine performs — walk the repo
for supported source files (honouring the hard-coded ignore dirs + ``.boundsignore``), and
extract one file's interface surface via its tree-sitter adapter. They are factored here so
the two commands stay independent of the engine's cached hot path while sharing this logic.

Everything is deterministic and zero-LLM: POSIX paths, sorted iteration, no timestamps.
"""

from __future__ import annotations

from pathlib import Path

from .. import config
from ..ignore import IgnoreMatcher, has_generated_marker
from ..models import ExtractResult
from . import get_adapter, supported_extensions


def strip_ext(rel: str) -> str:
    """Return ``rel`` with its file extension removed (for import-resolution stems)."""
    suffix = Path(rel).suffix
    return rel[: -len(suffix)] if suffix else rel


def iter_repo_source(project_root: Path, matcher: IgnoreMatcher | None = None) -> list[str]:
    """Repo-relative POSIX paths of every supported source file under ``project_root``.

    Skips the hard-coded :data:`config.DEFAULT_IGNORES` directories and any ``.boundsignore``
    match (``matcher``). Sorted for deterministic output.
    """
    exts = supported_extensions()
    out: list[str] = []
    for f in project_root.rglob("*"):
        if not f.is_file() or f.suffix not in exts:
            continue
        try:
            rel_parts = f.relative_to(project_root).parts
        except ValueError:
            continue
        if any(part in config.DEFAULT_IGNORES for part in rel_parts):
            continue
        rel = f.relative_to(project_root).as_posix()
        if matcher and matcher.matches(rel):
            continue
        out.append(rel)
    return sorted(out)


def extract_file(project_root: Path, rel: str) -> tuple[ExtractResult | None, bool]:
    """Extract one file's surface. Returns ``(result_or_None, is_generated)``.

    ``result`` is ``None`` when the extension is unsupported or the file is unreadable.
    ``is_generated`` is True when the file carries a ``@generated``-style header marker
    (callers skip generated files when proposing new exposes — s-14/s-16).
    """
    adapter = get_adapter(rel)
    if adapter is None:
        return None, False
    try:
        source = (project_root / rel).read_bytes()
    except OSError:
        return None, False
    generated = has_generated_marker(source)
    result = adapter.extract(rel, source)
    if result is None or result.error is not None:
        return None, generated
    return result, generated
