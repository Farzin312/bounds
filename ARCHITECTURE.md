# Compact — Architecture

> AI-native codebase understanding via subsystem boundary manifests.
> This document is the **authoritative engineering contract** for Compact's internals. Every module
> signature, dataclass field, error code, and JSON shape below is binding — the implementation builds
> against it. For the product pitch and usage, see [README.md](README.md); for scope/phasing, see
> [ROADMAP.md](ROADMAP.md).

---

## 0. Design principles (non-negotiable)

1. **Zero LLM cost for all structural operations.** Tree-sitter only. Tier 3 (LLM) is opt-in via `--deep` and is *stubbed* in the MVP.
2. **Deterministic.** Same inputs → same output, byte-for-byte. No timestamps in hashes, sorted collections everywhere.
3. **JSON-first.** Every command prints a JSON object to stdout. `--human` re-renders the same data for humans. Errors are JSON too.
4. **Hidden `.compact/`.** Never auto-discovered by other tools; walked up from CWD by the CLI only.
5. **Fail soft, report hard.** A single unparsable file degrades to an `Issue`, never a crash. Exit codes encode severity.
6. **Forward-compatible.** `version` on every manifest + cache file; unknown fields preserved/ignored, never fatal.

---

## 1. Directory structure

```
compact/
├── pyproject.toml                 # build + deps + console_script entry point
├── README.md                      # product pitch + quickstart + agent integration guide
├── ARCHITECTURE.md                # this file — engineering contract
├── ROADMAP.md                     # MVP scope vs. future phases
├── LICENSE
├── .compact/                      # Compact's own manifests (bootstrap demo, Phase 5)
│   ├── root.yaml
│   └── subsystems/
│       └── <name>/compact.yaml
├── src/
│   └── compact/
│       ├── __init__.py            # __version__
│       ├── cli.py                 # click group + all commands  (Phase 3)
│       ├── config.py              # constants: dir names, schema version, defaults
│       ├── errors.py              # CompactError + stable error-code registry
│       ├── models.py              # all dataclasses (the data model)
│       ├── output.py              # JSON / human emit + exit-code mapping
│       ├── gitutil.py             # git repo detection + changed-file diff
│       ├── manifest/
│       │   ├── __init__.py
│       │   ├── loader.py          # discover .compact/, load root + subsystems
│       │   └── schema.py          # validate manifest dicts → list[Issue]
│       ├── extract/
│       │   ├── __init__.py
│       │   ├── base.py            # LanguageAdapter ABC + hashing helpers
│       │   ├── registry.py        # extension/lang → adapter resolution
│       │   ├── python.py          # PythonAdapter
│       │   └── typescript.py      # TypeScriptAdapter (.ts/.tsx/.js/.jsx)
│       ├── cache/
│       │   ├── __init__.py
│       │   └── store.py           # State + load/save state.json
│       └── validate/
│           ├── __init__.py
│           ├── engine.py          # mode dispatch + orchestration
│           ├── propagation.py     # reference propagation (consumers of changed providers)
│           └── checks.py          # the 6 preflight checks
└── tests/
    ├── conftest.py
    ├── test_manifest.py
    ├── test_extract_python.py
    ├── test_extract_typescript.py
    ├── test_cache.py
    ├── test_propagation.py
    ├── test_checks.py
    ├── test_cli_integration.py
    └── fixtures/
        └── sample_project/        # tiny multi-subsystem TS+Py project
            ├── .compact/
            └── src/...
```

---

## 2. Data flow

