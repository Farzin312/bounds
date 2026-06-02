"""Shared filesystem-to-extraction helpers for the ``discover`` and ``calibrate`` commands.

Both commands need the same low-level moves the validation engine performs — walk the repo
for supported source files (honouring the hard-coded ignore dirs + ``.boundsignore``), and
extract one file's interface surface via its tree-sitter adapter. They are factored here so
the two commands stay independent of the engine's cached hot path while sharing this logic.

Everything is deterministic and zero-LLM: POSIX paths, sorted iteration, no timestamps.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from .. import config
from ..ignore import IgnoreMatcher, has_generated_marker
from ..models import ExtractResult, SubsystemCompact
from . import get_adapter, supported_extensions


def walk_supported(base: Path, exts: set[str] | None = None) -> list[Path]:
    """Supported source files under ``base``, symlink-cycle-safe (``exts=None`` → every file).

    A stack-based walk that records each visited directory's *real* path, so a symlinked
    directory that points back into the tree is descended at most once — a symlink loop can never
    hang the walk (unlike a bare ``rglob('*')`` on older Python). Skips :data:`config.DEFAULT_IGNORES`
    directories by name. Order is unspecified (callers sort); fail soft on any per-entry OS error.
    The single home for the recursive source walk shared by every walking call site.
    """
    out: list[Path] = []
    seen_dirs: set[Path] = set()
    try:
        base_real = base.resolve()
    except OSError:
        base_real = base
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
                    if entry.name in config.DEFAULT_IGNORES:
                        continue
                    # Never descend a symlinked directory that escapes the walk root —
                    # blocks unbounded traversal of (or extraction from) an external tree.
                    if entry.is_symlink():
                        try:
                            entry.resolve().relative_to(base_real)
                        except (OSError, ValueError):
                            continue
                    stack.append(entry)
                elif entry.is_file() and (exts is None or entry.suffix in exts):
                    out.append(entry)
            except OSError:
                continue
    return out


def in_default_ignores(path: Path, project_root: Path) -> bool:
    """True if ``path`` lies under any :data:`config.DEFAULT_IGNORES` directory.

    The single home for the hard-coded ignore-directory check: the repo walk, the
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

    The single home for extension stripping: ``validate.checks`` imports this rather
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

    The single home for the owned-file walk: the validation engine and ``describe`` both
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


def path_specificity(rel: str, paths) -> int:
    """How specifically a subsystem's declared ``paths`` cover ``rel`` — deeper match = higher.

    Used so that when two subsystems' paths nest (a parent dir and a sub-package), the *deepest*
    declared path owns the file, the same longest-prefix rule the tsconfig resolver uses. An exact
    file path beats a directory prefix; a glob contributes the depth of its literal prefix; a
    root/catch-all (``.`` or pure glob) is least specific (0). Returns 0 when no path covers ``rel``.
    """
    best = 0
    for raw in paths or []:
        lit = re.split(r"[*?\[]", str(raw).strip("/"), maxsplit=1)[0].rstrip("/")
        if not lit or lit == ".":
            continue  # root / pure-glob: least specific
        if rel == lit:
            best = max(best, lit.count("/") + 2)   # exact file path: most specific
        elif rel.startswith(lit + "/"):
            best = max(best, lit.count("/") + 1)   # directory / glob-prefix ancestor
    return best


def resolve_owners(
    project_root: Path,
    subsystems: dict[str, SubsystemCompact],
    exts: set[str],
) -> dict[str, tuple[str, Path]]:
    """Map every owned file (rel-posix) → ``(owning_subsystem, abs_path)`` — most-specific path wins.

    The single home for file→subsystem ownership, shared by the validation engine and
    :func:`extract_project`. When subsystem paths nest, the deepest matching path owns the file, so a
    sub-package keeps its own code instead of the alphabetically-first parent swallowing it (which
    used to starve the child and report false structural drift). Ties in specificity break to the
    sorted-first subsystem name for determinism.
    """
    claims: dict[str, tuple[int, str, Path]] = {}
    for name in sorted(subsystems):
        sub = subsystems[name]
        for abs_path in iter_subsystem_files(project_root, sub, exts):
            rel = abs_path.relative_to(project_root).as_posix()
            spec = path_specificity(rel, sub.paths)
            prev = claims.get(rel)
            if prev is None or spec > prev[0]:  # strictly-greater keeps the sorted-first tie winner
                claims[rel] = (spec, name, abs_path)
    return {rel: (name, abs_path) for rel, (_spec, name, abs_path) in claims.items()}


