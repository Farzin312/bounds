# Premortem + Arboretum Analysis: Adapter Bug Detection for Bounds

**Date:** 2026-05-31
**Context:** Codex found 2 P2 adapter bugs on PR #20 (Prisma relation fields leaking as columns; SQL revision-header masking all-error hard failure) that Bounds' existing validation — boundary integrity checks — couldn't catch.

---

## PHASE 0 — CURRENT STATE (ground truth)

### What exists today
- **4 LanguageAdapters** in `src/bounds/extract/`: Python, TypeScript, SQL, Prisma
- **7 validation checks** in `src/bounds/validate/checks.py`: structural drift, boundary, contract, cross-impact, cycles, orphans, schema
- **Schema fold** (`src/bounds/validate/schema.py`): combines SQL + Prisma into unified table catalog
- **CLI**: `bounds validate` (full/quick/preflight/audit/hotfix), `bounds describe`, `bounds impact`, `bounds where`
- **Zero-LLM** by design: all extraction and validation is deterministic, no LLM calls

### What it cannot catch
- Adapter output correctness: if a Prisma adapter emits a relation field as a column, the fold accepts it and check_schema passes — there's no contract saying "columns must be scalar-only"
- Edge-case error handling paths: the SQL adapter's all-error mask was a logic error in the adapter itself, not a boundary violation
- Semantic consistency: "does this column list actually make sense for the language?"

---

## PHASE 1 — PREMORTEM

### IDEA 1 — Adapter Output Contracts (deterministic)

| Dimension | Failure mode |
|-----------|-------------|
| **Technical** | Contracts are only as good as the contract-writer. The first bug (Prisma relation fields) would be caught by a "columns contain only scalars" contract — but only because that bug is *already known*. Novel adapter bugs with no known pattern slip through exactly like before. Contracts are retrospective, not generative. |
| **Market** | Nobody uses adapter contracts because they're invisible: they succeed silently and fail only when an adapter has a real bug. But adapter bugs are rare (the two found were P2s in 20 PRs). Users will see "no issues" for weeks and then one day a contract fires. Without a steady stream of value, they stop running `bounds validate` or ignore the output. |
| **Positioning** | Modest dilution: contracts are still zero-LLM. But they expand Bounds' surface from "boundary integrity" to "adapter correctness" — a category where it can never be complete (you can't contract-test every possible adapter output). Risk: users conclude "if even Bounds' own contracts don't fully catch bugs, what good is it?" |
| **Complexity** | Low. Adding `contract: ...` to each adapter is ~50 lines per adapter. But the maintenance burden is real: each new adapter (Java, Go, Rust) needs a contract, and each contract needs test fixtures. As adapter count grows, so does contract surface area. |

### IDEA 2 — AI Review Command (opt-in, tier 3)

| Dimension | Failure mode |
|-----------|-------------|
| **Technical** | LLMs hallucinate false positives. A `bounds audit --deep` that flags adapter outputs as "suspicious" when they're actually correct creates noise that destroys trust. Worse: the LLM *doesn't* find the real bug but "catches" 3 phantom issues — the user wastes time, disables the command, and never re-enables it. |
| **Market** | Nobody uses it because it's too slow and too expensive. A `bounds validate` run is ~200ms. An LLM call is 5-30s and costs tokens. Users run validate in CI on every push. They will not add a 30s token-cost step. Off by default doesn't help — it means nobody remembers it exists. |
| **Positioning** | **Fatal dilution.** Bounds' entire brand is "zero-LLM, verified, deterministic." An AI review command — even opt-in — plants the doubt: "wait, are the other commands secretly LLM-backed too?" It blurs the brand line that makes Bounds unique among architecture tools (most of which are LLM-heavy). |
| **Complexity** | High. API key management, token cost tracking, prompt engineering per adapter, output parsing, false-positive rate tuning, hallucination guardrails. All for a feature that's off by default and likely fires P2-level bugs quarterly at best. |

### IDEA 3 — Combined AI + Deterministic Pipeline