```
                    ┌─────────────────────────────────────────────────────────┐
   CLI (click)      │  compact <cmd> [--human] [--quick|--mode M] [--enforce]  │
                    └───────────────┬─────────────────────────────────────────┘
                                    │
                    ┌───────────────▼───────────────┐
   discovery        │  manifest.loader.find_root()   │  walk up CWD → .compact/
                    │  load_root() + load_all()      │  RootManifest + {name: SubsystemCompact}
                    └───────────────┬────────────────┘   (+ schema.validate_* → Issues)
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        │ (list/describe)           │ (validate/preflight/overview)
        ▼                           ▼
   format & emit            ┌───────────────────────────────┐
                            │  validate.engine.run(mode)     │
                            └───────────────┬───────────────┘
                                            │
                 ┌──────────────────────────┼───────────────────────────┐
                 │ file set selection                                    │
                 │  full/audit → glob all subsystem paths                │
                 │  quick      → gitutil.changed_files() ∩ subsystem paths│
                 └──────────────────────────┬───────────────────────────┘
                                            │
                            ┌───────────────▼────────────────┐
   extraction (cached)      │ for each file:                 │
                            │  content_hash = sha256(bytes)  │
                            │  if cache hit → reuse symbols  │  cache/store.py (state.json)
                            │  else adapter.extract() + cache│  extract/registry.py
                            └───────────────┬────────────────┘
                                            │  ExtractResult per file
                            ┌───────────────▼────────────────┐
   change classification    │ structure_hash changed?        │
                            │  → mark subsystem "dirty"       │
                            │ propagation.propagate(dirty)    │  → affected consumers
                            └───────────────┬────────────────┘
                                            │
                            ┌───────────────▼────────────────┐
   checks                   │ checks.run_all(ctx, modes)     │  → list[Issue]
                            │  (1..6, see §7)                 │
                            └───────────────┬────────────────┘
                                            │  ValidationReport
                            ┌───────────────▼────────────────┐
   output                   │ output.emit(report, human)     │  JSON | human, exit code
                            └─────────────────────────────────┘
```

**Cache insight (content- vs structure-addressable):**
- `content_hash` = `sha256(file bytes)` → decides *cache validity*. Unchanged content ⇒ skip tree-sitter entirely (the sub-200ms path).
- `structure_hash` = `sha256` over the **sorted exported symbols + sorted import module strings** (the AST-derived interface surface) → decides *whether to propagate*. A comment- or body-only edit changes `content_hash` but **not** `structure_hash`, so consumers are not invalidated. This is the "AST hash, not raw bytes" idea from the spec.

---

## 3. Data model (`models.py`) — exact fields

All dataclasses use `@dataclass(slots=True)` where practical and provide `to_dict()` for JSON. Lists default via `field(default_factory=list)`.

```python
# ---- Manifest tier (declared) ----
@dataclass
class Interface:
    name: str
    kind: str = "unknown"          # function|class|const|type|interface|variable|unknown
    signature: str | None = None   # Tier-3 (LLM) enrichment; None in MVP

@dataclass
class Consumes:
    subsystem: str                 # provider subsystem name
    via: str | None = None         # path/module through which consumed (e.g. "user_repository")
    interfaces: list[str] = []     # provider interface names depended on

@dataclass
class SubsystemCompact:
    name: str
    role: str                      # service|platform|connector|library
    criticality: str = "leaf"      # core|connector|leaf  (drives propagation depth)
    description: str = ""
    paths: list[str] = []          # dirs/globs (relative to repo root) owned by this subsystem
    exposes: list[Interface] = []  # declared public surface
    consumes: list[Consumes] = []  # declared cross-boundary deps
    files: list[str] = []          # optional explicit file list (else derived from paths)
    consumed_by: list[str] = []    # AUTO-filled by loader (inverse of consumes)
    source_path: str = ""          # abs path to this compact.yaml

@dataclass
class RootManifest:
    version: str                   # schema version, e.g. "1"
    project: str
    languages: list[str] = []      # ["typescript","python"]
    enforce: str = "off"           # on|off  (whether full-mode issues are blocking)
    subsystems: list[str] = []     # subsystem names (each has its own compact.yaml)
    source_path: str = ""

# ---- Extraction tier (deterministic) ----
@dataclass
class Symbol:
    name: str
    kind: str                      # function|class|const|type|interface|variable
    line: int
    exported: bool = True

@dataclass
class ImportRef:
    module: str                    # raw module string: "./user_repository", "a.b.c"
    names: list[str] = []          # imported names ([] = namespace/default/side-effect)
    line: int = 0

@dataclass
class ExtractResult:
    path: str                      # repo-relative posix path
    language: str
    symbols: list[Symbol] = []     # exported/public symbols only
    imports: list[ImportRef] = []
    content_hash: str = ""
    structure_hash: str = ""
    error: str | None = None       # set if parse failed (file still counted, soft-fail)

# ---- Validation tier ----
@dataclass
class Issue:
    code: str                      # stable code, see §8
    severity: str                  # error|warning|info
    message: str
    subsystem: str | None = None
    file: str | None = None
    fix: str | None = None         # deterministic, actionable suggestion

@dataclass
class ValidationReport:
    status: str                    # fresh|stale|unresolved
    mode: str                      # quick|full|preflight|hotfix|audit
    ok: bool                       # True if no blocking issues for the mode
    issues: list[Issue] = []
    stats: dict = {}               # {files_scanned, cache_hits, subsystems, dirty, propagated, duration_ms}
```

