"""Bootstrap discovery: auto-generate manifests from an un-bounded codebase.

``bounds discover`` is the one-command onboarding path that replaces the 5-step manual
bootstrap. It walks the repo, groups source files into candidate subsystems by directory,
runs tree-sitter on each candidate, builds a cross-candidate import graph, and proposes a
``root.yaml`` plus one manifest per candidate. Default is dry-run (print the proposal);
``--apply`` writes the files; ``--merge <name> <path>...`` folds several directories into one
named subsystem; ``--namespace <ns>`` tags every candidate.

Heuristic (deterministic, zero LLM):

1. Walk the repo for supported non-test source files (honouring ignore dirs + ``.boundsignore``).
2. A **candidate** is any directory holding direct source files; score by direct-file count
   (high ≥5, medium 3-4, low <3 — low candidates are dropped unless merged). A directory
   whose files are predominantly SQL/Prisma schema (a migration set) is always kept,
   regardless of count: the schema fold collapses N migrations into one table surface, so
   the per-file count carries no signal there.
3. Tree-sitter each candidate's files → ``exposes`` (every exported symbol, ``verified: true``
   since extraction confirmed it), skipping ``@generated`` files.
4. Resolve cross-candidate imports → ``consumes`` edges (direct imports only).
5. Infer ``role``/``criticality`` from graph degree (a starting point the developer edits).

Calibrate shares the extraction engine to *reconcile* existing manifests; discover
*creates* them from scratch.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import yaml

from . import config, errors, gitutil, tsconfig
from .extract import adapter_for_language, supported_extensions
from .extract.scan import (
    extract_file,
    extract_project,
    is_framework_entry_file,
    is_test_file,
    is_test_symbol,
    iter_repo_source,
    mapping_coverage,
    resolve_doc_owners,
    resolve_test_owners,
)
from .ignore import IgnoreMatcher, load_matcher
from .manifest import loader as manifest_loader
from .models import SubsystemCompact
from .validate.checks import index_extracts, resolve_import
from .validate.schema import SCHEMA_LANGUAGES, schema_catalog


def run_discover(
    project_root: Path,
    *,
    apply: bool = False,
    namespace: str | None = None,
    merges: list[tuple[str, list[str]]] | None = None,
) -> dict:
    """Discover candidate subsystems and propose manifests (writing them when ``apply``)."""
    matcher = load_matcher(project_root)
    sources = iter_repo_source(project_root, matcher)
    # Skip git-ignored paths by default so build/dist dirs don't become garbage manifests.
    # Fail soft: in a non-git repo (no .git) gitignored() returns an empty set, leaving the
    # existing DEFAULT_IGNORES + .boundsignore behavior untouched.
    ignored = gitutil.gitignored(project_root, sources)
    if ignored:
        sources = [rel for rel in sources if rel not in ignored]
    sources = [rel for rel in sources if not is_test_file(rel)]

    repo = gitutil.repo_root(project_root) or project_root
    existing_root = None
    existing_subs: dict[str, SubsystemCompact] = {}
    existing_file_owner: dict[str, str] = {}
    existing_extracts: dict = {}
    existing_generated: set[str] = set()
    existing_owned: set[str] = set()
    existing_tests: set[str] = set()
    try:
        existing_root, existing_subs, _existing_issues = manifest_loader.load_all(project_root)
    except errors.BoundsError as exc:
        if exc.code != errors.E_MANIFEST_NOT_FOUND:
            raise
    if existing_subs:
        existing_file_owner, existing_extracts, existing_generated = extract_project(
            project_root, existing_subs, matcher
        )
        existing_owned = set(existing_file_owner)
        existing_tests = set(resolve_test_owners(project_root, existing_subs, matcher, repo))
        sources = [
            rel for rel in sources
            if rel not in existing_owned and rel not in existing_tests
        ]

    if not sources:
        if existing_subs:
            coverage = mapping_coverage(
                project_root,
                existing_owned,
                matcher,
                repo=repo,
                subsystems=existing_subs,
            )
            return {
                "mode": "discover",
                "applied": apply,
                "root": {
                    "version": config.SCHEMA_VERSION,
                    "project": project_root.resolve().name,
                    "languages": list(existing_root.languages) if existing_root else [],
                    "subsystems": sorted(existing_subs),
                },
                "candidates": [],
                "coverage": coverage,
                "written": [],
                "skipped": [],
                "notice": (
                    "no unmapped supported source found; existing Bounds manifests already own "
                    "the discoverable source. Run `bounds calibrate` to reconcile drift."
                ),
            }
        raise errors.BoundsError(
            errors.E_USAGE,
            "no supported source files found to discover",
            fix="run from a project root containing non-test .py/.ts/.js sources, "
            "or check your .boundsignore / .gitignore",
        )

    # Map each source file to its candidate subsystem (merges win over directory grouping).
    merges = merges or []
    file_to_candidate, candidate_files = _group(sources, merges)
    file_owner_for_imports = {
        **existing_file_owner,
        **file_to_candidate,
    }

    # Extract every candidate file once; include existing-owned extracts too so imports from newly
    # discovered source can resolve to already-materialized subsystems without creating duplicates.
    extract_sources = sorted(set(sources))
    extracts: dict = dict(existing_extracts)
    generated: set[str] = set(existing_generated)
    for rel in extract_sources:
        result, is_gen = extract_file(project_root, rel)
        if is_gen:
            generated.add(rel)
        if result is not None:
            extracts[rel] = result
    known_noext, suffix_index = index_extracts(extracts)  # one shared projection (built once)

    # Cross-candidate consume edges (direct imports only). TS path aliases (@/…) resolve too.
    aliases = tsconfig.load(project_root)
    consumes: dict[str, set[str]] = {c: set() for c in candidate_files}
    for rel, result in extracts.items():
        owner = file_to_candidate.get(rel)
        if owner is None:
            continue
        for imp in result.imports:
            target = resolve_import(rel, imp.module, known_noext, suffix_index, aliases)
            tgt_owner = file_owner_for_imports.get(target) if target else None
            if tgt_owner and tgt_owner != owner:
                consumes[owner].add(tgt_owner)
    consumed_by: dict[str, int] = {c: 0 for c in candidate_files}
    for owner, deps in consumes.items():
        for dep in deps:
            consumed_by[dep] = consumed_by.get(dep, 0) + 1

    # Build proposals.
    candidates: list[dict] = []
    kept: list[str] = []
    for name in sorted(candidate_files):
        files = sorted(candidate_files[name])
        is_schema = _is_schema_candidate(files, extracts)
        # A migration set folds to one table surface, so file count is meaningless there:
        # schema dirs are always kept (even a lone schema.sql), never count-dropped.
        score = "schema" if is_schema else _score(len(files))
        merged = any(name == m[0] for m in merges)
        if score == "low" and not merged:
            candidates.append({"name": name, "files": len(files), "score": score, "dropped": True})
            continue
        kept.append(name)
        exposes = _exposes_for(files, extracts, generated)
        cby = consumed_by.get(name, 0)
        cand = {
            "name": name,
            "files": len(files),
            "score": score,
            "dropped": False,
            "role": _infer_role(cby, len(consumes.get(name, set()))),
            "criticality": _infer_criticality(cby),
            "paths": _candidate_paths(name, files),
            "exposes": exposes,
            "consumes": sorted(consumes.get(name, set())),
        }
        if is_schema:
            # SQL/Prisma DDL never produces *exported* symbols (the table surface is the
            # fold's job, not a declared export), so ``exposes`` is legitimately empty here.
            # Surface the folded table count so an --apply run visibly maps the schema
            # instead of looking like an empty subsystem.
            cand["schema"] = True
            cand["tables"] = len(schema_catalog(name, extracts, file_to_candidate))
        if namespace:
            cand["namespace"] = namespace
        candidates.append(cand)

    # Drop consume-edges to candidates that were not kept (low-score, dropped): writing a
    # `consumes: <name>` for a subsystem discover never materializes would make `validate` raise a
    # self-inflicted E_UNRESOLVED_REFERENCE on a fresh run. Only edges between kept subsystems stand.
    kept_set = set(kept)
    materialized_set = kept_set | set(existing_subs)
    for cand in candidates:
        if cand.get("consumes"):
            cand["consumes"] = [c for c in cand["consumes"] if c in materialized_set]

    # Auto-link tests (and docs) by convention so a fresh discover already maps them — the user does
    # very little. Conservative: only convention-confident files, collapsed to directory globs when a
    # directory maps wholly to one subsystem (token-lean). Resolved against the KEPT subsystems.
    _link_tests_docs(project_root, matcher, gitutil.repo_root(project_root) or project_root, candidates)

    # Languages actually present in the extracted source — so root.yaml reflects the repo instead
    # of init's hardcoded `[python]` default (a pure-TS/Prisma repo must not claim to be Python).
    detected_languages = sorted({r.language for r in extracts.values() if getattr(r, "language", None)})
    root_proposal = {
        "version": config.SCHEMA_VERSION,
        "project": project_root.resolve().name,
        "languages": detected_languages,
        "subsystems": kept,
    }

    applied = False
    written: list[str] = []
    skipped: list[str] = []
    if apply and kept:
        written, skipped = _write(project_root, root_proposal, candidates)
        applied = True

    # Mapping coverage: how much SOURCE the proposal actually covers — surfaced right here so a
    # polyglot repo can't look fully discovered while an unsupported language sits unmapped. Test
    # files are excluded from the source denominator and reported in their own bucket (subsystems
    # passed so the doc/test linkage buckets reflect the discovered subsystems' paths).
    owned = {rel for rel, cand in file_to_candidate.items() if cand in kept_set}
    cov_subs = dict(existing_subs)
    cov_subs.update({
        c["name"]: SubsystemCompact(name=c["name"], paths=list(c.get("paths") or []),
                                    tests=list(c.get("tests") or []), docs=list(c.get("docs") or []))
        for c in candidates if not c.get("dropped")
    })
    coverage = mapping_coverage(project_root, owned | existing_owned, matcher, repo=repo, subsystems=cov_subs)

    result = {
        "mode": "discover",
        "applied": applied,
        "root": root_proposal,
        "candidates": candidates,
        "coverage": coverage,
        "written": sorted(written),
        "skipped": sorted(skipped),
    }
    if coverage["files_unmapped"] > 0:
        result["next_step"] = (
            f"mapped {coverage['mapped_pct']}% of source; "
            f"{coverage['files_unmapped']} file(s) unmapped"
            + (f" in unsupported languages ({', '.join(coverage['unsupported_languages'])})"
               if coverage["unsupported_languages"] else "")
            + ". To reach 100%: `bounds init --subsystem <name>` and add the files to its `paths`, "
            "or have an AI author the manifest in the same format — then `bounds validate`. "
            "See docs/coverage.md."
        )
    notice = _apply_notice(applied, written, skipped)
    if notice is not None:
        result["notice"] = notice
    return result


def _apply_notice(applied: bool, written: list[str], skipped: list[str]) -> str | None:
    """A human-facing line explaining a write run whose effect isn't obvious from the lists.

    Without this, an ``--apply`` run that wrote nothing because every candidate manifest
    already existed looks like a silent no-op. Returns ``None`` for dry-run and for the
    ordinary case where new manifests were written; otherwise names the 0-written outcome.
    ``written`` always carries ``root.yaml`` (merged each run), so the per-candidate manifest
    count is what was written minus that root file.
    """
    if not applied:
        return None
    manifests_prefix = f"{config.BOUNDS_DIR}/{config.MANIFESTS_DIR}/"
    new_manifests = [w for w in written if w.startswith(manifests_prefix)]
    if new_manifests:
        return None
    if skipped:
        return (
            f"wrote 0 new manifests; {len(skipped)} already exist "
            "-- run `bounds calibrate` to reconcile them against the current source"
        )
    return "wrote 0 new manifests (no candidate subsystems to create)"


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------
def _group(
    sources: list[str], merges: list[tuple[str, list[str]]]
) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Assign each source file to a candidate subsystem. Returns (file->candidate, candidate->files)."""
    merge_index: list[tuple[str, list[str]]] = [
        (name, [Path(p).as_posix().rstrip("/") for p in paths]) for name, paths in merges
    ]
    file_to_candidate: dict[str, str] = {}
    candidate_files: dict[str, set[str]] = {}

    # 1. Files claimed by an explicit --merge-into win over directory grouping.
    remaining: list[str] = []
    for rel in sources:
        mname = _merge_match(rel, merge_index)
        if mname is not None:
            file_to_candidate[rel] = mname
            candidate_files.setdefault(mname, set()).add(rel)
        else:
            remaining.append(rel)

    # 2. Group the rest by their parent directory (full POSIX path).
    by_dir: dict[str, list[str]] = {}
    for rel in remaining:
        parent = Path(rel).parent
        dkey = "" if parent in (Path("."), Path("")) else parent.as_posix()
        by_dir.setdefault(dkey, []).append(rel)

    # 2b. Fold a module's structural sub-directories (dto/, services/, entities/, …) into the
    #     module dir so a framework module (e.g. NestJS auth/{dto,services,auth.module.ts}) becomes
    #     ONE `auth` subsystem, not five confusingly-named ones (auth-dto, auth-services, …). Only
    #     folds when the parent is itself a candidate, so a standalone structural dir is preserved.
    dir_set = set(by_dir)
    folded: dict[str, list[str]] = {}
    for dkey, files in by_dir.items():
        folded.setdefault(_fold_target(dkey, dir_set), []).extend(files)
    by_dir = folded

    # 3. Name each directory: its basename when unique, else a path-derived name so two
    #    same-named dirs in different trees (e.g. a/utils and b/utils) stay SEPARATE
    #    subsystems instead of being silently fused.
    names = _name_dirs(sorted(by_dir))
    for dkey, files in by_dir.items():
        name = names[dkey]
        for rel in files:
            file_to_candidate[rel] = name
            candidate_files.setdefault(name, set()).add(rel)
    return file_to_candidate, candidate_files


