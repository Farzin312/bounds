# Bounds coding standards

A **reviewable, diff-checkable** checklist. Every item here can be confirmed against a PR by a human
or an agent — when you review (or self-review) a change, walk the blocking sections and confirm each.
This is the project layer on top of the universal review baseline; on any conflict, **this file wins**.

These rules are not bureaucracy — they are the invariants that make Bounds *Bounds*: deterministic,
zero-LLM on the structural path, token-lean, and honest. Bounds exists to keep a codebase from
drifting from its declared intent; this file keeps *Bounds itself* from drifting from its own.

> **How to use this in review.** For a structural change (anything under `core/extract/`,
> `core/validate/`, `shared/cache/`, `core/manifest/`, `cli/`, `shared/output.py`, or
> `shared/models.py`), confirm every **(blocking)** section.
> For docs-only changes, the Output-contract honesty rules still apply. New behavior without a test
> is a blocking failure on its own.

---

## 1. Determinism *(blocking)*

Same inputs → byte-identical output, every run, every machine.

- [ ] No `datetime` / `time` / wall-clock, `random`, `uuid`, or other nondeterministic source feeds
      any **hash** or any **serialized field**. (Timing like `duration_ms` is the one allowed
      exception, and it is excluded from golden comparisons.)
- [ ] No reliance on `set` iteration order or `dict` insertion order in serialized output. **Sort at
      the serialization boundary** — including `stats` keys (`models.ValidationReport.to_dict`) and
      `overview.edges` (`core/describe.py`). Collections inside payloads are sorted by their producer.
- [ ] The cache `updated_at` column is always written empty (no timestamp ever reaches an artifact).
- [ ] A change touching any command's JSON ships (or is covered by) a **golden / byte-stability**
      test asserting two runs are identical (minus `duration_ms`).

## 2. Fail-soft / report-hard *(blocking)*

A bad *file* degrades to an `Issue`; only a bad *project* aborts.

- [ ] A single unreadable / oversized / unparsable file → an `Issue` (`E_EXTRACTION_FAILED` /
      `E_UNSUPPORTED_LANGUAGE`), **never** a crash and **never** a silent `continue`/`except: pass`
      on an *owned/declared* file. A dropped owned file makes a real symbol look like
      `verified:false` — that is the bug this rule prevents.
- [ ] Only genuinely fatal conditions raise `BoundsError`: no `.bounds/`, bad YAML, unknown
      subsystem, bad usage. Nothing else raises out of a command body.
- [ ] Unresolved static analysis is surfaced (≥ `info` or a counted stat), never hidden behind an
      exact number that silently omits the unresolved set (e.g. the `coverage` block).

## 3. Zero-LLM on the structural path *(blocking)*

- [ ] No model / network / API client is imported or called under `extract/`, `validate/`, `cache/`,
      or `manifest/`. The only LLM tier is Tier-3 `describe --deep`, which is **stubbed**. If your
      change adds a network or model dependency to a structural path, it is wrong by construction.

## 4. Resource bounds *(blocking for new walks / traversals / subprocesses)*

- [ ] A file is size-checked against `config.MAX_FILE_BYTES` **before** `read_bytes()` — never read a
      giant blob into memory.
- [ ] Graph / tree traversals are **iterative or depth-capped**, never unbounded recursion (a deep
      input must not raise `RecursionError`).
- [ ] Every recursive filesystem walk goes through `extract.scan.walk_supported` (symlink-cycle-safe,
      visited-realpath guarded). Do **not** add a bare `Path.rglob('*')` source walk.
- [ ] Every subprocess passes `timeout=` and fails soft on `TimeoutExpired` (see `gitutil`).
- [ ] SQLite connections set `PRAGMA busy_timeout`; cache writes are best-effort and catch
      `sqlite3.Error` (a locked cache never fails a validation).

## 5. Paths

