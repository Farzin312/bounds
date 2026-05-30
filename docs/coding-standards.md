# Bounds coding standards

A **reviewable, diff-checkable** checklist. Every item here can be confirmed against a PR by a human
or an agent — when you review (or self-review) a change, walk the blocking sections and confirm each.
This is the project layer on top of the universal review baseline; on any conflict, **this file wins**.

These rules are not bureaucracy — they are the invariants that make Bounds *Bounds*: deterministic,
zero-LLM on the structural path, token-lean, and honest. Bounds exists to keep a codebase from
drifting from its declared intent; this file keeps *Bounds itself* from drifting from its own.

> **How to use this in review.** For a structural change (anything under `extract/`, `validate/`,
> `cache/`, `manifest/`, `cli.py`, `output.py`, `models.py`), confirm every **(blocking)** section.
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
      `overview.edges` (`cli.py`). Collections inside payloads are sorted by their producer.
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

- [ ] No business logic in `cli.py` beyond arg-parse + a single `go()` closure per command. Tier-1/2
      assembly lives in `describe.py`; validation in `validate/`; extraction in `extract/`.
- [ ] The filesystem→extraction primitives have **one home, `extract/scan.py`**, reused everywhere —
      never copied:
      - `walk_supported` — the recursive source walk (symlink-safe).
      - `iter_subsystem_files` — a subsystem's owned files (engine + describe agree).
      - `iter_repo_source` — every repo source file.
      - `extract_project` — project-wide `(file_owner, extracts, generated)` (calibrate, `impact
        --verify`, `where`).
      - `extract_file` / `strip_ext` / `in_default_ignores`.
- [ ] Import resolution has **one home** — `validate.checks.resolve_import` + `build_suffix_index`
      (O(1), no `O(files × imports × files)` scans). Build the suffix index **once** per call site
      and pass it into the loop.
- [ ] Before adding a helper, grep for an existing one. A second copy of a walk, an extractor, a
      resolver, or a severity literal is a blocking review finding.

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

*When a review finds a real issue that gets fixed, leave one short `SUPERVISOR-NOTE (review, <date>):
<what> — <why>.` at the fix site so later agents understand the decision. Respect existing notes
before changing annotated code; delete a note only when a later change wholly replaces what it
annotated.*