# Directory basenames that are a module's implementation sub-parts, not subsystem boundaries of
# their own. When one sits directly under another candidate directory, its files fold into that
# parent — so a framework module's `dto/`/`services/`/… don't each become a separate (and
# confusingly-named) subsystem. Conservative by design: folding requires the parent to already be
# a candidate, so a deliberate standalone `src/types` (whose parent has no direct sources) is kept.
_STRUCTURAL_SUBPARTS = frozenset({
    "dto", "dtos", "entity", "entities", "model", "models", "schema", "schemas",
    "interface", "interfaces", "type", "types", "constant", "constants", "enum", "enums",
    "service", "services", "controller", "controllers", "guard", "guards", "pipe", "pipes",
    "decorator", "decorators", "middleware", "middlewares", "repository", "repositories",
    "dao", "util", "utils", "helper", "helpers", "validator", "validators",
})


def _fold_target(dkey: str, dir_set: set[str]) -> str:
    """Walk a structural sub-dir up to the module candidate it belongs to (else return it as-is).

    ``src/auth/dto`` → ``src/auth`` when ``src/auth`` is a candidate; repeats so
    ``src/auth/dto/nested`` collapses too. Stops at the first non-structural dir or when the parent
    isn't a candidate, so unrelated trees never fuse.
    """
    cur = dkey
    seen: set[str] = set()
    while cur and cur not in seen:
        seen.add(cur)
        base = cur.rsplit("/", 1)[-1] if "/" in cur else cur
        parent = cur.rsplit("/", 1)[0] if "/" in cur else ""
        if base in _STRUCTURAL_SUBPARTS and parent in dir_set:
            cur = parent
        else:
            break
    return cur


