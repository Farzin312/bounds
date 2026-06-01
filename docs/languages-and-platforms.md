# Languages & platforms

*What Bounds extracts and validates per language, and where it runs.*

[← Docs index](./README.md) · [Bounds README](../README.md)

---

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

> **Known gaps (current extractors).** Extraction is intentionally surface-level and ESM-first, so a
> few constructs are **not** yet captured:
>
> - **TS/JS — barrel re-exports.** `export * from "./mod"` records a dependency edge but does not
>   expand the target file's symbols into the barrel's public surface.
> - **TS/JS — `.pyi`-style decl files & namespaces.** TypeScript `namespace` blocks are not descended,
>   and only **top-level** imports/exports are captured (nested or conditional ones are skipped).
> - **Python — `.pyi` stubs** are not analyzed, and **`__all__` is not honored** — the extractor reports
>   the actual top-level definitions rather than an `__all__`-declared surface.
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

**Adding a language is one adapter class** (`extract.base.LanguageAdapter` — set `language_name`,
`extensions`, implement `extract`) plus a single registry entry in `extract/registry.py`. Use
`base.make_result(...)` so both content hashes are computed consistently.

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
- `schema_diagnostics` — the actionable "why the catalog may be incomplete" list (see below).

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
| A statement carrying real DDL the grammar genuinely can't parse (e.g. a `DO $$ … $$` block that creates a table dynamically; a pg_dump fragment) | Emits **`E_SCHEMA_UNPARSED`** naming the file (a warning, never blocking); surfaced in `describe.schema_diagnostics` and `validate`. The catalog **self-reports** incompleteness |
| A whole file of DDL that yields nothing parseable | Listed in `describe.unparsed_files` (a genuine extraction failure) |
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