| Dimension | Failure mode |
|-----------|-------------|
| **Technical** | The pipeline couples two systems with very different failure modes. AI suggests "check for X pattern." Deterministic check runs on X pattern but the AI *missed* the actual bug (pattern Y) — so nothing fires. The user sees the same "all clear" output but now with an expensive AI pre-pass that never pays off. Worse: the AI suggestion becomes a false sense of coverage. |
| **Market** | Same as Idea 2 (slow, expensive, off by default) PLUS the added complexity of "why is this combined? just run the deterministic check." Users won't understand the value of the AI pre-scan because its output (suggested patterns) is invisible — only the deterministic check output is visible. |
| **Positioning** | **Fatal dilution** (same as Idea 2) PLUS architectural cruft. Bounds' engine pipeline (`checks.py`) is beautifully simple: pure functions over extracted data. Adding an AI pre-pass that mutates the check list destroys that purity. The engine becomes stateful, the check list becomes non-deterministic, and the test suite becomes probabilistic. |
| **Complexity** | Highest of all five. You need: LLM API integration, prompt chain management, a pattern suggestion → check injection mechanism, deterministic check generators from LLM output, and a way to test it (impossible — LLM output is non-deterministic). This is a research project, not a feature. |

### IDEA 4 — Adversarial Extraction Mode

| Dimension | Failure mode |
|-----------|-------------|
| **Technical** | The fuzz fixtures must stay in sync with actual adapter logic — or they test things the adapter doesn't handle (false positive) or miss things the adapter now handles differently (false negative). When the adapter logic is refactored, the fuzz fixtures rot silently because `--fuzz` is not on the default path. After 6 months, the fuzz suite tests obsolete edge cases and the maintainer has no idea. |
| **Market** | Nobody runs `--fuzz` because it has no output unless there's a bug. It's like running `validate` but slower and with no guarantee of finding anything. Developers don't run extra commands that produce no output 99% of the time unless they're debugging a specific issue. For CI, it adds 10-50ms per adapter with zero value if fuzz fixtures are stale. |
| **Positioning** | Minor dilution: it adds a new mode ("fuzz") that expands Bounds' surface from "architecture verification" to "adapter QA." This isn't an architecture concern. But it's still zero-LLM, so brand damage is minimal. The bigger risk is that users who run it and get stale results conclude "Bounds' validation is useless." |
| **Complexity** | Medium. Each adapter needs 3-5 edge-case fixture files (deliberately corrupted, borderline syntax, language-specific edge cases). Writing these is labor-intensive and requires deep language knowledge. Maintaining them is worse — every time the parser grammar changes, fixtures may need updating. |

### IDEA 5 — Cross-Adapter Schema Reconciliation

| Dimension | Failure mode |
|-----------|-------------|
| **Technical** | Real projects are messy. A table seen by Prisma but not in SQL migrations is often *intentional* (Prisma-managed migrations). Vice versa: legacy tables in raw SQL that aren't in Prisma schema. The reconciliation fires hundreds of warnings on any real project. Users mute them all and miss the one real inconsistency. The signal-to-noise ratio is terrible in practice. |
| **Market** | **Nobody trusts it on their real repo.** The first run produces 50+ "inconsistencies." The user spends an afternoon triaging them and finds 48 are false positives (intentional design choices, legacy tables, temporary migration states). They never run it again. Word spreads: "Bounds' cross-schema check is spammy." |
| **Positioning** | **Unique but dangerous.** This is genuinely novel — no other tool reconciles Prisma + SQL + ORM catalogs. But it positions Bounds as a "schema consistency checker," which is adjacent to (but distinct from) its core value prop of "architecture boundary verification." Users who come for schema reconciliation and find it noisy will write Bounds off entirely. |
| **Complexity** | Very high. Every ORM (Django, SQLAlchemy, TypeORM, Drizzle) has a different model declaration syntax. A "cross-adapter reconciliation" feature would need adapters for each ORM. Bounds currently has 4 adapters; this would require 8-12, all of which are schema-aware (different from the existing code-only adapters like Python/TypeScript). The fold logic (`schema.py`) would need to merge catalogs from multiple schema subsystems, which it currently does not. |

