# Mapping coverage — aiming for 100%, honest when it isn't

Bounds' goal is to map a repo's structural surface **deterministically, without an LLM**, and to map
**all of it**. When it can't — an unsupported language, a directory no manifest covers — it must say
so loudly and tell you the next step, never silently leave half the repo dark. This page explains the
coverage signal and how a human *or an AI* closes a gap.

## The signal

`bounds validate` and `bounds discover` report mapping coverage over the repo's **source code**
(docs/config/assets are excluded so they can't dilute the number):

```jsonc
// bounds validate  →  stats.coverage.mapping
{
  "files_source_total": 8,
  "files_mapped": 3,
  "files_unmapped": 5,
  "mapped_pct": 37.5,
  "unmapped_unowned_supported": 0,     // supported language, just not in a manifest yet
  "unmapped_unsupported_language": 5,  // no adapter for this language yet
  "unmapped_by_language": { "go": 5 },
  "unsupported_languages": ["go"]
}
```

When `files_unmapped > 0`, `validate` emits one **loud, non-blocking** `E_COVERAGE_GAP` warning and
`discover` adds a `next_step` — e.g. *"mapped 37.5% of source (3/8 files); unmapped: 5 in unsupported
languages (go×5)"* with the fix below. It is a **warning, not an error**: an incomplete map is
honest, not a CI failure on its own (you opt into stricter gates — see *CI* below).

Two kinds of gap, two fixes:

| Gap | What it means | How to close it |
|-----|---------------|-----------------|
| **unowned-supported** | Bounds *has* an adapter (Python/TS/JS/SQL/Prisma) but the file is in no subsystem's `paths` | add it to a manifest — deterministic, no AI needed |
| **unsupported language** | no adapter yet (Go, Rust, Java, …) | hand-author (or AI-author) a manifest in the format below, then verify |

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