def _merge_match(rel: str, merge_index: list[tuple[str, list[str]]]) -> str | None:
    for name, paths in merge_index:
        for p in paths:
            if rel == p or rel.startswith(p + "/"):
                return name
    return None


def _basename(dkey: str) -> str:
    return "root" if dkey == "" else dkey.rsplit("/", 1)[-1]


def _name_dirs(dirs: list[str]) -> dict[str, str]:
    """Map each directory to a candidate name. Unique basenames win; colliding basenames get
    a deterministic path-derived name (``b/utils`` -> ``b-utils``) so unrelated trees never fuse."""
    base_counts: dict[str, int] = {}
    for d in dirs:
        base_counts[_basename(d)] = base_counts.get(_basename(d), 0) + 1

    names: dict[str, str] = {}
    used: set[str] = set()
    for d in dirs:  # pass 1: unique basenames
        base = _basename(d)
        if base_counts[base] == 1:
            names[d] = base
            used.add(base)
    for d in dirs:  # pass 2: disambiguate the colliders
        if d not in names:
            names[d] = _disambiguate(d, used)
            used.add(names[d])
    return names


def _disambiguate(dkey: str, used: set[str]) -> str:
    """Grow a name from the path tail until unique: utils -> b-utils -> a-b-utils."""
    segs = [s for s in dkey.split("/") if s] or ["root"]
    for take in range(2, len(segs) + 1):
        cand = "-".join(segs[-take:])
        if cand not in used:
            return cand
    return "-".join(segs)


