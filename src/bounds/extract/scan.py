"""Shared filesystem-to-extraction helpers for the ``discover`` and ``calibrate`` commands.

Both commands need the same low-level moves the validation engine performs — walk the repo
for supported source files (honouring the hard-coded ignore dirs + ``.boundsignore``), and
extract one file's interface surface via its tree-sitter adapter. They are factored here so
the two commands stay independent of the engine's cached hot path while sharing this logic.

Everything is deterministic and zero-LLM: POSIX paths, sorted iteration, no timestamps.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from .. import config
from ..ignore import IgnoreMatcher, has_generated_marker
from ..models import ExtractResult, SubsystemCompact
from . import get_adapter, supported_extensions


def walk_supported(base: Path, exts: set[str]) -> list[Path]:
    """Supported source files under ``base``, symlink-cycle-safe (s-33).

    A stack-based walk that records each visited directory's *real* path, so a symlinked
    directory that points back into the tree is descended at most once — a symlink loop can never
    hang the walk (unlike a bare ``rglob('*')`` on older Python). Skips :data:`config.DEFAULT_IGNORES`
    directories by name. Order is unspecified (callers sort); fail soft on any per-entry OS error.
    The single home for the recursive source walk shared by every walking call site (s-34).
    """
    out: list[Path] = []
    seen_dirs: set[Path] = set()
    stack: list[Path] = [base]
    while stack:
        d = stack.pop()
        try:
            real = d.resolve()
        except OSError:
            continue
        if real in seen_dirs:  # symlink-cycle / already-visited guard
            continue
        seen_dirs.add(real)
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    if entry.name not in config.DEFAULT_IGNORES:
                        stack.append(entry)
                elif entry.is_file() and entry.suffix in exts:
                    out.append(entry)
            except OSError:
                continue
    return out


def in_default_ignores(path: Path, project_root: Path) -> bool:
    """True if ``path`` lies under any :data:`config.DEFAULT_IGNORES` directory.

    The single home for the hard-coded ignore-directory check (s-34): the repo walk, the
    owned-file walk, and the unowned-file universe all share this one predicate instead of
    re-deriving ``part in DEFAULT_IGNORES`` in three places. Fail soft: a path outside the
    project (``relative_to`` raises) is treated as not-ignored.
    """
    try:
        rel = path.relative_to(project_root)
    except ValueError:
        return False
    return any(part in config.DEFAULT_IGNORES for part in rel.parts)


def strip_ext(rel: str) -> str:
    """Return ``rel`` with its file extension removed (for import-resolution stems).

    The single home for extension stripping (s-34): ``validate.checks`` imports this rather
    than carrying its own copy. Uses :class:`~pathlib.PurePosixPath` so repo-relative posix
    paths are split the same way on every platform.
    """
    suffix = PurePosixPath(rel).suffix
    return rel[: -len(suffix)] if suffix else rel


def iter_repo_source(project_root: Path, matcher: IgnoreMatcher | None = None) -> list[str]:
    """Repo-relative POSIX paths of every supported source file under ``project_root``.

    Skips the hard-coded :data:`config.DEFAULT_IGNORES` directories and any ``.boundsignore``
    match (``matcher``). Sorted for deterministic output.
    """
    exts = supported_extensions()
    out: list[str] = []
    for f in walk_supported(project_root, exts):
        try:
            rel = f.relative_to(project_root).as_posix()
        except ValueError:
            continue
        if matcher and matcher.matches(rel):
            continue
        out.append(rel)
    return sorted(out)


def iter_subsystem_files(project_root: Path, sub: SubsystemCompact, exts: set[str]) -> list[Path]:
    """Every supported source file a subsystem owns (its ``paths`` globs + explicit ``files``).

    The single home for the owned-file walk (s-34): the validation engine and ``describe`` both
    call this so they agree on exactly which files belong to a subsystem — no second copy to
    drift. Deduplicated by real path (so a file reached via two globs is counted once) and
    sorted by posix path for deterministic output. Skips :data:`config.DEFAULT_IGNORES`.
    """
    out: list[Path] = []
    seen: set[Path] = set()

    def add(f: Path) -> None:
        try:
            key = f.resolve()
        except OSError:
            key = f
        if key in seen:
            return
        seen.add(key)
        out.append(f)

    for raw in sub.paths or []:
        base = project_root / raw
        if base.is_dir():
            for f in walk_supported(base, exts):
                add(f)
        elif base.is_file():
            if base.suffix in exts:
                add(base)
        else:  # treat as a glob relative to the project root
            for f in sorted(project_root.glob(raw)):
                if f.is_file() and f.suffix in exts and not in_default_ignores(f, project_root):
                    add(f)

    for raw in sub.files or []:
        f = project_root / raw
        if f.is_file() and f.suffix in exts:
            add(f)

    return sorted(out, key=lambda p: p.as_posix())


def extract_project(
    project_root: Path,
    subsystems: dict[str, SubsystemCompact],
    matcher: IgnoreMatcher | None = None,
) -> tuple[dict[str, str], dict[str, ExtractResult], set[str]]:
    """Project-wide extraction shared by calibrate / impact --verify / ``where`` (s-34 single home).

    Walks every subsystem's owned files (flat topology: first declared owner wins), applies the
    optional ``.boundsignore`` ``matcher``, and tree-sitter-extracts each. Returns
    ``(file_owner, extracts, generated)`` — rel-posix → owner, rel-posix → :class:`ExtractResult`
    (only files that parsed), and the set of ``@generated``-marked files. Deterministic: sorted
    subsystem + file iteration. The owned-file walk is :func:`iter_subsystem_files`, so this agrees
    with the validation engine on ownership rather than carrying yet another copy.
    """
    exts = supported_extensions()
    file_owner: dict[str, str] = {}
    extracts: dict[str, ExtractResult] = {}
    generated: set[str] = set()
    for name in sorted(subsystems):
        for abs_path in iter_subsystem_files(project_root, subsystems[name], exts):
            rel = abs_path.relative_to(project_root).as_posix()
            if rel in file_owner:  # flat topology: first declared owner wins
                continue
            if matcher and matcher.matches(rel):
                continue
            result, is_gen = extract_file(project_root, rel)
            file_owner[rel] = name
            if is_gen:
                generated.add(rel)
            if result is not None:
                extracts[rel] = result
    return file_owner, extracts, generated


def extract_file(project_root: Path, rel: str) -> tuple[ExtractResult | None, bool]:
    """Extract one file's surface. Returns ``(result_or_None, is_generated)``.

    ``result`` is ``None`` when the extension is unsupported, the file is unreadable, or the file
    exceeds :data:`config.MAX_FILE_BYTES` (s-33 resource bound — a giant blob is skipped, never
    read into memory). ``is_generated`` is True when the file carries a ``@generated``-style header
    marker (callers skip generated files when proposing new exposes — s-14/s-16).
    """
    adapter = get_adapter(rel)
    if adapter is None:
        return None, False
    path = project_root / rel
    try:
        if path.stat().st_size > config.MAX_FILE_BYTES:
            return None, False
        source = path.read_bytes()
    except OSError:
        return None, False
    generated = has_generated_marker(source)
    result = adapter.extract(rel, source)
    if result is None or result.error is not None:
        return None, generated
    return result, generated
