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

from .. import config, errors
from ..ignore import IgnoreMatcher, has_generated_marker
from ..models import ExtractResult, Issue, SubsystemCompact
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


# Directory-segment names that mark a test tree (pytest / Jest / Vitest / Mocha conventions).
_TEST_DIR_PARTS = {"tests", "test", "__tests__", "spec", "specs", "e2e"}


def is_test_file(rel: str) -> bool:
    """A test file by directory or filename convention (pytest / Jest / Vitest / Mocha).

    The single shared home for test-file recognition: discover (to keep test cases out of a
    subsystem's public surface), doc/test ownership resolution, and mapping-coverage all key off
    this one predicate so they never disagree about what counts as a test. ``rel`` is a
    repo-relative posix path.
    """
    parts = rel.split("/")
    if any(p in _TEST_DIR_PARTS for p in parts[:-1]):
        return True
    stem = parts[-1]
    return (stem.startswith("test_") or stem.startswith("test.")
            or "_test." in stem or ".test." in stem or ".spec." in stem or "_spec." in stem
            or "conftest." in stem)


def is_test_symbol(name: str, kind: str) -> bool:
    """A test case, not public API: a ``test_*`` function or a ``Test*`` class (xUnit/pytest)."""
    return name.startswith("test_") or (kind == "class" and name.startswith("Test"))


# Next.js App-/Pages-Router special filenames. Under an ``app/`` or ``pages/`` directory these are
# framework ENTRY files: the framework invokes their exports by convention (the default page/layout
# component, route handlers ``GET``/``POST``…, and route-segment config like ``revalidate``/
# ``dynamic``/``generateMetadata``). Nothing *imports* them, so — like test cases — their exports are
# not a consumable cross-module surface.
_FRAMEWORK_ENTRY_STEMS = {
    "page", "layout", "loading", "error", "not-found", "template", "default", "route",
    "global-error", "middleware", "instrumentation", "sitemap", "robots", "manifest",
    "opengraph-image", "twitter-image", "icon", "apple-icon",
}
_TS_JS_EXTS = ("ts", "tsx", "js", "jsx", "mts", "cts", "mjs", "cjs")


def is_framework_entry_file(rel: str) -> bool:
    """A Next.js framework entry file (``page``/``layout``/``route``/… under ``app/`` or ``pages/``).

    Such a file's exports are framework-invoked by convention, not a consumable API — so they are
    excluded from a subsystem's ``exposes`` (discover) and from undeclared-export drift (validate),
    exactly like a test case (:func:`is_test_symbol`), to stop a Next.js app from flooding with
    framework-callback noise. Gated on BOTH the special filename AND an ``app``/``pages`` path
    segment, so an ordinary library file (e.g. ``lib/route.ts``) is never mistaken for a route entry.
    ``rel`` is a repo-relative posix path.
    """
    parts = rel.split("/")
    base = parts[-1]
    stem, _, ext = base.partition(".")
    if ext not in _TS_JS_EXTS or stem not in _FRAMEWORK_ENTRY_STEMS:
        return False
    return any(seg in ("app", "pages") for seg in parts[:-1])


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


def iter_subsystem_files(
    project_root: Path, sub: SubsystemCompact, exts: set[str] | None
) -> list[Path]:
    """Files a subsystem owns (its ``paths`` globs + explicit ``files``), filtered by ``exts``.

    The single home for the owned-file walk: the validation engine and ``describe`` both
    call this so they agree on exactly which files belong to a subsystem — no second copy to
    drift. ``exts`` is the extension allow-set (e.g. ``supported_extensions()``); pass ``None`` to
    include EVERY extension — used by :func:`subsystems_with_unsupported_source` so an
    unsupported-language file (``.go``/``.rs``/…) is visible (mirroring ``walk_supported``'s
    ``exts=None`` convention). Deduplicated by real path (so a file reached via two globs is counted
    once) and sorted by posix path for deterministic output. Skips :data:`config.DEFAULT_IGNORES`.
    """
    out: list[Path] = []
    seen: set[Path] = set()

    def included(suffix: str) -> bool:
        return exts is None or suffix in exts

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
            if included(base.suffix):
                add(base)
        else:  # treat as a glob relative to the project root
            for f in sorted(project_root.glob(raw)):
                if f.is_file() and included(f.suffix) and not in_default_ignores(f, project_root):
                    add(f)

    for raw in sub.files or []:
        f = project_root / raw
        if f.is_file() and included(f.suffix):
            add(f)

    return sorted(out, key=lambda p: p.as_posix())