def _candidate_paths(name: str, files: list[str]) -> list[str]:
    """The minimal set of parent directories owning a candidate's files (deduped, sorted).

    A dir already covered by an ancestor in the set is dropped, so a folded module collapses to a
    single root path (``src/auth`` rather than ``src/auth`` + ``src/auth/dto`` + ``src/auth/services``).
    """
    dirs = sorted({Path(f).parent.as_posix() for f in files})
    covering: list[str] = []
    for d in dirs:  # sorted ⇒ an ancestor always precedes its descendants
        if not any(d == c or d.startswith(c + "/") for c in covering):
            covering.append(d)
    return covering or [name]


def _link_tests_docs(
    project_root: Path,
    matcher: IgnoreMatcher | None,
    repo: Path,
    candidates: list[dict],
) -> None:
    """Attach convention-detected ``tests``/``docs`` link entries to each kept candidate, in place.

    Builds a :class:`SubsystemCompact` per kept candidate (only ``paths`` matter for resolution),
    runs :func:`resolve_test_owners`/:func:`resolve_doc_owners` against them, groups the linked files
    by owner, and collapses each owner's files to minimal directory globs (token-lean) via
    :func:`_collapse_link_paths`. Unlinked files are skipped (conservative — only confident maps).
    Deterministic: sorted throughout.
    """
    kept = [c for c in candidates if not c.get("dropped")]
    if not kept:
        return
    subs = {
        c["name"]: SubsystemCompact(name=c["name"], paths=list(c.get("paths") or []))
        for c in kept
    }
    test_owners = resolve_test_owners(project_root, subs, matcher, repo)
    doc_owners = resolve_doc_owners(project_root, subs, matcher, repo)
    by_owner_tests: dict[str, list[str]] = {}
    for rel, owner in test_owners.items():
        if owner is not None:
            by_owner_tests.setdefault(owner, []).append(rel)
    by_owner_docs: dict[str, list[str]] = {}
    for rel, owner in doc_owners.items():
        if owner is not None:
            by_owner_docs.setdefault(owner, []).append(rel)
    for cand in kept:
        tests = _collapse_link_paths(sorted(by_owner_tests.get(cand["name"], [])), test_owners, cand["name"])
        docs = _collapse_link_paths(sorted(by_owner_docs.get(cand["name"], [])), doc_owners, cand["name"])
        if tests:
            cand["tests"] = tests
        if docs:
            cand["docs"] = docs


