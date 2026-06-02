# Changelog

## [Unreleased] — 2026-06-02

### Fixed (correctness — found by re-running the OSS cross-language benchmark + internal Next.js/Supabase dogfood)

- **TS type/re-export extraction (BOUNDS-008).** `export type`/`interface`/`enum`, `export * as ns`, and local vs cross-module re-exports are now detected with correct `kind` (`enum_declaration`→`enum`, not `const`; overload signatures collapse to one `function` symbol); discover and validate agree, so a fresh `discover → validate` no longer shows false `E_STRUCTURAL_DRIFT` on type-heavy TS modules (chalk drift ~4).
- **Test cases re-flagged as drift (BOUNDS-015).** A tests subsystem's `test_*`/`Test*` cases — excluded from `exposes` by BOUNDS-014 — were still flagged as "undeclared exports", flooding `validate` (click had silently regressed to **479** issues / 476 info-drifts). `check_structural_drift` now excludes them symmetrically; click is back to **3** (0 drift).
- **Next.js framework-entry exports flooded drift (BOUNDS-016).** `page`/`layout`/`route`/… files under `app/`|`pages/` export framework-invoked callbacks (the default component, `GET`/`POST`, `generateMetadata`, `revalidate`, …); these are no longer flagged as undeclared exports (zod 166→155). Directly improves the TS + Postgres/Supabase target stack.
- **`describe` file counting (BOUNDS-006)** now uses `scan.resolve_owners` (most-specific-path-wins) so it agrees with `validate` — no more double-counting a nested child's files — and emits an `E_SUBSYSTEM_OVERLAP` warning (`describe --full`) for genuine same-path conflicts.
- **`bounds where <path>` (BOUNDS-007)** — a path-shaped argument now returns the file's owning subsystem + every symbol it defines, instead of 0 results.
- **`describe` JSON/human parity (BOUNDS-010)** — `files[]` is populated in JSON under the same `--full` gate the human view uses.
- **`overview` health (BOUNDS-009)** — `health.ok` folds a real `validate` pass (+ a `health.validation` block) so the dashboard can't read "healthy" while `validate` would block.
- **`validate` fatal exit code (BOUNDS-011)** — a fatal `E_MANIFEST_NOT_FOUND` exits `2` (regression-guarded).
- **OpenCode command path** — written to `.opencode/commands/` (plural) per current OpenCode docs; the synced `/bounds` command now loads.

### Added

- **Subsystem ↔ docs/tests linkage.** Optional `docs:`/`tests:` manifest fields (human-curated, authoritative) + convention auto-detection (`tests/<area>/` → `<area>`, `docs/<name>.*` → `<name>`); `bounds discover` auto-populates them. `bounds validate`/`overview` coverage gains informational `tests`/`docs` buckets, and **test files are excluded from the source denominator** so a repo's tests never read as an unmapped gap. `describe --full` shows a subsystem's linked docs + tests. Coverage stays "100%-or-guidance": an unmapped library file fires `E_COVERAGE_GAP` with the exact fix; tests/docs are tracked but never block. See [docs/coverage.md](docs/coverage.md).

### Changed

- **Docs mirrored** — ARCHITECTURE.md §3/§5/§7/§8/§9, README (honest post-fix convergence + specialization), benchmark report, and `known-issues.md` (BOUNDS-006…016) updated in the same change. Every test now carries a meaningful docstring; `tests/validate/test_validate.py` split by concern.

## [Unreleased] — 2026-05-31

### Fixed