def path_specificity(rel: str, paths, files=None) -> int:
    """How specifically a subsystem's declared ``paths``/``files`` cover ``rel`` — deeper = higher.

    Used so that when two subsystems' paths nest (a parent dir and a sub-package), the *deepest*
    declared path owns the file, the same longest-prefix rule the tsconfig resolver uses. An explicit
    ``files:`` entry naming ``rel`` exactly is the strongest intent a manifest can express and
    outranks any directory prefix; an exact file path in ``paths`` ties it; a glob contributes the
    depth of its literal prefix; a root/catch-all (``.`` or pure glob) is least specific (0). Returns
    0 when nothing covers ``rel``.
    """
    best = 0
    for raw in files or []:
        if rel == str(raw).strip("/"):
            best = max(best, rel.count("/") + 2)   # explicit single-file declaration: most specific
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
    matcher: IgnoreMatcher | None = None,
    repo: Path | None = None,
    include_gitignored: bool = False,
) -> dict[str, tuple[str, Path]]:
    """Map every owned file (rel-posix) → ``(owning_subsystem, abs_path)`` — most-specific path wins.

    The single home for file→subsystem ownership, shared by the validation engine and
    :func:`extract_project`. When subsystem paths nest, the deepest matching path owns the file, so a
    sub-package keeps its own code instead of the alphabetically-first parent swallowing it (which
    used to starve the child and report false structural drift). Ties in specificity break to the
    sorted-first subsystem name for determinism.

    Optionally **ignore-aware**: pass ``matcher`` (``.boundsignore``) and/or ``repo`` (the git root,
    for ``.gitignore``) to drop files the validation engine would skip, so a caller like ``describe``
    that wants parity with ``validate`` counts exactly the same set. Omit both for the raw ownership
    map (the engine applies its own filter + symlink handling on top).
    """
    claims: dict[str, tuple[int, str, Path]] = {}
    for name in sorted(subsystems):
        sub = subsystems[name]
        for abs_path in iter_subsystem_files(project_root, sub, exts):
            rel = abs_path.relative_to(project_root).as_posix()
            spec = path_specificity(rel, sub.paths, sub.files)
            prev = claims.get(rel)
            if prev is None or spec > prev[0]:  # strictly-greater keeps the sorted-first tie winner
                claims[rel] = (spec, name, abs_path)
    result = {rel: (name, abs_path) for rel, (_spec, name, abs_path) in claims.items()}
    if matcher is not None:
        result = {rel: v for rel, v in result.items() if not matcher.matches(rel)}
    if repo is not None and not include_gitignored and result:
        from .. import gitutil  # lazy: keep extract/ free of a top-level gitutil import
        ignored = gitutil.gitignored(repo, list(result))
        if ignored:
            result = {rel: v for rel, v in result.items() if rel not in ignored}
    return result