def _collapse_link_paths(files: list[str], owners: dict[str, str | None], owner: str) -> list[str]:
    """Collapse a sorted list of linked files to minimal directory globs where one dir maps cleanly.

    A directory is substituted for its individual files only when EVERY linked file in ``owners``
    under that directory belongs to ``owner`` (so a shared ``tests/`` dir split across subsystems is
    never over-claimed). Files in a not-cleanly-owned directory are listed individually. Keeps
    manifests token-lean (prefer ``tests/extract`` over enumerating every file) while staying correct.
    Deterministic: sorted output, ancestor-covers-descendant dedup.
    """
    # Directories whose every linked file (across all owners) belongs to `owner` are collapsible.
    files_by_dir: dict[str, list[str]] = {}
    for rel in files:
        files_by_dir.setdefault(Path(rel).parent.as_posix(), []).append(rel)
    all_by_dir: dict[str, list[tuple[str, str | None]]] = {}
    for rel, o in owners.items():
        all_by_dir.setdefault(Path(rel).parent.as_posix(), []).append((rel, o))
    out: list[str] = []
    for d in sorted(files_by_dir):
        siblings = all_by_dir.get(d, [])
        clean = all(o == owner for _rel, o in siblings)
        owner_named_dir = PurePosixPath(d).name == owner
        if clean and owner_named_dir and d not in ("", "."):
            out.append(d)  # whole directory maps to this owner — collapse to the dir glob
        else:
            out.extend(files_by_dir[d])  # mixed/owner-shared dir — list files individually
    # Dedup any dir already covered by an ancestor dir in the set (sorted ⇒ ancestor precedes).
    covering: list[str] = []
    for entry in sorted(out):
        if not any(entry == c or entry.startswith(c + "/") for c in covering):
            covering.append(entry)
    return covering