- [ ] `pathlib` everywhere. Store and compare repo-relative paths in **posix form** (`as_posix()`).
      No `os.sep` concatenation, no hard-coded `/` or `\`.
- [ ] Walks skip `config.DEFAULT_IGNORES` (handled for you inside `walk_supported`).

## 6. Output contract *(blocking)*

- [ ] Exactly **one JSON object** per command to stdout. Fatal errors print `{"error":{code,message,fix}}`.
- [ ] `--human` and `--ci` render the **same data** — they re-render `to_dict()`, never expose a field
      the JSON omits and never omit one it carries. A new JSON field gets a matching line in the
      relevant `output.py` renderer **in the same PR**.
- [ ] JSON changes are **additive only**: never rename or repurpose an existing key (agents parse it).
      A semantics change to an existing key's *value* is allowed when documented; a key *rename* is not.
      (Example: `validation_status` became subsystem-scoped — the key stayed, `project_status` was added.)
- [ ] Token-lean: no body dumps, no cache dumps, no whole-map payloads where a targeted one suffices.
- [ ] Every JSON-shape change is mirrored in **`ARCHITECTURE.md`** (the shapes + error table) in the
      same PR.

## 7. Error codes *(blocking)*

- [ ] `errors.py` is **append-only**: never renumber, rename, or repurpose an existing code; only add.
- [ ] Severity has a **single source** — `errors.SEVERITY`. Checks construct issues through the
      `_issue(...)` helper (defaulting severity from the table); an explicit override is only for a
      genuinely context-dependent severity (e.g. an undeclared export surfaced at `info`).

## 8. Structure / DRY — one home per concept *(blocking)*

The thing Bounds preaches, applied to Bounds. **Do not define the same function/class/walk twice.**

- [ ] No business logic in `cli/main.py`; it owns Click registration and delegates to
      `cli/{read,setup,drift,maintain}.py`. Tier-1/2 assembly lives in `core/describe.py`;
      validation in `core/validate/`; extraction in `core/extract/`.
- [ ] Internal imports obey the downward DAG (`shared` → `core` → `agents`/`maintenance` → `cli`).
      Run `tests/meta/test_layering.py`; never waive an upward import as a convenience.
- [ ] CLI and agent orchestration modules stay under 500 lines. For algorithm-heavy modules, treat
      500 lines as a review signal and split by responsibility, not by arbitrary chunks.
- [ ] The filesystem→extraction primitives have **one home, `extract/scan.py`**, reused everywhere —
      never copied:
      - `walk_supported` — the recursive source walk (symlink-safe).
      - `iter_subsystem_files` — a subsystem's owned files (engine + describe agree).
      - `iter_repo_source` — every repo source file.
      - `extract_project` — project-wide `(file_owner, extracts, generated)` (calibrate, `impact
        --verify`, `where`).
      - `extract_file` / `strip_ext` / `in_default_ignores`.
      - `read_source_bytes` / `is_oversized` — the **size-guard + read + OSError mechanism**.
        `extract_file` and the validation engine both call these; they differ only in *policy*
        (silent skip vs. a loud warning `Issue` on owned files). Never re-inline a
        `stat().st_size > MAX_FILE_BYTES` check or a bare `read_bytes()` try/except.
- [ ] Import resolution has **one home** — `validate.checks.resolve_import` + `build_suffix_index`
      (O(1), no `O(files × imports × files)` scans). Build the suffix index **once** per call site
      and pass it into the loop.
- [ ] Schema (SQL/Prisma) has fixed homes: `validate.schema` is the only migration fold —
      `_fold_subsystem_schema` for tables/columns and `fold_subsystem_objects` for the non-table
      surface (functions/views/indexes/triggers/types dedup; **policies + RLS are an ordered
      create/alter/drop / enable-disable-force fold**, never a flat dedup — a dropped policy must net
      out). `schema_objects` / `schema_rls_posture` read that fold; don't re-walk symbols to recount.
      `hash_schema_catalog(catalog)` hashes an **already-built** catalog (a caller holding a catalog
      reuses it — never call `schema_structure_hash` after `schema_catalog` and fold twice);
      `extract.base.canonical_columns` is the one column dedup/sort for every schema adapter's
      `metadata["columns"]`.
- [ ] SQL extraction lives only in `extract.sql`: grammar-native DDL via `_grammar_symbols`
      (descends into transaction containers via `_iter_statements`), Postgres RLS via the
      comment/string/body-masked `_recover_rls` (never a raw whole-file regex — mask first, via
      `_mask_spans`), and `_table_ref` canonicalises every table reference to its bare name. A
      non-DDL parse error (cron/seed/grant) is never `E_SCHEMA_UNPARSED`. When extraction output
      changes for unchanged source, **bump `config.STATE_VERSION`** so stale `cache.db` rebuilds.
- [ ] "Does this file belong to language X?" has one home — `extract.registry.is_language_file`
      (or the adapter's own `extensions`). Never hardcode an extension list at a call site.
- [ ] Loading feedback (spinners) has one home — `cli._progress(message)`. Wrap **only** the heavy
      compute, never `output.emit`. No per-command spinner re-implementation.
- [ ] Before adding a helper, grep for an existing one. A second copy of a walk, an extractor, a
      resolver, a hash, a column-dedup, an extension list, or a severity literal is a blocking review
      finding.

## 9. New language adapter recipe

To add a language:

1. Subclass `extract.base.LanguageAdapter`; set `language_name` and `extensions`.
2. Implement `extract(rel_path, source)` with a top-level `try/except Exception → make_result(error=...)`
   (fail soft — never raise).
3. Build the result via `base.make_result(...)` so both hashes are computed consistently.
4. Register it in `extract/registry.py`.
5. Add a fixture under `tests/fixtures/` and a test asserting extracted symbols + imports.

## 10. Tests for new behavior *(blocking)*

- [ ] Every new behavior / command / check ships a test. Every bug fix ships a **regression test that
      asserts the JSON shape**, not just internal state.
- [ ] Performance-sensitive paths keep the budget: `bounds validate --quick` validation logic stays
      **< 200ms** on a typical repo. Don't add a full-tree walk or extra subprocess to the quick path.

---

*When a fix turns on a non-obvious decision, leave a short comment at the site explaining **why**
(not just what), so the next contributor doesn't undo it. Keep these comments accurate and remove
them once a later change makes them obsolete.*