def _ownership_overlap_issues(
    project_root: Path,
    subsystems: dict[str, SubsystemCompact],
    exts: set[str],
    matcher: IgnoreMatcher | None = None,
    repo: Path | None = None,
    include_gitignored: bool = False,
    *,
    subsystem: str | None = None,
    aggregate: bool = True,
) -> list[Issue]:
    """Equal-specificity ownership conflicts in the same file universe as validation.

    Nested paths are valid: the deepest declaration wins. This reports only ties at the winning
    specificity, where ownership falls back to alphabetical subsystem order. ``aggregate=True``
    emits one warning per contender set so a broad duplicate path does not flood validation.
    ``aggregate=False`` preserves the per-file diagnostics used by ``describe --full``.
    """
    scanned = resolve_owners(project_root, subsystems, exts, matcher, repo, include_gitignored)
    claims: dict[str, dict[int, set[str]]] = {}
    for name in sorted(subsystems):
        sub = subsystems[name]
        for abs_path in iter_subsystem_files(project_root, sub, exts):
            rel = abs_path.relative_to(project_root).as_posix()
            if rel not in scanned:
                continue
            spec = path_specificity(rel, sub.paths, sub.files)
            claims.setdefault(rel, {}).setdefault(spec, set()).add(name)

    grouped: dict[tuple[int, tuple[str, ...]], list[str]] = {}
    per_file: list[tuple[str, int, tuple[str, ...]]] = []
    for rel in sorted(claims):
        by_spec = claims[rel]
        top = max(by_spec)
        contenders = tuple(sorted(by_spec[top]))
        if len(contenders) < 2 or (subsystem is not None and subsystem not in contenders):
            continue
        if aggregate:
            grouped.setdefault((top, contenders), []).append(rel)
        else:
            per_file.append((rel, top, contenders))

    issues: list[Issue] = []
    if aggregate:
        for (_spec, contenders), rels in sorted(grouped.items()):
            owner = subsystem or min(contenders)
            others = [c for c in contenders if c != owner]
            sample = ", ".join(rels[:3])
            if len(rels) > 3:
                sample += f", +{len(rels) - 3} more"
            issues.append(Issue(
                code=errors.E_SUBSYSTEM_OVERLAP,
                severity=errors.SEVERITY[errors.E_SUBSYSTEM_OVERLAP],
                message=(f"{len(rels)} file(s) are claimed at equal specificity by "
                         f"{list(contenders)}; ownership is decided only by sorted-first name "
                         f"({min(contenders)}). Sample: {sample}"),
                subsystem=owner,
                file=rels[0] if len(rels) == 1 else None,
                fix=("narrow one subsystem path or move shared files to `files:` so a single "
                     f"subsystem owns them (currently overlaps with {others})"),
            ))
        return issues

    for rel, _spec, contenders in per_file:
        owner = subsystem or min(contenders)
        others = [c for c in contenders if c != owner]
        issues.append(Issue(
            code=errors.E_SUBSYSTEM_OVERLAP,
            severity=errors.SEVERITY[errors.E_SUBSYSTEM_OVERLAP],
            message=(f"'{rel}' is claimed at equal specificity by {list(contenders)}; "
                     f"ownership is decided only by sorted-first name ({min(contenders)})"),
            subsystem=owner,
            file=rel,
            fix=(f"narrow one path or move '{rel}' to `files:` so a single subsystem owns it "
                 f"(currently overlaps with {others})"),
        ))
    return issues


def _gitignore_filter(
    project_root: Path,
    rels: list[str],
    repo: Path | None,
    include_gitignored: bool,
) -> list[str]:
    """Drop gitignored paths from ``rels`` (parity with mapping_coverage's denominator filter).

    No-op when ``include_gitignored`` is True or ``repo`` is None (non-git repo); otherwise asks git
    once for the ignored subset. Lazy import keeps ``extract/`` free of a top-level gitutil import.
    """
    if include_gitignored or repo is None or not rels:
        return rels
    from .. import gitutil
    ignored = gitutil.gitignored(repo, rels)
    return [r for r in rels if r not in ignored]


def _walk_rels(
    project_root: Path,
    predicate,
    matcher: IgnoreMatcher | None,
    repo: Path | None,
    include_gitignored: bool,
) -> list[str]:
    """Repo-relative posix paths of every non-ignored file matching ``predicate(rel, ext)``.

    Ignore-aware exactly like :func:`mapping_coverage` — ``config.DEFAULT_IGNORES`` (via
    :func:`walk_supported`), ``.boundsignore`` (``matcher``), and ``.gitignore`` (``repo``) — so the
    doc/test universe matches the file universe the rest of validation uses. Sorted, deterministic.
    """
    rels: list[str] = []
    for abs_path in walk_supported(project_root, None):  # None => every file
        rel = abs_path.relative_to(project_root).as_posix()
        if matcher and matcher.matches(rel):
            continue
        if not predicate(rel, abs_path.suffix):
            continue
        rels.append(rel)
    rels = _gitignore_filter(project_root, rels, repo, include_gitignored)
    return sorted(rels)


def _explicit_owner(rel: str, subs: dict[str, "SubsystemCompact"], *, link_field: str) -> str | None:
    """Most-specific subsystem that EXPLICITLY declares ``rel``, or None.

    Considers each subsystem's ``link_field`` (``tests``/``docs``) first, then its ``paths``/``files``
    — all path-shaped, so :func:`path_specificity` ranks them on one scale (an explicit
    ``files:`` entry is strongest, then exact path, then directory/glob prefix). Most-specific wins;
    equal specificity breaks to the sorted-first subsystem name for determinism. Returns None when no
    subsystem covers ``rel`` at specificity > 0.
    """
    best_spec = 0
    best_name: str | None = None
    for name in sorted(subs):  # sorted ⇒ first-seen at a given specificity is the sorted-first name
        sub = subs[name]
        declared = list(getattr(sub, link_field, []) or [])
        # link_field paths + the subsystem's own paths/files, ranked together (path_specificity
        # treats `paths`-shaped entries uniformly). files: is the strongest single-file intent.
        spec = path_specificity(rel, declared + list(sub.paths or []), sub.files)
        if spec > best_spec:  # strictly-greater keeps the sorted-first tie winner
            best_spec = spec
            best_name = name
    return best_name