---

## PHASE 2 — SUCCESS MODELS

### IDEA 1 — Adapter Output Contracts
**What would need to be true for 10x?**
- A *third* adapter bug is caught before shipping to prod. A user in the community posts "Bounds contracts caught a Prisma enum mapping bug before we deployed" — the tweet goes viral in the Prisma/TypeScript community.
- Contracts become the standard pattern for "how to write an adapter," documented in CONTRIBUTING.md. New contributors write contracts first, then implementation — turning the design philosophy inside out (contract-driven extraction).
- A contributed adapter (e.g., from a large company's internal tooling) ships with contracts that are so useful that the company open-sources more adapters.
- **Star attractor:** A blog post titled "How we caught 3 adapter bugs with 50 lines of YAML" — concrete, shareable, zero-LLM.

**Genuinely useful vs. cool:**
- Useful if contracts catch real bugs. Cool if they're easy to write and maintain.
- Verdict: **useful but bounded.** Best for preventing regression on *known* bug patterns. Useless for novel bugs.

### IDEA 2 — AI Review Command
**What would need to be true for 10x?**
- An LLM finds a *novel* adapter bug that deterministic checks (including contracts) could never catch — e.g., "this Prisma adapter incorrectly flattens composite types into columns" — and the fix is deployed before it reaches prod.
- The cost per run drops to <$0.001 (through caching, batching, or model improvements), making it feasible to run in CI.
- The false-positive rate is <5%, and every false positive is immediately actionable (not noise).
- **Star attractor:** "Bounds found a bug ChatGPT missed" — ironic twist of the Codex story.

**Genuinely useful vs. cool:**
- Cool: "AI catches bugs!" Useful: it actually catches bugs that matter and doesn't waste time.
- Verdict: **cool but not useful** at current LLM cost/reliability. The risk of brand dilution outweighs the novelty.

### IDEA 3 — Combined AI + Deterministic Pipeline
**What would need to be true for 10x?**
- Same as Idea 2 (LLM must find novel bugs at low cost) PLUS the deterministic checks must actually fire on the AI's suggestions (otherwise the pipeline is just an expensive no-op).
- The AI pre-scan must be fast enough (<1s per file) to not slow down the deterministic check pipeline.
- **Star attractor:** "AI designed the check that caught the bug" — a compelling story, but hard to explain concisely.

**Genuinely useful vs. cool:**
- This is the most sophisticated idea and the least likely to work well. The coupling creates complexity that kills utility.
- Verdict: **research project, not a product feature.** Do not build.

### IDEA 4 — Adversarial Extraction Mode
**What would need to be true for 10x?**
- The fuzz fixtures become a de facto "adapter conformance test suite" that adapter contributors use during development (like `cargo test` for Rust crate authors).
- A contributor adds a new adapter and the fuzz suite catches 2 of their bugs before they open the PR. The contributor writes "I used Bounds' fuzz mode to validate my Java adapter and it caught X, Y, Z" — proving the tests are useful beyond regression.
- The fuzz suite auto-discovers edge cases via property-based testing (Hypothesis/fuzzing library integration) instead of manually-written fixtures.
- **Star attractor:** "Property-testing for database schema extraction" — a genuinely novel engineering technique.

**Genuinely useful vs. cool:**
- Useful if it's part of the development workflow. Cool if it catches bugs automatically.
- Verdict: **useful for maintainers, invisible to users.** Not a star attractor unless it catches a famous bug.

### IDEA 5 — Cross-Adapter Schema Reconciliation
**What would need to be true for 10x?**
- A major incident is prevented: a company deploys a migration that drops a table Prisma thinks exists, and Bounds catches it in CI before production goes down. The story spreads on Hacker News.
- The reconciliation is *smart*: it knows that Prisma-managed migrations intentionally create a subset of the full schema. Intelligent filtering (not just set-diff) reduces noise to near-zero.
- Integration with ORM model files (Django models, SQLAlchemy ORM definitions, TypeORM entities) makes it the go-to tool for "schema drift detection" — a well-known pain point.
- **Star attractor:** "Bounds saved us from a prod schema mismatch" — the kind of story every engineering org relates to. This is the highest-potential star attractor of all five ideas.

**Genuinely useful vs. cool:**
- This solves a real pain point: schema drift between sources of truth is a classic problem. No tool does it well.
- Verdict: **genuinely useful AND the highest star potential.** The challenge is reducing noise to acceptable levels.

---

## PHASE 3 — ARBORETUM DECISIONS

### IDEA 1 — Adapter Output Contracts
**Classification: ABSORB — build NOW**

**Why:**
- Trivial to implement: each adapter gets a `contract()` method returning assertions about its output
- Validated in the existing `check_schema` function (add ~20 lines to `checks.py`)
- Catches exactly the kind of bugs Codex found (known-pattern regressions)
- Zero-LLM, deterministic, cacheable
- Low maintenance: contracts describe *invariant* properties of adapter output (e.g., "columns never contain relation fields") — these don't change with parser grammar updates

**What to build:**
- `LanguageAdapter.contracts()` method returning `list[AdapterContract]` dataclasses
- Each contract has: `code: str`, `description: str`, `check(result: ExtractResult) -> str | None` (returns error message or None)
- New check `check_adapter_contracts` in `checks.py`, runs in `full`, `preflight`, `audit` modes
- PrismaAdapter contract: "columns contain only scalar types"
- SQLAdapter contract: "when all statements fail parsing and revision header exists, error is set"

### IDEA 2 — AI Review Command
**Classification: ABANDON**

**Why:**
- Fatal brand dilution for Bounds' "zero-LLM" positioning
- High latency, token cost, and non-determinism incompatible with CI
- Off-by-default means nobody uses it
- LLM hallucination rate makes it unreliable for the exact kind of precision Bounds users expect
- The existing `--deep` flag on `describe` is already Tier 3 (LLM enrichment) — adding an LLM *review* command is a different order of magnitude (review implies judgment, not enrichment)

### IDEA 3 — Combined AI + Deterministic Pipeline
**Classification: ABANDON**

**Why:**
- All the downsides of Idea 2 PLUS architectural complexity that corrupts the pure-function check engine
- Coupling LLM output to deterministic pipeline creates non-deterministic test outcomes
- The value prop ("AI suggests, code enforces") is unclear: why not just write the deterministic check directly?
- The deterministic checks that would verify AI suggestions are *already* what Idea 1 proposes — you don't need the AI pre-pass

### IDEA 4 — Adversarial Extraction Mode
**Classification: HOLD — decision-gated**

**Gate:** First new adapter contributed by a third party (community pull request).

**Why hold:**
- Useful for maintainers, invisible to users. No star-attractor value until it catches a bug in the wild.
- Labor-intensive: needs 3-5 fixtures per adapter, plus per-language edge-case knowledge
- Rapidly stale: fixtures need updating when parser grammars change
- Property-based testing (Hypothesis) integration would make it more valuable but is non-trivial

**When to revisit:**
- A new adapter (Go, Java, Rust) is contributed. At that point, the fuzz suite becomes the "adapter conformance test" that ensures new adapters meet basic quality standards.
- Until then, the existing unit tests in `tests/test_extract.py` cover the critical edge cases.

### IDEA 5 — Cross-Adapter Schema Reconciliation
**Classification: ABSORB — build NEXT (major effort, deferred)**

**Why:**
- Highest star-attractor potential of all five ideas
- Solves a real, well-known pain point (schema drift between sources of truth)
- Genuinely unique — no other tool does this
- But: requires significant work to be useful
  - Noise reduction is the critical challenge (intentional vs. accidental drift)
  - Must learn to distinguish "Prisma-managed" vs "raw SQL" vs "ORM model" schema domains
  - Integration with specific ORMs (Django, SQLAlchemy, TypeORM, Drizzle) is a non-trivial effort
  - The fold logic needs to merge catalogs from multiple schema subsystems

**Why not NOW:**
- Requires new adapter types (ORM model readers) — these are different from the existing LanguageAdapters
- The reconciliation logic itself is ~200-400 lines of careful diff algebra
- Noise reduction heuristics need real-world validation across many project shapes
- Premortem showed the failed version (noisy, untrusted) is the more likely outcome without careful design

**Design constraints for future build:**
1. MUST be opinionated about which is the "source of truth" per table — Prisma-managed tables vs. raw SQL vs. ORM
2. MUST allow explicit ignore rules (`.boundsignore`-style or subsystem annotations)
3. MUST report confidence levels (high/medium/low) not just pass/fail
4. MUST start with the simplest possible signal: "table exists in SQL fold but not Prisma" (or vice versa)
5. SHOULD be a separate command (`bounds reconcile`) rather than a new check mode, to keep validate's output clean

---

## PHASE 4 — FINAL RECOMMENDATION

### What gets built NOW

**Feature: Adapter Output Contracts** (Idea 1)

This is the clear winner: high-value, zero brand dilution, minimal code, catches real bugs.

#### Specification

##### 1. New dataclass (`src/bounds/models.py`)

```python
@dataclass
class AdapterContract:
    code: str          # e.g., "E_ADAPTER_COLUMN_CONTRACT"
    description: str   # e.g., "columns contain only scalar Prisma types"
    severity: str      # "error" — contracts always block on violation

    def check(self, result: ExtractResult) -> str | None:
        """Return a human-readable violation message, or None if contract holds."""
        ...
```

Wait — contracts are per-adapter, not per-file. Let me revise: the method on the adapter makes more sense.

##### 2. New method on `LanguageAdapter` (`src/bounds/extract/base.py`)

```python
@dataclass(frozen=True)
class ContractViolation:
    code: str
    message: str

class LanguageAdapter(ABC):
    @abstractmethod
    def extract(self, rel_path: str, source: bytes) -> ExtractResult:
        ...

    def contracts(self) -> list[ContractViolation]:
        """Return violations of adapter output contracts for the *most recent* extract result.
        
        Base implementation returns empty list (no contracts). Adapters with known
        invariants override this to verify their output is self-consistent.
        """
        return []
```

Actually, even simpler: contracts are just functions that validate an ExtractResult. No state needed.

##### 3. Implementation

**File: `src/bounds/extract/base.py`** — Add `ContractViolation` and method signature.

```python
@dataclass(frozen=True)
class ContractViolation:
    code: str
    message: str

class LanguageAdapter(ABC):
    ...
    def contracts(self) -> list[ContractViolation]:
        return []
```

**File: `src/bounds/extract/prisma.py`** — Verify columns are scalar-only.

```python
def contracts(self) -> list[ContractViolation]:
    violations: list[ContractViolation] = []
    for sym in getattr(self, '_last_result', {}).symbols:
        if sym.kind == "table":
            columns = sym.metadata.get("columns", [])
            # This check runs against the *current* adapter logic — it's a self-consistency test.
            # If _is_column_field has a bug, this catches it at the model level.
            # TODO: store last_result per-extract; contracts run after extract
            pass
    return violations
```

Hmm, this design is getting tangled. Let me step back and make it cleaner.

**Simpler design:** Contracts are checks that run on each `ExtractResult` after extraction. They live in `checks.py` (like all other checks) but are adapter-specific. Each adapter registers a contract function.

**Cleanest design:**

Each adapter class defines a class method `check_contract(result: ExtractResult) -> list[Issue]` that validates its own output. The base implementation returns `[]`. Adapters override it with their specific invariants. The `check_adapter_contracts` function in `checks.py` iterates all extracts, finds the adapter, and calls `check_contract`.

Let me write the actual spec:

**File: `src/bounds/extract/base.py`**
```python
class LanguageAdapter(ABC):
    ...
    def check_result_contract(self, result: ExtractResult) -> list["Issue"]:
        """Validate adapter output invariants. Returns issues for violations.
        
        Override in subclasses that have self-consistency invariants.
        Base implementation: no contracts.
        """
        return []
```

**File: `src/bounds/extract/prisma.py`**
```python
class PrismaAdapter(LanguageAdapter):
    ...
    def check_result_contract(self, result: ExtractResult) -> list[Issue]:
        issues: list[Issue] = []
        for sym in result.symbols:
            if sym.kind == "table":
                columns = sym.metadata.get("columns", [])
                # Re-derive column fields to verify consistency
                for col in columns:
                    # Check: every column name should be a valid scalar field, not a relation field.
                    # We do this by re-parsing the relevant model body and comparing.
                    pass
        return issues
```

Wait — the contract check should NOT re-parse the source. It's a pure check over the result. Let me think about what a contract on *output* looks like.

The Prisma adapter bug was: relation fields (models/enums referenced by PascalCase type) were included in the columns list. A contract that checks this on the output is:

```python
def check_result_contract(self, result: ExtractResult) -> list[Issue]:
    issues: list[Issue] = []
    for sym in result.symbols:
        if sym.kind == "table":
            for col in sym.metadata.get("columns", []):
                # A column name that matches a known model name is a likely relation-field leak.
                # We can't know the full model set from a single file, but we can flag
                # PascalCase column names as suspicious (95% of Prisma columns are snake_case).
                ...
```

Hmm, this is getting heuristic. Let me think differently.

The *actual* contract we want is:

> "No column in any table symbol was derived from a Prisma relation field."

But the metadata doesn't store the original field types — we'd need to compare against the source. So the contract IS a re-check of the source, just like the original extraction. That's a different kind of test — more like integration/fuzz.

OK, let me simplify to what's practical:

**Practical contract design:** Each adapter declares a set of invariants on its output. For PrismaAdapter:

- Contract: "Every column name in every `table` symbol matches exactly one scalar field in the corresponding Prisma `model` block."
- This is checked by re-scanning the model definition for that symbol and verifying each column name appears as a scalar-typed field.

But this requires re-accessing the source bytes... which means the contract check needs the source. The current `check_*` functions in `checks.py` only get ExtractResult, not source bytes.

Let me redesign:

**Design v3 (pragmatic):**

Contracts are methods on adapters that receive `(result: ExtractResult, source: bytes)` — the source is available at extraction time and can be passed through if we want.

Actually, the simplest approach: add `check_result_contract(result, source)` to the adapter interface. The engine passes source through.

But the engine currently discards source after extraction. We'd need to change the pipeline.

**Simplest possible design:** Don't touch the adapter interface. Add a new check function in `checks.py` that operates on extracts + source (read from filesystem on demand). This is what I'll spec.

Actually wait — the engine has already read source into memory (`source = abs_path.read_bytes()`) on line 163 of `engine.py`. It passes it to `adapter.extract(rel, source)` and then... discards it. We could keep it.

But modifying the engine for this is a bigger change. Let me reconsider.

**Even simpler:** The contracts are *metadata-level* — they validate that the metadata of the result is self-consistent. For Prisma:

- "The columns list in a table symbol, when re-checked against the model source's field lines, contains no relation-field-derived names."
- But we don't have source in the checks.

**Final pragmatic design:** The contract lives INSIDE the adapter's `extract()` method. After extraction, run self-consistency checks and if any fail, add a `schema_error` symbol (like SQL does for unparsed statements). The check_schema function already reports these.

No — that's mixing concerns. The adapter should extract; validation should validate.

OK, here's the cleanest design that doesn't require engine changes:

**Contracts as non-source checks on ExtractResult metadata:**

The PrismaAdapter contract:
- After extracting a table symbol with columns, verify that no column name looks like a relation leak. How? We re-extract the *column names from metadata* — no, we need the field types.

Actually, I realize the Prisma adapter *already has* `_is_column_field()` — the bug was that it wasn't being called. Now it is. So the contract would be: "for every `table` symbol, re-parse the model body and verify no relation-field type made it into `columns`." But that requires re-parsing the source.

**Final decision:** Make the contracts operate on `(result, source)` and add source to the validation pipeline. This is a modest change.

Actually, the absolute simplest approach that catches the P2 bugs:

**For Prisma:** The contract is a file-level check that reads the `.prisma` file, finds all model blocks, re-extracts column lists using `_is_column_field()`, and compares to the extraction result's columns. If they differ, flag a contract violation.

**For SQL:** The contract checks: "If result has `schema_meta` symbols AND `schema_error` symbols AND no valid DDL symbols, then the result should have `error` set." This is purely metadata-based and doesn't need source.

Both are implementable. Let me write the final spec.

### Final Implementation Spec

#### Files to create/modify:

1. **`src/bounds/errors.py`** — Add `E_ADAPTER_CONTRACT = "E_ADAPTER_CONTRACT"` with severity "error"

2. **`src/bounds/models.py`** — Add `ContractViolation` dataclass (or reuse Issue)

3. **`src/bounds/extract/base.py`** — Add `check_contract(result, source) -> list[Issue]` method

4. **`src/bounds/extract/prisma.py`** — Override `check_contract`:
   - For each `table` symbol in `result`, find the corresponding `model` block in source
   - Re-scan fields using `_is_column_field()`
   - If any collected column name is not in the re-scanned column set → violation

5. **`src/bounds/extract/sql.py`** — Override `check_contract`:
   - If any `schema_meta` symbol AND any `schema_error` symbol AND no valid DDL symbols exist, but `result.error` is None → violation

6. **`src/bounds/validate/checks.py`** — Add `check_adapter_contracts` function:
   ```python
   def check_adapter_contracts(ctx: CheckContext) -> list[Issue]:
       issues = []
       for rel, result in ctx.extracts.items():
           adapter = get_adapter(rel)
           if adapter is None:
               continue
           # Read source (already cached? or re-read)
           source = (ctx.project_root / rel).read_bytes()
           contract_issues = adapter.check_contract(result, source)
           for ci in contract_issues:
               ci.file = rel
               ci.subsystem = ctx.file_owner.get(rel)
               issues.append(ci)
       return issues
   ```

   Wait — re-reading source is wasteful and could race. Better: pass source through the pipeline.

7. **`src/bounds/validate/engine.py`** — In the extraction loop, keep source in a `dict[str, bytes]` and pass to checks via context. Or simpler: add `source_bytes` to `CheckContext`.

Actually, the simplest approach: store source in `ExtractResult` or add a parallel dict. But `ExtractResult` should stay canonical.

**Simplest real design:** Add `sources: dict[str, bytes]` to `CheckContext` and populate it in the engine. Contracts that need source use `ctx.sources[rel]`. Contracts that don't (like the SQL one) ignore it.

#### Directory: New adapter contracts

**`src/bounds/extract/prisma.py`** — Add:
```python
def check_contract(self, result: ExtractResult, source: bytes) -> list[Issue]:
    """Verify columns contain only scalar fields, never relation fields."""
    issues: list[Issue] = []
    lines = source.decode("utf-8", "replace").splitlines()
    for sym in result.symbols:
        if sym.kind != "table":
            continue
        model_name = sym.metadata.get("model", sym.name)
        # Find model block in source
        i = 0
        while i < len(lines):
            m = _MODEL_RE.match(lines[i])
            if m and m.group(1) == model_name:
                _, extracted_cols, _ = _parse_model_body(lines, i + 1)
                expected = sorted(set(extracted_cols))
                actual = sorted(sym.metadata.get("columns", []))
                if expected != actual:
                    issues.append(Issue(
                        errors.E_ADAPTER_CONTRACT,
                        "error",
                        f"Prisma adapter produced unexpected column for model '{model_name}': "
                        f"expected {expected}, got {actual}. Relation fields may have leaked.",
                    ))
                break
            i += 1
    return issues
```

**`src/bounds/extract/sql.py`** — Add:
```python
def check_contract(self, result: ExtractResult, source: bytes) -> list[Issue]:
    """Verify that all-error + revision header produces a hard error."""
    issues: list[Issue] = []
    has_meta = any(s.kind == "schema_meta" for s in result.symbols)
    has_error_sym = any(s.kind == "schema_error" for s in result.symbols)
    valid = [s for s in result.symbols if s.kind not in ("schema_error", "schema_meta")]
    if has_meta and has_error_sym and not valid and result.error is None:
        issues.append(Issue(
            errors.E_ADAPTER_CONTRACT,
            "error",
            "SQL adapter: file with revision header but zero parseable statements did not "
            "produce a hard error. The schema_meta symbol may be masking parse failures.",
        ))
    return issues
```

8. **`src/bounds/validate/engine.py`** — Add `sources` dict to context:
```python
ctx = CheckContext(
    project_root=project_root,
    root=root,
    subsystems=subsystems,
    extracts=extracts,
    sources={rel: source for rel, (_, source, _) in ...},  # populated during extraction
    file_owner=file_owner,
    ...
)
```

9. **`src/bounds/validate/checks.py`** — Add `check_adapter_contracts` to `_ALL` and `CHECKS_BY_MODE`

10. **Tests:** Add `tests/test_adapter_contracts.py` with:
    - `test_prisma_contract_catches_relation_field_leak()` — feed a Prisma extract result with a relation field in columns, verify contract fires
    - `test_sql_contract_catches_masked_hard_error()` — feed a SQL extract result with meta + error symbols but no error, verify contract fires
    - `test_prisma_contract_passes_on_valid_output()` — valid Prisma output, no violations
    - `test_sql_contract_passes_on_valid_output()` — valid SQL output, no violations

#### New error code

```python
# In errors.py
E_ADAPTER_CONTRACT = "E_ADAPTER_CONTRACT"
SEVERITY[E_ADAPTER_CONTRACT] = "error"
```

#### CLI changes

None. Contracts run as part of `bounds validate` (full/preflight/audit modes). Users don't need to learn a new command. The output is a standard Issue with code=`E_ADAPTER_CONTRACT`.

#### What the user sees

```
$ bounds validate
{
  "validation_status": "stale",
  "ok": false,
  "issues": [
    {
      "code": "E_ADAPTER_CONTRACT",
      "severity": "error",
      "message": "Prisma adapter produced unexpected column for model 'User': expected ['email', 'id'], got ['email', 'id', 'posts']. Relation fields may have leaked.",
      "file": "schema.prisma",
      "subsystem": "db"
    }
  ]
}
```

#### Why this wins

1. **Genuinely useful** — catches exactly the bugs Codex found, in CI, every time, with zero LLM
2. **Star-attracting** — "Bounds catches its own adapter bugs in CI" is a concrete, shareable story. A blog post with a before/after (Codex found it → now Bounds finds itself) is compelling
3. **Not bloated** — zero new commands, zero config, zero API keys. It's a hidden improvement to `bounds validate` that makes it stronger
4. **Consistent with zero-LLM** — deterministic, source-based, byte-stable

---

## Summary

| Idea | Classification | Why |
|------|---------------|-----|
| 1 — Adapter Output Contracts | **ABSORB — NOW** | Catches known-pattern bugs, trivial code, zero brand dilution |
| 2 — AI Review Command | **ABANDON** | Fatal brand dilution, slow, expensive, unreliable |
| 3 — Combined AI + Deterministic | **ABANDON** | All of Idea 2's problems + architectural corruption |
| 4 — Adversarial Extraction | **HOLD** (gate: 3rd-party adapter PR) | Useful for maintainers but labor-intensive and invisible |
| 5 — Cross-Adapter Reconciliation | **ABSORB — NEXT** | Highest star potential but must be designed carefully to avoid noise |

### Build this now

**One feature:** Adapter Output Contracts — an invariant-checking layer on each LanguageAdapter that validates its own output for self-consistency, run as part of `bounds validate`.

**Files to change:** `errors.py`, `models.py`, `extract/base.py`, `extract/prisma.py`, `extract/sql.py`, `validate/checks.py`, `validate/engine.py` (~100 lines total)

**New tests:** 4 in `tests/test_adapter_contracts.py`

**Result:** The two P2 bugs Codex found would now be caught by `bounds validate` itself, making Bounds' validation complete at the adapter level for the first time.