# ---------------------------------------------------------------------------
# Scoring + inference
# ---------------------------------------------------------------------------
def _score(n: int) -> str:
    """Confidence that a directory is a real subsystem, from its direct-file count.

    No upper bound: a large directory is a *legitimately large* subsystem (e.g. a flat
    ``components/`` tree), not garbage — dropping it silently would hide the repo's biggest
    surfaces from the map, the opposite of discovery's job. Junk dirs are excluded upstream
    by ``DEFAULT_IGNORES`` + ``.gitignore`` + generated-file detection, not by a size cap.
    """
    if n >= 5:
        return "high"
    if n >= 3:
        return "medium"
    return "low"


def _schema_extensions() -> set[str]:
    """File extensions whose adapter is a schema (SQL/Prisma) language, from the registry."""
    exts: set[str] = set()
    for lang in SCHEMA_LANGUAGES:
        adapter = adapter_for_language(lang)
        if adapter is not None:
            exts.update(adapter.extensions)
    return exts


def _is_schema_candidate(files: list[str], extracts: dict) -> bool:
    """True when a candidate's files are predominantly SQL/Prisma schema DDL.

    Such a directory (a migration set, or a ``schema.sql`` snapshot) is always kept: the
    subsystem-level schema fold collapses any number of migrations into a single table
    surface, so the per-file count that scores ordinary code dirs carries no signal here.

    Classification falls back to file EXTENSION when a file isn't in ``extracts`` — a
    wholly-unparsable migration extracts to ``None`` and would otherwise vanish from the
    language tally, mislabeling a real migration dir as non-schema and dropping it. Counting
    such a ``.sql``/``.prisma`` file as schema preserves the "always keep schema dirs" guarantee
    even when some migrations don't parse.
    """
    schema_exts = _schema_extensions()
    schema_n = 0
    for rel in files:
        result = extracts.get(rel)
        if result is not None:
            is_schema = result.language in SCHEMA_LANGUAGES
        else:  # extraction failed (e.g. unparsable SQL) — fall back to the extension
            is_schema = Path(rel).suffix.lower() in schema_exts
        schema_n += is_schema
    return schema_n >= 1 and schema_n * 2 >= len(files)  # schema-dominant (≥ half)


def _infer_role(consumed_by: int, consumes_count: int) -> str:
    """Heuristic starting role from graph degree (developer edits as needed)."""
    if consumed_by >= 3:
        return "library"
    if consumes_count > 0 and consumed_by == 0:
        return "service"
    if consumes_count > 0 and consumed_by > 0:
        return "connector"
    return "library"


def _infer_criticality(consumed_by: int) -> str:
    if consumed_by >= 3:
        return "core"
    if consumed_by >= 1:
        return "connector"
    return "leaf"


def _exposes_for(files: list[str], extracts: dict, generated: set[str]) -> list[dict]:
    """Every exported symbol across a candidate's files, verified by tree-sitter.

    Test cases (``test_*`` functions / ``Test*`` classes in a test file) are excluded: a test runner
    discovers them by naming convention, nothing *imports* them, so listing all of them as a public
    surface is wrong and bloats the manifest (a 400-test dir would yield an 800-line `exposes`).

    The public surface is exactly ``sym.exported`` as the adapter computed it — so discover and
    validate agree by construction. For Python that means a literal ``__all__`` is honoured (only its
    members are exposed; a leading-underscore name listed in ``__all__`` IS public, and a public-cased
    name omitted from ``__all__`` is NOT); when ``__all__`` is absent the underscore rule applies.
    We must NOT re-filter on a leading underscore here — that would drop a deliberately-``__all__``-ed
    ``_name`` and desync the two surfaces.
    """
    seen: dict[str, dict] = {}
    for rel in files:
        if rel in generated or rel not in extracts:
            continue
        if is_framework_entry_file(rel):
            continue  # a Next.js page/layout/route entry — framework-invoked, not a consumable API
        is_test = is_test_file(rel)
        for sym in extracts[rel].symbols:
            if not sym.exported:
                continue
            if is_test and is_test_symbol(sym.name, sym.kind):
                continue  # a test case is not a consumable interface
            # When the same exported name appears in several files (a barrel `export { Foo }
            # from "./foo"` re-exporting `foo.ts`'s `class Foo`), keep the entry whose kind is
            # a real declaration over a bare re-export `unknown`, so `kind`/`file` point at the
            # definition. First-seen wins among equally-declared entries (files are sorted, so
            # this is deterministic).
            prior = seen.get(sym.name)
            if prior is not None and not (prior["kind"] == "unknown" and sym.kind != "unknown"):
                continue
            seen[sym.name] = {"name": sym.name, "kind": sym.kind, "file": rel, "verified": True}
    return [seen[n] for n in sorted(seen)]