def resolve_test_owners(
    project_root: Path,
    subsystems: dict[str, "SubsystemCompact"],
    matcher: IgnoreMatcher | None = None,
    repo: Path | None = None,
    include_gitignored: bool = False,
) -> dict[str, str | None]:
    """Map every repo test file (rel-posix) → ``owning_subsystem`` name, or None when unlinked.

    Walks the tree once for test files (recognized via :func:`is_test_file`, ignore-aware like
    :func:`mapping_coverage`) and assigns an owner in priority order:

      1. **explicit** — the most-specific subsystem whose ``tests:`` (then ``paths``/``files``)
         declares the file (:func:`_explicit_owner`);
      2. **convention** — a test file directly under a subsystem's declared ``paths`` belongs to it
         (deepest path wins); else a path segment ``tests/<area>/…`` / ``test/<area>/…`` /
         ``__tests__/<area>/…`` or a filename ``test_<area>.py`` / ``<area>.test.ts`` /
         ``<area>.spec.ts`` maps to a subsystem whose NAME == ``<area>`` if one exists;
      3. else **None** — unlinked (informational, never a gap).

    Deterministic: most-specific wins, equal specificity breaks to the sorted-first subsystem name.
    """
    rels = _walk_rels(project_root, lambda rel, ext: is_test_file(rel), matcher, repo, include_gitignored)
    sub_names = set(subsystems)
    out: dict[str, str | None] = {}
    for rel in rels:
        owner = _explicit_owner(rel, subsystems, link_field="tests")
        if owner is None:
            owner = _convention_test_owner(rel, subsystems, sub_names)
        out[rel] = owner
    return out


def _convention_test_owner(
    rel: str, subsystems: dict[str, "SubsystemCompact"], sub_names: set[str]
) -> str | None:
    """Convention-based owner for a test file (no explicit ``tests:`` declaration matched).

    First: a test file directly under a subsystem's declared ``paths`` belongs to that subsystem
    (deepest declared path wins, via :func:`path_specificity`). Otherwise: derive an ``<area>`` from
    a ``tests/<area>/…`` (or ``test``/``__tests__``) path segment, or a ``test_<area>.py`` /
    ``<area>.test.ts`` / ``<area>.spec.ts`` filename, and map it to a subsystem of that exact name.
    Returns None when no convention matches.
    """
    # (a) directly under a subsystem's declared paths/files — deepest path owns it.
    best_spec = 0
    best_name: str | None = None
    for name in sorted(subsystems):
        sub = subsystems[name]
        spec = path_specificity(rel, sub.paths, sub.files)
        if spec > best_spec:
            best_spec = spec
            best_name = name
    if best_name is not None:
        return best_name
    # (b) area from a tests/<area>/ path segment.
    parts = rel.split("/")
    for i, part in enumerate(parts[:-1]):
        if part in {"tests", "test", "__tests__"} and i + 1 < len(parts) - 1:
            area = parts[i + 1]
            if area in sub_names:
                return area
    # (c) area from the filename: test_<area>.py / <area>.test.ts / <area>.spec.ts.
    stem = parts[-1]
    area = _filename_test_area(stem)
    if area and area in sub_names:
        return area
    return None


def _filename_test_area(stem: str) -> str | None:
    """Extract the tested ``<area>`` from a test filename, or None.

    ``test_auth.py`` → ``auth``; ``auth.test.ts`` / ``auth.spec.ts`` → ``auth``;
    ``auth_test.py`` → ``auth``. Returns the bare area name (no directory, no extension).
    """
    if stem.startswith("test_"):
        rest = stem[len("test_"):]
        return rest.split(".", 1)[0] or None
    for marker in (".test.", ".spec.", "_test.", "_spec."):
        if marker in stem:
            return stem.split(marker, 1)[0] or None
    return None