- **SQL schema extraction — major coverage + reporting fixes.** Verified on a real 289-migration Supabase schema:
  - **Transaction-wrapped DDL was silently dropped.** The walk now descends into `BEGIN; … COMMIT;` blocks, recovering tables, columns, functions, policies, and RLS that were lost in ~⅓ of migrations. On the test corpus: tables 103→114, functions 2→72, indexes 304→357, triggers 26→52.
  - **`describe.unparsed_files` cried wolf.** A no-DDL `.sql` file (a seed `INSERT`, a `GRANT`/`REVOKE`, a `SELECT cron.schedule(…)`, or a `CREATE SCHEMA`/`CREATE EXTENSION`-only migration) was reported as "unparsed". It now extracts cleanly to empty symbols and is never flagged; only files that genuinely lost DDL are reported (`unparsed_files` 72→25). A parse error in a non-DDL statement no longer raises `E_SCHEMA_UNPARSED`.
  - **Tables created `public.x` but altered bare were split.** Every table reference now canonicalises to its bare name, so all ops fold to one catalog entry.
  - **Tables with a table-level `CONSTRAINT … UNIQUE/CHECK (…)`** (which tree-sitter-sql can't parse) are now recovered best-effort with their columns, instead of losing the whole table.
- **Prisma adapter** — relation fields (`posts Post[]`, `author User`, `profile Profile?`) are no longer captured as columns. Only scalar types produce column entries, preventing phantom columns in `bounds describe` and false passes in column-granular `check_contract`.
- **SQL adapter** — `schema_meta` symbols (revision headers) no longer mask all-error SQL files. A migration with a valid `-- revision` header but zero parseable statements now correctly returns a hard parse failure instead of folding partial data.
- **TypeScript import resolver — dotted filenames.** Imports targeting files whose names contain dots (e.g. `foo.bar.ts`, `index.browser.ts`) now resolve correctly instead of being dropped, fixing undercounted dependency edges in TS projects.
- **TypeScript import resolver — tsconfig path aliases + `baseUrl`.** `@/…`-style path aliases (and `baseUrl`-relative bare imports) declared in `tsconfig.json` are now resolved to real files, so alias-heavy TS codebases no longer show a badly undercounted subsystem graph.
- **`discover` over-fragmented framework modules.** A module's structural sub-directories (`dto/`, `services/`, `entities/`, `types/`, …) sitting under a candidate module now fold into it, so a NestJS-style `auth/{dto,services,auth.module.ts}` becomes one `auth` subsystem instead of `auth` + `auth-dto` + `auth-services` (fewer, clearer names; e.g. axios 20→16 subsystems). Conservative: a standalone structural dir whose parent has no sources is preserved. `paths:` also collapses to the covering root.

### Changed

- **`bounds agent` UX overhaul.** Bare `bounds agent` now defaults to the read-only `--detect` (lists which coding agents are present) instead of erroring — it is always safe to type; only passing two mode flags is a usage error. `bounds --help` is now grouped into purpose-ordered sections ("Set up", "Read the map", "Catch drift", "Maintain") with concise, no-longer-truncated descriptions. `agent --sync` reports honestly — `created` / `updated` / `already current`, and distinguishes `left alone (you maintain these)` (a file you wrote that mentions bounds, no markers) from `left alone (you edited the bounds block)`, plus a hint that adding empty `<!-- BOUNDS:START -->` / `<!-- BOUNDS:END -->` markers and re-syncing lets Bounds manage a section. The `agent --sync` JSON gained `unchanged` and `skip_reasons` keys.
- **Policies and RLS now fold like tables.** `schema_objects` applies `CREATE`/`ALTER`/`DROP POLICY` and `ENABLE`/`DISABLE`/`FORCE`/`NO FORCE ROW LEVEL SECURITY` in migration order, so a dropped policy or disabled table nets out of the reported surface (was a flat dedup that ignored drops). Live policy/RLS coverage on the test corpus: policies 175→275, RLS 98→122. Because the result now depends on order, an unordered migration set carrying policy/RLS lifecycle ops (e.g. a `DROP POLICY` with no filename prefix / revision chain) now correctly surfaces `E_SCHEMA_NO_ORDER`.
- **`config.STATE_VERSION` 1→2.** SQL extraction output changed for unchanged source, so every existing binary `cache.db` is treated as version-mismatched and rebuilt on the next run (no stale symbols). `schema_hash` for a Postgres schema changes accordingly (the folded surface is now more complete) — it remains deterministic and byte-stable across runs.

### Added

- **`bounds guide`** — a state-aware setup checklist that inspects the current repo (`.bounds/` present? manifests discovered? agent config synced? CI gate installed?) and prints the next concrete steps, so a new user or agent is walked through onboarding instead of guessing the command order.
- **`agent --sync` now generates per-agent native command/skill files** (in addition to the canonical `AGENTS.md` block): Claude Code and Codex get skill files, Gemini / OpenCode / Cursor get command files, Copilot gets a prompt file, and Windsurf gets a workflow file (aider has no native format, so none is written). Each agent gets Bounds in the format it natively reads.
- **`bounds describe` RLS security posture (Postgres/Supabase).** A derived `rls_posture` block reports how many tables are `protected` (RLS on + ≥1 policy), `rls_without_policy` (RLS on, no policy), and `unprotected` (no RLS — the open door); `--full` lists the at-risk table names. Present only for schemas that use RLS. Computed deterministically from the fold for both humans (`--human`) and agents (JSON).
- **`bounds describe` schema coverage — an AI trust signal.** Every schema subsystem now carries `schema_coverage`: `{complete: true}` when all owned DDL extracted (so a table/policy *not* in the catalog genuinely isn't in the schema — absence is authoritative), or `{complete: false, unextracted_files: N, note}` when some DDL couldn't be parsed, telling a consumer **not** to read a parse gap as "this doesn't exist." The per-file `schema_diagnostics` detail behind it is gated to `--full`, so the default output is *leaner* than before while being more honest.
- **SQL policy/RLS recovery widened** — `CREATE POLICY IF NOT EXISTS`, `ALTER POLICY [RENAME TO]`, `DROP POLICY`, `FORCE`/`NO FORCE ROW LEVEL SECURITY`, and schema-qualified/quoted identifiers are now recovered (via a comment/string/function-body-masked scan), and policies survive even in pg_dump-style files the grammar shreds.

- **Adapter output contracts** — every LanguageAdapter can declare self-consistency invariants via `check_contract()`, validated at `bounds validate` time as a new `check_adapter_contracts` advisory check. Catches relation-field leaks in Prisma and all-error+revision-header masking in SQL deterministically, zero LLM. Wired into `quick`/`full`/`preflight`/`audit` modes as a warning-only guard.

- **Data-boundary / schema mapping** — a database TABLE is modeled as just another exposed symbol of a schema subsystem, so the existing contract/drift/boundary/impact/propagation machinery works on tables. Spans the full pipeline:
  - **ORM table recognition** (zero new deps) — Python SQLAlchemy (`__tablename__`, imperative `__table__ = Table("…")`), Django (`models.Model`, `Meta.db_table`; abstract models are correctly *not* tables), and TypeScript Drizzle (`pgTable`/`sqliteTable`/`mysqlTable`) and TypeORM (`@Entity("…")` and `@Entity({ name: "…" })`) model classes are tagged `kind: table` with their real table name. Detection is structural (no substring/eval), so a comment mentioning `__tablename__` or an f-string table name never fabricates a phantom table.
  - **SQL adapter (`.sql`)** — deterministic DDL extraction via **`tree-sitter-sql`** (a new runtime dependency, prebuilt wheels). Per-statement fail-soft: one unparsable statement is reported (`E_SCHEMA_UNPARSED`) without dropping the file's other valid statements. Double-quoted Postgres identifiers are preserved.
  - **Migration fold** — applies CREATE / ADD / DROP / RENAME (table & column) in deterministic order (filename numeric/timestamp prefix → `revision`/`down_revision` header chain → explicit `-- bounds:order N`; **never** file mtime) to materialize the current table catalog as the subsystem's effective `exposes`. Undetermined order is surfaced as the advisory `E_SCHEMA_NO_ORDER`.
  - **Prisma adapter (`.prisma`)** — `model` blocks fold like SQL tables (`@@map`/`@map` honored); no new dependency.
  - **`bounds describe <db>`** returns the verified table catalog (+ a deterministic `schema_hash`); **`bounds impact <table>`** returns the read/write blast radius before a migration; `table.column`-granular `exposes`/`consumes` catch a dropped-column drift in both directions.
  - **`bounds impact <table> --include-raw-queries`** — opt-in, low-confidence raw-SQL string consumers, surfaced as an advisory `heuristic_consumers` block. By construction it is never counted in `blast_radius` and can never produce a blocking `E_BOUNDARY_VIOLATION` (the "verified, not guessed" moat).
- **`bounds discover`** — bootstrap manifest generation: auto-discovers candidate subsystems from source and proposes `.bounds/` manifests for a new project.
- **`bounds calibrate`** — reconciles manifests against tree-sitter reality, proposing exports to add or remove. Honors a new per-export `internal` flag (mark a symbol deliberately private and calibration leaves it alone).
- **`bounds impact <name>`** — transitive blast radius: which subsystems break if the named subsystem changes. Backed by a new `transitive_consumers` graph walk in the propagation engine.
- **`bounds agent --sync/--detect/--check`** — cross-agent config *generator*. Writes the canonical contract into `AGENTS.md` (the cross-ecosystem standard file agents already read) plus a short per-agent pointer for eight agents (Claude Code, Codex, OpenCode, Gemini, Copilot, Cursor, Aider, Windsurf). Generates committed files; not a hosted integration or plugin registry.
- **`bounds ci --install`** (`--action`/`--precommit`/`--gitlab`/`--all`) — CI gate config *generator*. Writes a GitHub Action workflow, pre-commit hook, and/or GitLab CI config you commit. Not published Marketplace actions or a published pre-commit repo.
- **`bounds cache --migrate/--inspect/--prune`** — manage the binary extraction cache.
- **Schema flexibility** — `root.yaml` now accepts extensible roles and criticality values rather than a fixed enum.
- **Per-export `internal` flag** — exposes can be marked `internal: true` to exempt them from calibration add/remove and signal a deliberately-private symbol.
- **Install artifacts** — `install.sh` (pipx-preferred, installs from the git ref by default since the PyPI name is pending; `BOUNDS_REF` pins a tag/branch), a Homebrew `Formula/bounds.rb`, and a `Makefile` (`make install/dev/test/validate/benchmark`).
- **Cross-repo OSS benchmark harness** (`benchmarks/oss_run.py`) — clones real third-party projects (click, axios) at a cited commit, runs `bounds discover`, and measures Bounds-vs-source token economics, so the headline numbers aren't measured only on Bounds itself. Recorded results + a same-model capability head-to-head live in `benchmarks/results/oss-token-economics.md`.

### Changed

- **Cache format changed from `state.json` (JSON) to `.bounds/cache.db` (binary SQLite).** The new cache adds context-armor integrity. Bounds **auto-migrates** an existing `state.json` to `cache.db` on first run; `bounds cache --migrate` does the same on demand. SQLite is Python stdlib, so no new dependency was added.
- **Renamed the project, CLI, and Python package from `compact` to `bounds`.** The console entry point is now `bounds` (was `compact`); install from the git ref (`pipx install "git+https://github.com/Farzin312/bounds.git"`) — the PyPI name `bounds` is held by an unrelated package, so a PyPI install is not yet available. The Python package import path is now `bounds` (was `compact`).
- **Config directory moved from `.compact/` to `.bounds/`.** A backward-compatible fallback is in place: if `.bounds/` is absent, Bounds still reads a legacy `.compact/` directory and prints a deprecation notice to stderr when it does.
- **`CompactError` exception renamed to `BoundsError`.** The old `CompactError` name is kept as a deprecated alias for backward compatibility.

### Tests

- **The full test suite** now passes (expanded from the v0.1.0 figure), covering the new `discover`, `calibrate`, `impact`, `agent`, `ci`, and SQLite-cache surfaces, the data-boundary feature (SQL/Prisma migration fold, ORM table recognition, column-level drift, the raw-query moat guardrail, and false-positive/false-negative guards for each), in addition to extraction, validation, and schema flexibility.

## [0.1.0] — 2026-05-29

### Initial release

Bounds brings AI-native codebase understanding via subsystem boundary manifests — with zero-LLM structural validation powered by tree-sitter.

### Features

- **Subsystem manifests** — declare subsystem boundaries, roles, public interfaces, and cross-boundary dependencies in YAML. Hidden `.bounds/` directory prevents accidental token burn.
- **Tree-sitter extraction** — deterministic extraction of exported symbols and imports from Python and TypeScript/JavaScript source files. Zero LLM cost for structural operations.
- **Validation engine** — 6 checks covering structural drift, boundary compliance, contract compliance, cross-subsystem impact, cycle detection, and orphan detection.
- **5 validation modes** — `quick` (git-diff incremental, sub-200ms target), `full`, `preflight`, `hotfix`, `audit`.
- **Content-addressable cache** — per-file hashing over tree-sitter output (not raw source), so whitespace-only and comment-only changes don't trigger re-validation.
- **Reference propagation** — when a subsystem changes, all consumers are checked via interface-name comparison (zero tree-sitter, zero LLM).
- **Quick mode** — `bounds validate --quick` uses git diff to re-extract only changed files, then propagates impact through the subsystem graph.
- **JSON-by-default CLI** — every command outputs structured JSON. `--human` re-renders the same data for terminal use. `--ci` output for log grepability.
- **Stable error codes** — all error codes live in `errors.py` and are a public contract. Never renumbered, renamed, or repurposed between versions.
- **Commands:** `bounds init`, `bounds list`, `bounds describe`, `bounds validate`, `bounds preflight`, `bounds overview`.
- **Cross-platform support** — Linux, macOS, Windows. Python 3.10 through 3.14. Prebuilt tree-sitter wheels for all platforms — no C compiler required.
- **Agent-first design** — JSON output by default, designed for AI coding agents (Claude Code, OpenCode, Codex CLI, Cursor, Windsurf) to consume directly. Agent integration instructions for every major platform.

### Language support

- Python (tree-sitter-python) — functions, classes, decorated exports
- TypeScript / JavaScript (tree-sitter-typescript) — exports, classes, interfaces, type aliases

### Explicitly not in v0.1

- LLM enrichment (`describe --deep` is stubbed — returns `{"note": "LLM enrichment not enabled"}`)
- MCP server (integration today is CLI-only)
- Languages beyond Python + TypeScript/JavaScript
- `migrate` command, configurable ignore lists, IDE extensions

### Notes

- Bounds validates ITSELF (`bounds validate --human` on the Bounds project) — fully dogfooding from day one.
- The full test suite passes with pytest-xdist parallel execution.