`status` semantics:
- `fresh` — no errors; manifests match source.
- `stale` — structural drift / cross-impact detected (manifests need updating).
- `unresolved` — forward references to subsystems/interfaces that don't exist yet (incremental adoption; warning-level).

---

## 4. Module interfaces (binding signatures)

### `config.py`
```python
COMPACT_DIR = ".compact"
ROOT_FILE = "root.yaml"
SUBSYS_DIR = "subsystems"
SUBSYS_FILE = "compact.yaml"
STATE_FILE = "state.json"
SCHEMA_VERSION = "1"
VALID_ROLES = {"service","platform","connector","library"}
VALID_CRITICALITY = {"core","connector","leaf"}
VALID_MODES = {"quick","full","preflight","hotfix","audit"}
DEFAULT_IGNORES = {"node_modules",".git","dist","build","__pycache__",".venv",".compact"}
# propagation depth by criticality of the *changed* provider:
PROPAGATION_DEPTH = {"core": -1, "connector": 1, "leaf": 0}   # -1 = unbounded
```

### `errors.py`
```python
class CompactError(Exception):
    def __init__(self, code: str, message: str, fix: str | None = None): ...
    code: str; message: str; fix: str | None
    def to_dict(self) -> dict           # {error:{code,message,fix}}
# Registry constants (see §8) live here as module-level strings: E_MANIFEST_NOT_FOUND, ...
```

### `gitutil.py`
```python
def is_git_repo(path: Path) -> bool
def repo_root(path: Path) -> Path | None
def changed_files(repo: Path, base: str = "HEAD") -> list[Path]
    # union of: tracked modified (git diff --name-only base),
    #           staged (--cached), and untracked (ls-files --others --exclude-standard).
    # Returns absolute paths. Empty list if not a git repo (caller falls back to full).
```

### `manifest/loader.py`
```python
def find_root(start: Path) -> Path | None          # walk up for .compact/ ; returns the dir holding .compact
def load_root(project_root: Path) -> RootManifest   # raises CompactError(E_MANIFEST_*)
def load_subsystem(project_root: Path, name: str) -> SubsystemCompact
def load_all(project_root: Path) -> tuple[RootManifest, dict[str, SubsystemCompact], list[Issue]]
    # loads root + every declared subsystem, fills consumed_by, returns schema Issues (non-fatal)
```

### `manifest/schema.py`
```python
def validate_root(data: dict) -> list[Issue]
def validate_subsystem(name: str, data: dict) -> list[Issue]
    # checks required keys, enum membership (role/criticality), type shapes.
    # Returns Issues (E_SCHEMA_INVALID / warnings); never raises.
```

### `extract/base.py`
```python
class LanguageAdapter(ABC):
    language_name: str
    extensions: tuple[str, ...]
    @abstractmethod
    def extract(self, rel_path: str, source: bytes) -> ExtractResult: ...

def content_hash(source: bytes) -> str                       # sha256 hex
def structure_hash(symbols: list[Symbol], imports: list[ImportRef]) -> str
    # sha256 over "\n".join(sorted(f"{s.kind}:{s.name}" ...) + sorted(import modules))
def make_result(rel_path, language, symbols, imports, source, error=None) -> ExtractResult
    # fills both hashes
```