def mapping_coverage(
    project_root: Path,
    owned: set[str],
    matcher: IgnoreMatcher | None = None,
) -> dict:
    """How much of the repo's *source code* Bounds actually mapped, and an honest breakdown of what
    it could not — the metric that stops a polyglot repo from looking fully mapped while half of it
    is an unsupported language.

    Walks every non-ignored file (``config.DEFAULT_IGNORES`` + ``.boundsignore``), counts only files
    whose extension is in :data:`config.KNOWN_SOURCE_EXTS` (docs/config/assets are excluded so they
    can't dilute the %), and classifies each as:
      - **mapped** — owned by a subsystem (``rel in owned``),
      - **unowned-supported** — Bounds *has* an adapter for it, it's just not in any manifest's
        paths (fix: add it to a manifest — deterministically mappable),
      - **unsupported** — no adapter for that language yet (fix: hand-author/AI-author a manifest, or
        it waits for an adapter).
    Returns counts, ``mapped_pct``, and a sorted by-language breakdown of the unmapped source.
    Deterministic; safe to skip on the ``--quick`` hot path (callers gate it).
    """
    supported = supported_extensions()
    mapped = unowned_supported = unsupported = 0
    by_lang: dict[str, int] = {}
    unsupported_langs: set[str] = set()
    for abs_path in walk_supported(project_root, None):  # None => every file
        ext = abs_path.suffix
        lang = config.KNOWN_SOURCE_EXTS.get(ext)
        if lang is None:
            continue  # not source code (docs/config/assets) — out of the denominator
        rel = abs_path.relative_to(project_root).as_posix()
        if matcher and matcher.matches(rel):
            continue
        if rel in owned:
            mapped += 1
            continue
        by_lang[lang] = by_lang.get(lang, 0) + 1
        if ext in supported:
            unowned_supported += 1
        else:
            unsupported += 1
            unsupported_langs.add(lang)
    total = mapped + unowned_supported + unsupported
    return {
        "files_source_total": total,
        "files_mapped": mapped,
        "files_unmapped": unowned_supported + unsupported,
        "mapped_pct": round(mapped / total * 100, 1) if total else 100.0,
        "unmapped_unowned_supported": unowned_supported,
        "unmapped_unsupported_language": unsupported,
        "unmapped_by_language": dict(sorted(by_lang.items())),
        "unsupported_languages": sorted(unsupported_langs),
    }


def extract_project(
    project_root: Path,
    subsystems: dict[str, SubsystemCompact],
    matcher: IgnoreMatcher | None = None,
) -> tuple[dict[str, str], dict[str, ExtractResult], set[str]]:
    """Project-wide extraction shared by calibrate / impact --verify / ``where`` (single home).

    Resolves ownership via :func:`resolve_owners` (most-specific path wins), applies the optional
    ``.boundsignore`` ``matcher``, and tree-sitter-extracts each owned file. Returns
    ``(file_owner, extracts, generated)`` — rel-posix → owner, rel-posix → :class:`ExtractResult`
    (only files that parsed), and the set of ``@generated``-marked files. Deterministic: sorted
    iteration. Shares :func:`resolve_owners` with the validation engine so the two never disagree.
    """
    exts = supported_extensions()
    file_owner: dict[str, str] = {}
    extracts: dict[str, ExtractResult] = {}
    generated: set[str] = set()
    owners = resolve_owners(project_root, subsystems, exts)
    for rel in sorted(owners):
        name, _abs = owners[rel]
        if matcher and matcher.matches(rel):
            continue
        result, is_gen = extract_file(project_root, rel)
        file_owner[rel] = name
        if is_gen:
            generated.add(rel)
        if result is not None:
            extracts[rel] = result
    return file_owner, extracts, generated


def is_oversized(abs_path: Path) -> bool:
    """True if ``abs_path`` exceeds :data:`config.MAX_FILE_BYTES` (fail-soft on stat error).

    Single home for the size bound shared by :func:`read_source_bytes`, the validation
    engine's quick-mode cache-trust check, and anywhere else that must skip giant blobs.
    """
    try:
        return abs_path.stat().st_size > config.MAX_FILE_BYTES
    except OSError:
        return False


def read_source_bytes(abs_path: Path) -> tuple[bytes | None, str | None]:
    """Read a file's bytes under the :data:`config.MAX_FILE_BYTES` bound.

    Single home for the size-guard + read + ``OSError`` *mechanism*. Returns
    ``(source, None)`` on success; on failure ``(None, reason)`` where ``reason`` is
    ``"oversized"`` or a short OSError description. Callers own the *policy*: ``extract_file``
    drops silently, the validation engine surfaces a warning ``Issue`` on owned files.
    """
    if is_oversized(abs_path):
        return None, "oversized"
    try:
        return abs_path.read_bytes(), None
    except OSError as exc:
        return None, exc.strerror or type(exc).__name__


def extract_file(project_root: Path, rel: str) -> tuple[ExtractResult | None, bool]:
    """Extract one file's surface. Returns ``(result_or_None, is_generated)``.

    ``result`` is ``None`` when the extension is unsupported, the file is unreadable, or the file
    exceeds :data:`config.MAX_FILE_BYTES` (resource bound — a giant blob is skipped, never
    read into memory). ``is_generated`` is True when the file carries a ``@generated``-style header
    marker (callers skip generated files when proposing new exposes).
    """
    adapter = get_adapter(rel)
    if adapter is None:
        return None, False
    source, _ = read_source_bytes(project_root / rel)  # reason unused here: silent skip
    if source is None:  # unreadable or oversized — no report to attach to in this context
        return None, False
    generated = has_generated_marker(source)
    result = adapter.extract(rel, source)
    if result is None or result.error is not None:
        return None, generated
    return result, generated
