# Languages & platforms

*What Bounds extracts and validates per language, and where it runs.*

[← Docs index](./README.md) · [Bounds README](../README.md)

---

## Three support tiers

Bounds is honest about exactly how far it can take each language, in three tiers:

- **Fully supported** — Python, TypeScript/JavaScript, SQL migrations, Prisma. Extracted **and
  verified** by tree-sitter / a deterministic parser: `exposes` are confirmed (`verified: true`),
  drift is caught, tables are folded.
- **Partially supported** — a *fully-supported* language with a **documented, self-reported gap**.
  Examples: SQL where some Postgres DDL is unparseable (`E_SCHEMA_UNPARSED`, the rest of the file
  still folds), or a TS/JS ORM/barrel construct the extractor doesn't recognize. Bounds maps what it
  can and **self-reports the gap** — it never silently drops a construct (see *Known gaps* and the
  *SQL / schema* section below).
- **Unsupported** — no adapter yet (Go, Rust, Java, …). These are **hand-mappable and durable**: point
  a subsystem's `paths:` at the source, hand-author (or AI-author) the `exposes`, and they survive
  `calibrate`/`validate` (see *Compiled / unsupported-language handling*). The
  [Roadmap](#roadmap) lists which adapters are coming and when.

The rule across all three: **a gap is always visible, never silent.** An unparsable statement, an
unsupported file, or an unowned supported file each surface as a loud, actionable, non-blocking
warning (`E_SCHEMA_UNPARSED` / `E_COVERAGE_GAP`) with a next step — not a crash and not a quiet
omission.

## Language support

| Language | Extraction | Describe Merge | Validate | Status |
|----------|-----------|---------------|----------|--------|
| **Python** | Functions, classes, decorators; ORM table declarations — SQLAlchemy (`__tablename__`/`__table__`), Django (`models.Model`/`Meta.db_table`) (see gaps below) | Yes | Yes | **Implemented** |
| **TypeScript / JavaScript** | ESM/CommonJS imports and exports; ORM table declarations — Drizzle (`pgTable`/`sqliteTable`/`mysqlTable`), TypeORM (`@Entity`) (see gaps below) | Yes | Yes | **Implemented** |
| **SQL** | DDL migrations: tables + columns (create/drop/rename), functions/RPCs, views, indexes, triggers, types, **policies + row-level security**; descends into `BEGIN; … COMMIT;` transactions; deterministic migration fold to the current surface (dropped policies/tables net out) | Table catalog + schema objects + RLS posture | Yes | **Implemented** |
| **Prisma** | `model` blocks → tables (`@@map`/`@map` honored). No RLS concept (Prisma manages access in app code), so no policy/RLS surface | Table catalog | Yes | **Implemented** |
| **Go** | Functions, methods, exported symbols | Planned | Planned | Future (v0.2.0 target) |
| **Rust** | `pub fn`, `pub struct`, `pub enum`, traits | Planned | Planned | Future (v0.2.0 target) |
| **Java** | Classes, interfaces, public methods | Planned | Planned | Future (v0.3.0 target) |
| **Fallback** | YAML-only metadata, no tree-sitter | No merge | Data integrity only | Only for files **explicitly declared** in a manifest |

Python, TypeScript/JavaScript, SQL migrations, and Prisma schemas are verified today (Prisma via a
deterministic block parser, the rest tree-sitter); Go, Rust, and Java are on the roadmap. The **fallback path is not a catch-all** — it only covers files a manifest **names
directly** (metadata is preserved, but there is no tree-sitter verification). Files in an unsupported
language that are only **auto-discovered** (not declared in a manifest) are silently skipped rather
than validated.

### Roadmap

The fully-supported set grows by adding **one adapter class per language** (see
[Adding a language adapter](#adding-a-language-adapter)). The current order, by demand and grammar
maturity:

| Tier | Language | Target | What lands |
|------|----------|--------|------------|
| Next | **Go** | v0.2.0 | Functions, methods, exported (capitalized) symbols, imports |
| Next | **Rust** | v0.2.0 | `pub fn` / `pub struct` / `pub enum` / traits, `use` imports |
| After | **Java** | v0.3.0 | Classes, interfaces, public methods, imports |

Until an adapter lands, those languages are **unsupported but first-class for mapping**: hand-author a
manifest and it stays durable (next section). **Want a language sooner?** A well-scoped adapter PR is
the fastest path — the how-to below is the canonical guide. Or open an issue with the repo + the
`unmapped_by_language` breakdown from `bounds validate` so we can prioritize by real demand.

### Compiled / unsupported-language handling

Until an adapter lands, a compiled or otherwise unsupported language (Go, Rust, Java, …) is handled
honestly rather than ignored:

- **Counted as unsupported in coverage.** Such files show up in `stats.coverage.mapping` as
  `unmapped_unsupported_language` with a by-language breakdown and a loud, non-blocking
  `E_COVERAGE_GAP` — a polyglot repo can never look "fully mapped" while half of it is invisible.
- **Hand-mappable and durable.** Point a subsystem's `paths:` at the source and hand-author (or
  AI-author) the `exposes`. Because Bounds has no adapter to verify them, those exposes are treated as
  **unverifiable, never stale**: `calibrate` routes a declared-but-unfound expose to `needs_review`
  (never `remove_exposes`) and `validate` does not flag it as `E_STRUCTURAL_DRIFT`. The two agree
  (shared `scan.subsystems_with_unsupported_source` signal), so a hand-authored manifest is never
  silently degraded. See [coverage.md](coverage.md).
- **Generated files & build dirs excluded.** Build/dependency dirs (`vendor`, `target`, `.gradle`,
  `build`, …) are in `config.DEFAULT_IGNORES`, and generated files carrying a header banner (e.g. Go
  protobuf `// Code generated by protoc-gen-go. DO NOT EDIT.`) are detected as generated and excluded
  from the expose surface.

> **Known gaps (current extractors).** Extraction is intentionally surface-level and ESM-first, so a
> few constructs are **not** yet captured:
>
> - **TS/JS — barrel re-exports.** `export * from "./mod"` records a dependency edge but does not
>   expand the target file's symbols into the barrel's public surface.
> - **TS/JS — `.pyi`-style decl files & namespaces.** TypeScript `namespace` blocks are not descended,
>   and only **top-level** imports/exports are captured (nested or conditional ones are skipped).
> - **Python — `.pyi` stubs** are not analyzed. For regular `.py` files, a module-level literal
>   `__all__` is honored as the exported surface; dynamic `__all__` expressions fall back to the
>   underscore convention because Bounds does not guess.
> - **SQL — migration ordering.** Migrations are ordered by filename numeric/timestamp prefix, an
>   embedded `revision`/`down_revision` header chain (Alembic-style offline SQL), or an explicit
>   `-- bounds:order N` header; an order that can't be determined deterministically is folded in
>   lexical filename order and flagged `E_SCHEMA_NO_ORDER`. Bounds never uses file mtimes. Alembic
>   and Django *Python* migration files (`op.create_table(...)` / `migrations.CreateModel`) are code,
>   not `.sql`, and are not folded — model classes are picked up via ORM recognition instead.
> - **SQL/ORM — query strings.** Raw query references are never verified edges. They are available
>   only opt-in via `bounds impact <table> --include-raw-queries` as a low-confidence advisory and can
>   never become a blocking boundary violation (the "verified, not guessed" moat).
> - **Prisma/ORM — relation fields.** A Prisma relation field (`posts Post[]`, `author User`) is
>   excluded from the column catalog — it's a model reference, not a database column. Only scalar fields
>   (`String`, `Int`, `Boolean`, `DateTime`, `Float`, `Json`, `Bytes`, `BigInt`, `Decimal`) and their
>   list forms (`String[]`) produce column entries.
>
> These are extraction limits, not validation bugs: a symbol Bounds can't see simply won't appear in a
> contract. Declaring such a symbol in a manifest's `exposes` will surface it as unverified.

---

## Adding a language adapter

This is the **canonical, single-source how-to** for moving a language from *unsupported* to *fully
supported*. [CONTRIBUTING.md](../CONTRIBUTING.md) links here; don't duplicate these steps elsewhere.
Adding a language is **one adapter class plus a registry entry** — no engine changes.

1. **Create the adapter module** — `src/bounds/extract/<language>.py`. Subclass
   `extract.base.LanguageAdapter` and set the two class attributes:

   ```python
   from .base import LanguageAdapter, make_result
   from ..models import ExtractResult, ImportRef, Symbol

   class GoAdapter(LanguageAdapter):
       language_name = "go"
       extensions = (".go",)

       def extract(self, rel_path: str, source: bytes) -> ExtractResult:
           # Walk the tree-sitter tree for top-level exported symbols + import refs, then:
           return make_result(rel_path, self.language_name, symbols, imports, source)
   ```

2. **Implement `extract(rel_path, source) -> ExtractResult`.** Walk the tree-sitter tree for the
   file's **top-level exported symbols** (as `Symbol`s) and its **import references** (as
   `ImportRef`s), and return them via **`base.make_result(...)`**. `make_result` computes *both*
   hashes consistently — `content_hash` (raw bytes, cache validity) and `structure_hash` (the
   canonical sorted interface surface, drift propagation) — so **never** build an `ExtractResult`
   by hand. Build the parser lazily and cache the tree-sitter `Language` (see `extract/python.py`
   for the `_parser()` pattern — the `Language`/`Parser` objects are expensive and reused).

3. **Fail soft.** A parse failure becomes `make_result(..., error="...")` (empty symbols/imports),
   **never a raised exception** — the engine turns that into an `E_EXTRACTION_FAILED` warning. A
   single bad file must never crash the run.

4. **Register it** in `extract/registry.py`: add an instance to the tuple in `_ensure_built()` (next
   to `PythonAdapter()`, `TypeScriptAdapter()`, …). The registry indexes it by `language_name` and
   by every entry in `extensions`; `get_adapter` then dispatches by file extension automatically.
   Never hardcode an extension list at a call site — `registry.is_language_file` /
   `supported_extensions()` are the single homes for "is this file language X?".

5. **Add the grammar dependency** to `pyproject.toml` (`dependencies`), e.g.
   `"tree-sitter-go>=…"`, alongside the existing `tree-sitter-python` / `-typescript` / `-sql`
   pins. Prefer a grammar that ships prebuilt wheels for Linux/macOS/Windows so installs need no C
   compiler.

6. **Write tests** under `tests/extract/` (add cases to `test_extract.py`, or a focused module like
   `test_typescript_exports.py`): assert the extracted symbols, the `exported` flag, and the import
   refs for a representative source string. If the adapter declares a self-consistency contract,
   override `check_contract` and cover it in `test_adapter_contracts.py`.

7. **Bump `config.STATE_VERSION`** *only if* extraction output changes for source that was already
   supported (a new adapter for a previously-*unsupported* language adds files without changing
   existing output, so it usually does **not** require a bump). The bump invalidates the
   content-hash cache so stale extractions don't survive across the change — see
   [testing.md](testing.md).

8. **Update this matrix and the [Roadmap](#roadmap)** — move the language from *Planned* to
   *Implemented* in the same PR, and run `bounds validate` on Bounds itself (it dogfoods its own
   manifests) so the change stays drift-free.

Adapters are the extension point by design: the engine, cache, validation checks, and CLI are all
language-agnostic and route through the registry, so a new language needs no changes outside these
steps.

---

## Framework & import-resolution support (TypeScript / JavaScript)

Extraction sees *what* a file imports; the **import resolver** decides which in-repo file a specifier
points at, and that edge is what `impact` / boundary checks / `where` rely on. Real-world TS projects
rarely import by raw relative stem — they lean on framework filename conventions and `tsconfig`
aliases — so the resolver understands these shapes (a bare specifier it can't resolve becomes no edge,
never a wrong one):

| Import shape | Example | Resolves? | Notes |
|--------------|---------|-----------|-------|
| **Dot-named relative files** | `./auth.service` → `auth.service.ts` | ✅ | The NestJS / Angular convention. The dots belong to the *filename*, not a path separator |
| **`tsconfig` `paths` aliases** | `@/common`, `@app/*` → `src/common`, … | ✅ | One `*` wildcard per entry; tried in sorted order before the baseUrl fallback |
| **`baseUrl`-relative bare imports** | `common/types` with `baseUrl: "src"` → `src/common/types` | ✅ | TS resolves bare specifiers against `baseUrl` before node_modules |
| **`extends` chains** | a leaf `tsconfig.json` extending a shared base | ✅ | The chain is merged base→leaf (leaf wins); JSONC comments + trailing commas tolerated. Bare-package `extends` (`@tsconfig/node20`) is out of scope |

**Honest limits.** Only **one** `*` wildcard per `paths` entry is supported. An alias that resolves
into a `node_modules` package (a monorepo workspace dependency) is treated as **external** — no edge.
Aliases apply **only to TS/JS importers**: a Python file never consults `tsconfig`. A broken or missing
`tsconfig` fails soft (no aliases, never an error). And these are **import-resolution + extraction**
capabilities — Bounds does not "support a framework" beyond seeing its imports and exported symbols;
it does not model framework runtime semantics (DI graphs, decorators-as-routes, module systems).

**Verified.** The cross-repo benchmarks run end-to-end on **click** (Python) and **axios**
(TypeScript) — axios's cross-subsystem edges resolve *only* because of the dotted-filename +
`tsconfig` path-alias fixes (on `main` its graph is badly undercounted). See
[`benchmarks/`](../benchmarks/). The NestJS / Angular import shapes (dot-named service/component files)
are covered by the resolver's test matrix.

---

## How SQL / schema extraction works (and what happens when it can't)

A schema subsystem (`paths:` pointing at `.sql` migrations, or a `.prisma` schema) is never
re-declared in YAML — **the migrations are the contract.** Bounds derives the current surface by
*folding* every migration in deterministic order (see the ordering note above) and reports what
survives. `bounds describe <schema-subsystem>` returns:

- `tables` — the live table catalog (name + columns), and `schema_hash`, a stable digest of that
  surface so an agent (or a freshness gate) detects a schema change without re-reading DDL.
- `schema_object_counts` — per-kind counts of the non-table surface (functions/RPCs, views,
  indexes, triggers, types, **policies**, **rls**). `--full` swaps counts for the full list.
- `rls_posture` — a derived row-level-security read computed from the fold: how many tables are
  `protected` (RLS on + ≥1 policy), `rls_without_policy` (RLS on, no policy — usually unintended),
  and `unprotected` (no RLS — the open door). `--full` lists the at-risk table names. Present only
  for schemas that actually use RLS.
- `schema_coverage` — **the AI trust signal.** `{complete: true}` means every owned file
  extracted, so a table/policy *not* in the catalog genuinely isn't in the schema (absence is
  authoritative). When some DDL couldn't be parsed it becomes `{complete: false,
  unextracted_files: N, note}` — telling an AI not to read a parse gap as "this doesn't exist."
  This is how Bounds stays honest about its own blind spots instead of misleading a consumer.
- `schema_diagnostics` (`--full`) — the per-file detail behind an incomplete `schema_coverage`
  (which files lost DDL, and why); see the fallback table below.

Two extraction layers, by what the SQL grammar can and cannot do:

- **Grammar-native** (tables, columns, functions, views, indexes, triggers, types) — read straight
  from the tree-sitter parse, descending into transaction (`BEGIN; … COMMIT;`) blocks.
- **Postgres RLS dialect** (`CREATE`/`ALTER`/`DROP POLICY`, `ENABLE`/`DISABLE`/`FORCE ROW LEVEL
  SECURITY`) — tree-sitter-sql has **no grammar** for these, so Bounds recovers them with a regex
  pass over the source **after blanking every comment, string literal, and function body** (so a
  `CREATE POLICY` inside a comment, a seed string, or an `EXECUTE '…'` body is never a phantom).

### Fallback when a statement can't be parsed

Bounds **fails soft and reports hard** — a file the grammar can't fully handle never crashes the
run, and the loss is always surfaced, never silent:

| Situation | What Bounds does |
|-----------|------------------|
| A `CREATE TABLE` with a table-level `CONSTRAINT … UNIQUE/CHECK (…)` clause (tree-sitter-sql can't parse the clause) | **Recovers** the table name and all columns best-effort; the unmodeled constraint tail is not a catalog loss |
| A statement carrying real DDL the grammar genuinely can't parse (e.g. a `DO $$ … $$` block that creates a table dynamically; a pg_dump fragment) | Emits **`E_SCHEMA_UNPARSED`** naming the file (a warning, never blocking); flips `describe.schema_coverage` to `complete: false` and is listed in `schema_diagnostics` (`--full`) + `validate`. The catalog **self-reports** incompleteness so a consumer is never misled |
| A whole file of DDL that yields nothing parseable | Listed in `describe.unparsed_files`; also counted in `schema_coverage.unextracted_files` |
| A non-schema `.sql` file (a seed `INSERT`, a `GRANT`/`REVOKE`, a `SELECT cron.schedule(…)`) | Extracted cleanly to **empty** symbols — **not** flagged. A file with no DDL is not a schema failure |

### Structured-language capability summary

| Language | Tables | Columns | RLS / policies | Other objects | When extraction fails |
|----------|--------|---------|----------------|---------------|------------------------|
| **SQL** | ✅ fold | ✅ fold | ✅ fold (create/alter/drop, enable/disable/force) | functions, views, indexes, triggers, types | `E_SCHEMA_UNPARSED` per file; never silent |
| **Prisma** | ✅ models | ✅ scalar fields | — (not a Prisma concept) | enums excluded from columns | model that can't parse → not in catalog |
| **Python ORM** | ✅ name only (SQLAlchemy `__tablename__`, `Table("…")`) | — | — | — | unrecognized model → no table symbol |
| **TS/JS ORM** | ✅ name only (Drizzle `pgTable`/`sqliteTable`/`mysqlTable`, TypeORM `@Entity`) | — | — | — | unrecognized model → no table symbol |

> RLS/policy folding and transaction-aware extraction are **SQL-specific** — they require Postgres
> DDL. Prisma and the ORM detectors recognize *table identity* only; they do not model RLS, columns
> (ORM), or non-table objects. A construct an adapter can't recognize simply won't appear in the
> contract (and a manifest that declares it will show it `unverified`) — it is never a silent wrong
> answer.

---

## Cross-platform support

Runs on **Linux, macOS, and Windows**, Python **3.10–3.14**. Internally Bounds uses `pathlib`
everywhere and stores POSIX-normalized relative paths (`as_posix()`), so manifests are byte-identical
across operating systems. The tree-sitter grammar dependencies ship prebuilt wheels for these
platforms, so a git/PyPI install never needs a C compiler.

| Platform | Notes |
|----------|-------|
| **Linux** | glibc (`manylinux` x86_64/aarch64) and musl/Alpine (`musllinux` x86_64) |
| **macOS** | Apple Silicon (arm64) and Intel (x86_64) — no Xcode required |
| **Windows** | `win_amd64`/`win_arm64` — no Visual C++ Build Tools needed. `--quick` needs Git for Windows on PATH |