### `extract/registry.py`
```python
def get_adapter(rel_path: str) -> LanguageAdapter | None     # by extension
def adapter_for_language(name: str) -> LanguageAdapter | None
def supported_extensions() -> set[str]
# Adapters are lazily constructed singletons (tree-sitter Language objects are reused).
```

### Adapters — extraction rules
**PythonAdapter** (`.py`): top-level `function_definition`→function, `class_definition`→class, top-level assignments to UPPER/Capitalized names→const/variable. Public = name not starting with `_`. Imports: `import_statement` + `import_from_statement` (module = dotted path; names = imported identifiers).
**TypeScriptAdapter** (`.ts/.tsx/.js/.jsx`): walk `export_statement` children → `function_declaration`→function, `class_declaration`→class, `lexical_declaration`(const/let)→const, `interface_declaration`→interface, `type_alias_declaration`→type, `export ... default`→default, `export { a as b }`→re-export (use exported alias). Imports: `import_statement` (module from string literal; names from `import_clause`). TSX uses the `tsx` grammar; `.ts` uses the `typescript` grammar.

### `cache/store.py`
```python
@dataclass
class FileRecord:
    path: str; content_hash: str; structure_hash: str
    language: str; symbols: list[dict]; imports: list[dict]
    def to_result(self) -> ExtractResult
    @classmethod
    def from_result(cls, r: ExtractResult) -> "FileRecord"

class State:
    version: str
    files: dict[str, FileRecord]            # keyed by repo-relative path
    def get(self, path: str) -> FileRecord | None
    def put(self, r: ExtractResult) -> None
    def prune(self, live_paths: set[str]) -> None
    def to_dict(self) -> dict

def load_state(project_root: Path) -> State    # tolerant: missing/corrupt → empty State
def save_state(project_root: Path, state: State) -> None   # atomic write (.tmp → rename)
```

### `validate/propagation.py`
```python
def build_consumer_index(subsystems: dict[str, SubsystemCompact]) -> dict[str, list[str]]
    # provider_name -> [consumer names]
def propagate(dirty: set[str], subsystems: dict[str, SubsystemCompact]) -> set[str]
    # BFS over consumer edges; depth bounded per provider criticality (PROPAGATION_DEPTH).
    # Returns affected consumer subsystem names (excludes the originally-dirty set).
```

### `validate/checks.py`
```python
@dataclass
class CheckContext:
    project_root: Path
    root: RootManifest
    subsystems: dict[str, SubsystemCompact]
    extracts: dict[str, ExtractResult]      # rel_path -> result (only for scanned files)
    file_owner: dict[str, str]              # rel_path -> subsystem name
    dirty: set[str]                         # subsystems whose structure_hash changed
    propagated: set[str]                    # affected consumers

def check_structural_drift(ctx) -> list[Issue]      # E_STRUCTURAL_DRIFT
def check_boundary(ctx) -> list[Issue]              # E_BOUNDARY_VIOLATION
def check_contract(ctx) -> list[Issue]              # E_CONTRACT_MISSING_EXPORT
def check_cross_impact(ctx) -> list[Issue]          # E_STALE_INTERFACE
def check_cycles(ctx) -> list[Issue]                # E_CYCLE_DETECTED
def check_orphans(ctx) -> list[Issue]               # E_ORPHAN_EXPORT (warning)

CHECKS_BY_MODE = {
  "quick":     [drift, cross_impact],                          # fast, warning-only
  "full":      [drift, boundary, contract, cross_impact, cycles, orphans],
  "preflight": [drift, boundary, contract, cross_impact, cycles, orphans],  # blocking
  "audit":     [drift, boundary, contract, cross_impact, cycles, orphans],  # report only
  "hotfix":    [],                                             # no-op pass
}
```

### `validate/engine.py`
```python
def run(project_root: Path, mode: str = "full", base: str = "HEAD",
        enforce: str | None = None) -> ValidationReport
    # 1. load_all  2. select files  3. extract (cache)  4. classify dirty
    # 5. propagate 6. build CheckContext 7. run mode's checks 8. assemble report + status + ok
```