def resolve_doc_owners(
    project_root: Path,
    subsystems: dict[str, "SubsystemCompact"],
    matcher: IgnoreMatcher | None = None,
    repo: Path | None = None,
    include_gitignored: bool = False,
) -> dict[str, str | None]:
    """Map every repo doc file (rel-posix) → ``owning_subsystem`` name, or None when unlinked.

    Doc files are those whose extension is in :data:`config.DOC_EXTS` (``.md``/``.mdx``/``.rst``),
    ignore-aware like :func:`mapping_coverage`. Owner assignment, in priority order:

      1. **explicit** — the most-specific subsystem whose ``docs:`` (then ``paths``/``files``)
         declares the file;
      2. **convention** — ``docs/<name>/…``, ``docs/<name>.*``, or ``<name>.md`` whose name/stem
         == a subsystem name;
      3. else **None** — unlinked.

    Docs are ALWAYS informational, never a coverage gap. Deterministic: most-specific wins, equal
    specificity breaks to the sorted-first subsystem name.
    """
    rels = _walk_rels(
        project_root, lambda rel, ext: ext in config.DOC_EXTS, matcher, repo, include_gitignored
    )
    sub_names = set(subsystems)
    out: dict[str, str | None] = {}
    for rel in rels:
        owner = _explicit_owner(rel, subsystems, link_field="docs")
        if owner is None:
            owner = _convention_doc_owner(rel, sub_names)
        out[rel] = owner
    return out


def _convention_doc_owner(rel: str, sub_names: set[str]) -> str | None:
    """Convention-based owner for a doc file.

    ``docs/<name>/…`` maps to subsystem ``<name>``; otherwise ``docs/<name>.*`` or ``<name>.md``
    maps by stem. Returns None when no directory segment or stem matches a subsystem.
    """
    parts = rel.split("/")
    for i, part in enumerate(parts[:-1]):
        if part == "docs" and i + 1 < len(parts) - 1:
            area = parts[i + 1]
            if area in sub_names:
                return area
    stem = PurePosixPath(rel).stem
    if stem in sub_names:
        return stem
    return None


def subsystems_with_unsupported_source(
    project_root: Path,
    subsystems: dict[str, "SubsystemCompact"],
    matcher: IgnoreMatcher | None = None,
) -> set[str]:
    """Names of subsystems that own at least one UNSUPPORTED-language source file.

    A file is "unsupported source" when its extension is a known source language
    (:data:`config.KNOWN_SOURCE_EXTS`) but **no adapter handles it** — i.e. tree-sitter cannot
    extract its symbols (Go, Rust, Java, …). For such a file Bounds has *zero evidence* about which
    symbols a hand-authored ``exposes`` declares, so calibrate must not auto-remove those exposes and
    validate must not flag them as structural drift. This is the single shared signal both paths key
    off, so :mod:`bounds.calibrate` and :mod:`bounds.validate` can never disagree about an
    unsupported-language manifest.

    A directory/glob walk only — it inspects extensions, never reads source — so it stays cheap and
    adds no per-file source reads to the ``--quick`` budget. Ignore-aware (``.boundsignore``) and
    skips :data:`config.DEFAULT_IGNORES`; test files are excluded (a test in an unsupported language
    is not the public surface a manifest's exposes describes). Deterministic: returns a plain set the
    callers sort at their serialization boundary.
    """
    supported = supported_extensions()
    out: set[str] = set()
    for name in sorted(subsystems):
        sub = subsystems[name]
        for abs_path in iter_subsystem_files(project_root, sub, None):  # None => every extension
            ext = abs_path.suffix
            if ext in supported or ext not in config.KNOWN_SOURCE_EXTS:
                continue  # supported (verifiable) or not source code at all
            try:
                rel = abs_path.relative_to(project_root).as_posix()
            except ValueError:
                continue
            if is_test_file(rel):
                continue
            if matcher and matcher.matches(rel):
                continue
            out.add(name)
            break  # one unsupported-source file is enough to mark the subsystem
    return out


