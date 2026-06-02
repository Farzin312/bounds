# Mapping coverage — aiming for 100%, honest when it isn't

Bounds' goal is to map a repo's structural surface **deterministically, without an LLM**, and to map
**all of it**. When it can't — an unsupported language, a directory no manifest covers — it must say
so loudly and tell you the next step, never silently leave half the repo dark. This page explains the
coverage signal and how a human *or an AI* closes a gap.

## The signal

`bounds validate` and `bounds discover` report mapping coverage over the repo's **library source
code** (docs/config/assets are excluded so they can't dilute the number; **test files are excluded
too** and tracked in their own bucket — see *Docs & tests* below):

```jsonc
// bounds validate  →  stats.coverage.mapping
{
  "files_source_total": 8,             // NON-TEST library source only
  "files_mapped": 3,
  "files_unmapped": 5,
  "mapped_pct": 37.5,                   // over non-test library source
  "unmapped_unowned_supported": 0,     // supported language, just not in a manifest yet
  "unmapped_unsupported_language": 5,  // no adapter for this language yet
  "unmapped_by_language": { "go": 5 },
  "unsupported_languages": ["go"],
  // Informational linkage buckets — tracked, NEVER a blocking gap (see "Docs & tests"):
  "tests": { "total": 12, "linked": 11, "unlinked": 1, "unlinked_sample": ["tests/misc.py"] },
  "docs":  { "total": 6,  "linked": 4,  "unlinked": 2, "unlinked_sample": ["docs/notes.md"] }
}
```

When `files_unmapped > 0`, `validate` emits one **loud, non-blocking** `E_COVERAGE_GAP` warning and
`discover` adds a `next_step` — e.g. *"mapped 37.5% of library source (3/8 non-test files); unmapped:
5 in unsupported languages (go×5) (tests/docs are tracked separately, never a gap)"* with the fix
below. It is a **warning, not an error**: an incomplete map is honest, not a CI failure on its own
(you opt into stricter gates — see *CI* below). **The gap fires only on unmapped non-test library
source** — a repo's tests can never drag the % down or be flagged.

Two kinds of gap, two fixes:

| Gap | What it means | How to close it |
|-----|---------------|-----------------|
| **unowned-supported** | Bounds *has* an adapter (Python/TS/JS/SQL/Prisma) but the file is in no subsystem's `paths` | add it to a manifest — deterministic, no AI needed |
| **unsupported language** | no adapter yet (Go, Rust, Java, …) | hand-author (or AI-author) a manifest in the format below, then verify |

## Docs & tests — mapping source ↔ docs ↔ tests

A subsystem can link the **docs** and **test** files that cover it, so coverage maps the full triangle
of source ↔ docs ↔ tests. This uses a **hybrid model**: optional explicit manifest fields plus
convention auto-detection, with **explicit always winning**.

```yaml
name: auth
role: library
paths:
  - src/auth
docs:                      # optional, authoritative — repo-relative file / dir / glob (same shape as paths)
  - docs/auth.md
tests:                     # optional, authoritative
  - tests/auth             # a whole directory
  - tests/test_auth_edge.py
```

- **Explicit `docs:`/`tests:`** are human-curated and authoritative — the most-specific declaration
  wins, exactly like `paths`/`files`.
- **Convention auto-detection** supplements them with zero config:
  - a test file directly under a subsystem's `paths`, or under `tests/<name>/`, `test/<name>/`,
    `__tests__/<name>/`, or named `test_<name>.py` / `<name>.test.ts` / `<name>.spec.ts`, links to a
    subsystem named `<name>`;
  - a doc `docs/<name>.*` or `<name>.md` whose stem equals a subsystem name links to it.
- **`bounds discover` auto-populates `tests:`** (and `docs:`) from convention on a fresh run, so a new
  repo already maps its tests with little or no hand-editing (a whole directory collapses to one glob
  to keep manifests token-lean).