### `output.py`
```python
def emit(payload: dict, human: bool, stream=sys.stdout) -> None
def report_to_dict(report: ValidationReport) -> dict
def render_report_human(report: ValidationReport) -> str
def exit_code_for(report: ValidationReport, mode: str, enforce: str) -> int
    # 0 ok; 1 blocking errors (preflight always; full only when enforce=on); 2 internal error
```

---

## 5. Manifest schema (YAML)

**`.compact/root.yaml`**
```yaml
version: "1"
project: compact
languages: [python]
enforce: "off"
subsystems:
  - manifest
  - extract
  - validate
  - cli
```

**`.compact/subsystems/extract/compact.yaml`**
```yaml
name: extract
role: library
criticality: core
description: Tree-sitter extraction of exported symbols and imports per language.
paths:
  - src/compact/extract
exposes:
  - { name: get_adapter, kind: function }
  - { name: LanguageAdapter, kind: class }
  - { name: ExtractResult, kind: class }
consumes:
  - { subsystem: models, via: models, interfaces: [Symbol, ImportRef, ExtractResult] }
```

---

## 6. Validation modes

| Mode | When | Files | Checks | Blocking |
|------|------|-------|--------|----------|
| `quick` | every commit/PR | git-diff ∩ subsystems | drift, cross-impact | warning only |
| `full` | structure changes | all subsystem files | all 6 | iff `enforce=on` |
| `preflight` | pre-push | all | all 6 | always |
| `hotfix` | emergency | — | none | never (always ok) |
| `audit` | weekly | all | all 6 | never (report) |

`compact validate` defaults to `full`. `--quick` → quick. `--mode M` explicit. `compact preflight` → preflight mode.

---

## 7. The 6 checks (logic)

1. **Structural drift** (`E_STRUCTURAL_DRIFT`, error): for each subsystem, compare declared `exposes` names against the union of `exported` symbols actually extracted from its files. Declared-but-missing → drift; (optionally) undeclared exported symbol in a `core` subsystem → info. Fix: "add/remove `<name>` in exposes of `<subsystem>`".
2. **Boundary compliance** (`E_BOUNDARY_VIOLATION`, error): for each import in subsystem A resolving to a file owned by subsystem B, the imported names must all be in B's `exposes`. Importing a non-exposed (internal) symbol → violation. Resolution: match import `module` against B's file paths (suffix/relative resolution). Fix: "import only B's exposed interfaces, or add `<name>` to B.exposes".
3. **Contract compliance** (`E_CONTRACT_MISSING_EXPORT`, error): for each `consumes` entry, every listed interface must appear in the provider's `exposes`. Missing → contract break. Fix: "provider `<B>` does not expose `<iface>`; update consumer or provider".
4. **Cross-subsystem impact** (`E_STALE_INTERFACE`, error/stale): a provider's `structure_hash` changed (it's in `dirty`) and it has consumers (`consumed_by`) → those consumer interfaces may be stale. Emits one issue per affected consumer. Fix: "re-validate consumer `<C>`; provider `<B>` interface surface changed".
5. **Cycle detection** (`E_CYCLE_DETECTED`, error): build the directed graph from `consumes`; DFS for back-edges; report each cycle as a chain `A → B → C → A`. Fix: "break the dependency cycle; introduce an interface/inversion".
6. **Orphan detection** (`E_ORPHAN_EXPORT`, warning): an exposed interface that appears in no subsystem's `consumes` and the owning subsystem is not `service`/entrypoint (entrypoints legitimately expose unconsumed surface). Fix: "interface `<x>` of `<A>` is consumed by no one; consider removing or marking entrypoint".