def mapping_coverage(
    project_root: Path,
    owned: set[str],
    matcher: IgnoreMatcher | None = None,
    repo: Path | None = None,
    include_gitignored: bool = False,
    subsystems: dict[str, "SubsystemCompact"] | None = None,
) -> dict:
    """How much of the repo's *library source code* Bounds actually mapped, an honest breakdown of
    what it could not, plus separate docs/tests linkage buckets — the metric that stops a polyglot
    repo from looking fully mapped while half of it is an unsupported language.

    Walks every non-ignored file (``config.DEFAULT_IGNORES`` + ``.boundsignore``), counts only files
    whose extension is in :data:`config.KNOWN_SOURCE_EXTS` (docs/config/assets are excluded so they
    can't dilute the %). **Test files are excluded from the source denominator entirely** (recognized
    via :func:`is_test_file`) and tracked in a separate ``tests`` bucket: a repo's tests can never
    drag the mapped % down or be flagged as an unmapped-source gap. Each non-test source file is:
      - **mapped** — owned by a subsystem (``rel in owned``),
      - **unowned-supported** — Bounds *has* an adapter for it, it's just not in any manifest's
        paths (fix: add it to a manifest — deterministically mappable),
      - **unsupported** — no adapter for that language yet (fix: hand-author/AI-author a manifest, or
        it waits for an adapter).
    Returns source counts, ``mapped_pct``, a sorted by-language breakdown of the unmapped source, and
    two informational buckets — ``tests`` and ``docs`` — each ``{total, linked, unlinked,
    unlinked_sample}`` computed via :func:`resolve_test_owners`/:func:`resolve_doc_owners`
    (``subsystems`` required for linkage; absent ⇒ all unlinked). Deterministic; safe to skip on the
    ``--quick`` hot path (callers gate it). Honors ``.gitignore`` when ``repo`` is given and
    ``include_gitignored`` is False, so the denominator matches the gitignore-aware file universe the
    rest of validation uses (a gitignored owned file is excluded from both numerator and denominator,
    never miscounted as unmapped).
    """
    supported = supported_extensions()
    # Collect source-code candidates first so gitignore can be applied in one batched call (parity
    # with the engine's owned-file gitignore filtering). Test files are bucketed separately below
    # and never enter the source denominator.
    candidates: list[tuple[str, str, str]] = []  # (rel, ext, lang)
    for abs_path in walk_supported(project_root, None):  # None => every file
        ext = abs_path.suffix
        lang = config.KNOWN_SOURCE_EXTS.get(ext)
        if lang is None:
            continue  # not source code (docs/config/assets) — out of the denominator
        rel = abs_path.relative_to(project_root).as_posix()
        if matcher and matcher.matches(rel):
            continue
        if is_test_file(rel):
            continue  # tests are NOT library source — counted in the `tests` bucket, never a gap
        candidates.append((rel, ext, lang))
    if not include_gitignored and repo is not None:
        from .. import gitutil  # lazy: keep extract/ free of a top-level gitutil import
        ignored = gitutil.gitignored(repo, [c[0] for c in candidates])
        candidates = [c for c in candidates if c[0] not in ignored]

    mapped = unowned_supported = unsupported = 0
    by_lang: dict[str, int] = {}
    unsupported_langs: set[str] = set()
    for rel, ext, lang in candidates:
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
    pct = round(mapped / total * 100, 1) if total else 100.0
    if total and mapped < total and pct >= 100.0:
        pct = 99.9  # never display a rounded 100% while a gap remains

    subs = subsystems or {}
    test_owners = resolve_test_owners(project_root, subs, matcher, repo, include_gitignored)
    doc_owners = resolve_doc_owners(project_root, subs, matcher, repo, include_gitignored)
    return {
        "files_source_total": total,
        "files_mapped": mapped,
        "files_unmapped": unowned_supported + unsupported,
        "mapped_pct": pct,
        "unmapped_unowned_supported": unowned_supported,
        "unmapped_unsupported_language": unsupported,
        "unmapped_by_language": dict(sorted(by_lang.items())),
        "unsupported_languages": sorted(unsupported_langs),
        "tests": _linkage_bucket(test_owners),
        "docs": _linkage_bucket(doc_owners),
    }


def _linkage_bucket(owners: dict[str, str | None]) -> dict:
    """Summarize a doc/test ownership map into ``{total, linked, unlinked, unlinked_sample}``.

    ``linked`` counts files whose owner is not None; ``unlinked_sample`` is up to ten sorted
    rel-posix paths of the unlinked files (a token-lean preview — the full set lives in the
    resolver). Sorted for determinism.
    """
    unlinked = sorted(rel for rel, owner in owners.items() if owner is None)
    linked = len(owners) - len(unlinked)
    return {
        "total": len(owners),
        "linked": linked,
        "unlinked": len(unlinked),
        "unlinked_sample": unlinked[:10],
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