**Tests and docs are tracked, never a blocking gap.** They are excluded from the source denominator
(`mapped_pct` is over non-test library source) and reported only in the informational `tests`/`docs`
buckets — an unlinked test or doc is surfaced (so you *can* link it) but never fires `E_COVERAGE_GAP`
and never fails CI. This is deliberate: no repo's tests get flagged as an unmapped-source gap by
default. `bounds describe <name> --full` shows a subsystem's linked docs/tests (explicit + convention).

## 100%-or-guidance

Library source aims for **100% mapped**, and when a gap remains the report names the **exact minimal
manifest action** to close it (add the file to a subsystem's `paths:`, or scaffold one with
`bounds init --subsystem <name>`). Tests and docs are *tracked* toward full linkage but are never a
blocking gap — so "100%" is a goal for source, and a visible, opt-in target for docs/tests.

## Closing a gap (humans and AI)

The goal is always **deterministic mapping first**; AI is the fallback for languages Bounds can't yet
parse. Either way the format is the same and the result is verifiable.

1. **Scaffold a subsystem:** `bounds init --subsystem <name>` writes
   `.bounds/manifests/<name>.yaml` and tells you to add `<name>` to `root.yaml`'s `subsystems:` list.
2. **Point it at the files** — edit `paths:` to the directory/files the subsystem owns.
3. **Declare the public surface** in `exposes:` (for an unsupported language Bounds can't extract
   this, so you or an AI list the exported symbols by hand). Minimal valid manifest:

   ```yaml
   name: payments-go            # required
   role: library                # required: service | platform | connector | library
   criticality: core            # optional (default: leaf)
   paths:
     - services/payments        # the files this subsystem owns
   exposes:                     # the public surface (hand-authored for unsupported langs)
     - { name: Charge, kind: function }
     - { name: Refund, kind: function }
   consumes:
     - { subsystem: models, interfaces: [Invoice] }   # list interfaces so orphan/boundary checks work
   ```

4. **Verify:** `bounds validate` — coverage should rise and the manifest should be drift-free. Re-run
   until `mapped_pct` is where you want it.

**For AI agents filling a gap:** read the format above and the existing `.bounds/manifests/*.yaml` as
examples, author the missing manifest(s), add the names to `root.yaml`, then run `bounds validate`
and fix anything it reports. Declare `consumes` **with `interfaces`** (not just the subsystem) so
boundary and orphan checks have something to judge against. The CLI is the verifier — a manifest you
can't `validate` clean isn't done.

## Tracking coverage over time

Coverage is a metric we want to push toward 100% by improving the algorithms (better discovery, more
adapters), so it is recorded, not hidden:

- Per repo, on every `validate`/`discover` run (`stats.coverage.mapping`).
- Across the OSS corpus in
  [benchmarks/results/oss-cross-language.md](../benchmarks/results/oss-cross-language.md), regenerated
  by `python benchmarks/oss_bench.py` / `oss_features.py`.

**Report a gap.** If Bounds left source unmapped that you think it should have handled — a supported
file it missed, or a language you want supported — open an issue with the repo and the
`unmapped_by_language` breakdown. That data is how the supported-language list and the discovery
heuristics get better. Contributors: see [known-issues.md](known-issues.md) and [testing.md](testing.md).

## CI: how coverage and partial maps interact with the gate

The gate (`bounds preflight`, or `bounds validate --enforce on`) blocks on **errors**, not warnings:

- `E_COVERAGE_GAP` is a **warning** — an incomplete map never fails CI by itself (you can map a repo
  incrementally without a red build).
- Want unmapped *supported* files to be a hard failure? `bounds validate --fail-on-unowned` promotes
  a tracked, supported file that is in no subsystem to a blocking `E_UNOWNED_FILE` error (files
  matching a `root.entry_points` glob stay non-blocking).
- Real contract problems still block: structural drift, boundary violations, cycles, missing exports,
  invalid schema (see the severity table in [known-issues.md](known-issues.md) / `errors.py`).
- Overlapping/nested subsystem paths are resolved deterministically (the deepest path owns the file —
  see [BOUNDS-001]); a genuine same-path conflict diagnostic is tracked as [BOUNDS-006].

So a polyglot repo with an unsupported language can still pass CI (the gap is an honest warning),
while the structural contract for the languages Bounds *does* map is enforced.