Forward references (a `consumes.subsystem` or path that doesn't resolve to a known subsystem) → `E_UNRESOLVED_REFERENCE` (warning) and set report status `unresolved`.

---

## 8. Error / status code registry (stable)

| Code | Severity | Meaning |
|------|----------|---------|
| `E_STRUCTURAL_DRIFT` | error | exposes ≠ actual exports |
| `E_BOUNDARY_VIOLATION` | error | import of another subsystem's internal |
| `E_CONTRACT_MISSING_EXPORT` | error | consumer needs an interface provider doesn't expose |
| `E_STALE_INTERFACE` | error | provider surface changed; consumer may be stale |
| `E_CYCLE_DETECTED` | error | circular subsystem dependency |
| `E_ORPHAN_EXPORT` | warning | exposed interface consumed by no one |
| `E_UNRESOLVED_REFERENCE` | warning | forward ref to unknown subsystem/interface |
| `E_MANIFEST_NOT_FOUND` | fatal | no `.compact/` found |
| `E_MANIFEST_PARSE_ERROR` | fatal | YAML parse failure |
| `E_SCHEMA_INVALID` | error | manifest missing required keys / bad enum |
| `E_SUBSYSTEM_NOT_FOUND` | fatal | `describe <name>` for unknown subsystem |
| `E_UNSUPPORTED_LANGUAGE` | warning | file extension has no adapter (skipped) |
| `E_EXTRACTION_FAILED` | warning | tree-sitter could not parse a file |

**Exit codes:** `0` success / non-blocking; `1` blocking validation failure; `2` fatal (manifest/usage error).

---

## 9. CLI commands (`cli.py`) — contract

All commands accept `--human/-H` (default JSON) and resolve the project root via `find_root(Path.cwd())`.

```
compact list                       → {subsystems:[{name,role,criticality,description,exposes_count,consumes_count}]}
compact describe <name>            → full SubsystemCompact.to_dict() + {validation_status}
compact describe <name> --deep     → same + {semantic: {...}}  (Tier-3 stub: {"note":"LLM enrichment not enabled"})
compact validate [--quick|--mode M] [--enforce on|off] [--base REF]
                                   → ValidationReport.to_dict()
compact preflight                  → ValidationReport (mode=preflight) + per-check summary
compact overview                   → {project, subsystems, roles:{...}, criticality:{...}, edges, cycles, health:{...}}
compact init --root                → scaffolds .compact/root.yaml
compact init --subsystem <name>    → scaffolds .compact/subsystems/<name>/compact.yaml
```

Every command's JSON includes top-level `"validation_status"` where meaningful and `"ok": bool`. Fatal `CompactError` → `{"error":{code,message,fix}}` on stdout + exit 2.

---

## 10. Build sequencing & parallelization

Dependency DAG (→ = "depends on"):
```
config, errors            (no deps)            ← FOUNDATION, write first, by hand
models                    → (none)             ← FOUNDATION
output      → models, errors
gitutil     → (stdlib only)
manifest/*  → models, errors, config
extract/*   → models, config           ┐
cache/*     → models, extract           │ parallel-safe once foundation locked
                                        ┘
validate/*  → models, manifest, extract, cache, config   ← integration core
cli         → everything                                  ← last
tests       → everything                                  ← parallel per module
```

**Orchestration plan (ultracode):**
- **Stage A (hand-authored):** `pyproject.toml`, `__init__.py`, `config.py`, `errors.py`, `models.py` — these *are* the contract; coherence > fan-out.
- **Stage B (parallel workflow):** independent leaf modules against the locked foundation — `manifest/{loader,schema}`, `extract/{base,registry,python,typescript}`, `cache/store`, `gitutil`, `output`. One agent per module.
- **Stage C (hand-authored):** `validate/{propagation,checks,engine}` + `cli.py` — the integration core; written with full context, then run.
- **Stage D:** integrate, run `compact` end-to-end, fix mismatches.
- **Stage E (parallel workflow):** one test module per source module + integration test; then adversarial review.
- **Stage F:** bootstrap `.compact/` for Compact itself + sample project (Phase 5 verify).

**Acceptance criteria (definition of done):**
- `pip install -e .` succeeds; `compact --help` works.
- `compact init --root` then `compact list` round-trips.
- `compact validate` on the sample project returns deterministic JSON with each of the 6 checks exercised by at least one fixture.
- `compact validate --quick` re-extracts only git-changed files (verified via cache-hit stats).
- `compact` validates **itself** (bootstrap) with status `fresh` (or known, explained warnings).
- `pytest` green.
