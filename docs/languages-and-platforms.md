# Languages & platforms

*What Bounds extracts and validates per language, and where it runs.*

[← Docs index](./README.md) · [Bounds README](../README.md)

---

## Language support

| Language | Extraction | Describe Merge | Validate | Status |
|----------|-----------|---------------|----------|--------|
| **Python** | Functions, classes, decorators; ORM table declarations — SQLAlchemy (`__tablename__`/`__table__`), Django (`models.Model`/`Meta.db_table`) (see gaps below) | Yes | Yes | **Implemented** |
| **TypeScript / JavaScript** | ESM/CommonJS imports and exports; ORM table declarations — Drizzle (`pgTable`/`sqliteTable`/`mysqlTable`), TypeORM (`@Entity`) (see gaps below) | Yes | Yes | **Implemented** |
| **SQL** | DDL migrations: create/drop/rename tables, add/drop/rename columns; deterministic migration fold to the current table catalog | Table catalog | Yes | **Implemented** |
| **Prisma** | `model` blocks → tables (`@@map`/`@map` honored) | Table catalog | Yes | **Implemented** |
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
> - **Prisma/ORM — relation fields.** A Prisma relation field (`posts Post[]`) is captured as a column
>   like any other field; column granularity is declared-surface, not a typed scalar/relation split.
>
> These are extraction limits, not validation bugs: a symbol Bounds can't see simply won't appear in a
> contract. Declaring such a symbol in a manifest's `exposes` will surface it as unverified.

**Adding a language is one adapter class** (`extract.base.LanguageAdapter` — set `language_name`,
`extensions`, implement `extract`) plus a single registry entry in `extract/registry.py`. Use
`base.make_result(...)` so both content hashes are computed consistently.

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