# ---------------------------------------------------------------------------
# Apply (write root.yaml + manifests)
# ---------------------------------------------------------------------------
def _write(project_root: Path, root_proposal: dict, candidates: list[dict]) -> tuple[list[str], list[str]]:
    """Write .bounds/root.yaml (merging into any existing subsystems list) and per-candidate manifests.

    Existing manifest files are never overwritten (reported as skipped) so re-running discover
    is safe on a partially-bounded repo.
    """
    cfg = project_root / config.BOUNDS_DIR
    (cfg / config.MANIFESTS_DIR).mkdir(parents=True, exist_ok=True)
    # Ensure the regenerable, binary cache is gitignored whenever discover scaffolds .bounds/ —
    # same self-contained .gitignore init writes (shared template, idempotent).
    config.ensure_bounds_gitignore(cfg)
    written: list[str] = []
    skipped: list[str] = []

    # Merge subsystems into an existing root.yaml if present. Re-read the RAW mapping (not the
    # RootManifest projection) so developer-authored keys — notably custom `roles:`/`criticality:`
    # — are preserved across the rewrite instead of being silently dropped.
    root_path = cfg / config.ROOT_FILE
    existing_subs: list[str] = []
    root_doc = dict(root_proposal)
    if root_path.is_file():
        try:
            raw = yaml.safe_load(root_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            raw = None
        if isinstance(raw, dict):
            root_doc = dict(raw)
            existing_subs = [str(s) for s in (raw.get("subsystems") or [])]
    root_doc["subsystems"] = sorted(set(existing_subs) | set(root_proposal["subsystems"]))
    # Detection is authoritative over init's `[python]` placeholder: when discover actually saw
    # source, overwrite languages with what it found (only when non-empty, so a no-op run can't
    # blank a hand-set value).
    if root_proposal.get("languages"):
        root_doc["languages"] = root_proposal["languages"]
    root_doc.setdefault("version", root_proposal["version"])
    root_doc.setdefault("project", root_proposal["project"])
    root_doc.setdefault("entry_points", [])
    root_path.write_text(yaml.safe_dump(root_doc, sort_keys=False), encoding="utf-8")
    written.append(root_path.relative_to(project_root).as_posix())

    for cand in candidates:
        if cand.get("dropped"):
            continue
        man_path = cfg / config.MANIFESTS_DIR / f"{cand['name']}.yaml"
        rel = man_path.relative_to(project_root).as_posix()
        if man_path.is_file():
            skipped.append(rel)
            continue
        doc = {
            "name": cand["name"],
            "role": cand["role"],
            "criticality": cand["criticality"],
        }
        if cand.get("namespace"):
            doc["namespace"] = cand["namespace"]
        doc["paths"] = cand["paths"]
        doc["exposes"] = [{"name": e["name"], "kind": e["kind"]} for e in cand["exposes"]]
        doc["consumes"] = [{"subsystem": s} for s in cand["consumes"]]
        # Convention-linked tests/docs (only when discover confidently mapped some) — already
        # sorted/collapsed, so a fresh discover links them and the user does very little.
        if cand.get("tests"):
            doc["tests"] = cand["tests"]
        if cand.get("docs"):
            doc["docs"] = cand["docs"]
        man_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        written.append(rel)
    return written, skipped
