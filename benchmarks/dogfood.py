#!/usr/bin/env python3
"""Bounds dogfood benchmark harness — mapping coverage + token economics.

Deterministic, model-agnostic. Reports BOTH of Bounds' headline value props on the Bounds repo
itself (dogfooding):

  1. **Mapping coverage** — how much of the repo's library source Bounds actually mapped, read from
     the CLI's own authoritative metric (`bounds validate` -> stats.coverage.mapping, i.e.
     src/bounds/extract/scan.py::mapping_coverage). 3-way per file: mapped / unowned-supported /
     unsupported-language (no "partial" tier — partial extraction is a separate signal). Plus
     tests/docs linkage.
  2. **Token economics** — how many tokens an agent spends to obtain a subsystem's structural
     contract via `bounds` vs reading the equivalent raw source.

Design constraints (see benchmarks/README.md):
  - Pure stdlib + OPTIONAL tiktoken (cl100k_base when present, else a labeled ~1-token/4-chars
    estimate — the tokenizer is printed so a submission states which produced its numbers).
  - No wall-clock, timestamps, or randomness in the emitted report. Output is byte-stable so it can
    be pasted straight into a results/ submission.

Usage:
    python benchmarks/dogfood.py          # or: make benchmark

Exits 0 on success.
"""
from __future__ import annotations

import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR))
from _lib import (  # noqa: E402 - shared benchmark helpers (the one home for these)
    REPO_ROOT,
    _make_counter,
    bounds_text,
    coverage_summary_lines,
    manifest_source_files,
    read_coverage,
    source_text,
)

MANIFEST_DIR = REPO_ROOT / ".bounds" / "manifests"

# The subsystem we profile per-command. `models` is core and stable, so the numbers are
# reproducible across releases.
SUBSYSTEM = "models"


def _subsystem_sources(name: str) -> list[Path]:
    """Source files a subsystem manifest's ``paths:`` resolve to (via the shared parser)."""
    return manifest_source_files(REPO_ROOT, name)


def _whole_map_source_text() -> str:
    """Concatenated source of every subsystem (the `bounds list` whole-map alternative)."""
    texts: list[str] = []
    for manifest in sorted(MANIFEST_DIR.glob("*.yaml")):
        texts.append(source_text(_subsystem_sources(manifest.stem)))
    return "".join(texts)


def main() -> int:
    count, tokenizer_label = _make_counter()

    sources = _subsystem_sources(SUBSYSTEM)
    src_text = source_text(sources)
    source_label = ", ".join(p.relative_to(REPO_ROOT).as_posix() for p in sources) or "(none)"

    coverage = read_coverage(REPO_ROOT)

    # (command, bounds-output-text, source-equivalent-text)
    rows = [
        (f"bounds describe {SUBSYSTEM}", bounds_text(["describe", SUBSYSTEM], REPO_ROOT), src_text),
        ("bounds list", bounds_text(["list"], REPO_ROOT), _whole_map_source_text()),
        (f"bounds impact {SUBSYSTEM}", bounds_text(["impact", SUBSYSTEM], REPO_ROOT), src_text),
    ]

    print("# Bounds benchmark — mapping coverage + token economics")
    print()
    print("- Repo: bounds (self / dogfood)")
    print(f"- Tokenizer: {tokenizer_label}")
    print()

    # --- Mapping coverage (authoritative; from `bounds validate`) ---
    print("## Mapping coverage")
    print()
    print("How much of the repo's library source Bounds mapped. 3-way per source file — mapped /")
    print("unowned-supported / unsupported-language (no partial tier). Source = the authoritative")
    print("`bounds validate` metric (`stats.coverage.mapping`); tests are excluded from the")
    print("denominator and tracked separately.")
    print()
    for line in coverage_summary_lines(coverage):
        print(line)
    print()

    # --- Token economics ---
    print("## Token economics")
    print()
    print(f"Subsystem profiled: `{SUBSYSTEM}` (source: `{source_label}`).")
    print("The real saving is TARGETED retrieval (`describe`/`impact`) vs reading the source; the")
    print("whole-map `bounds list` figure is the cheap-orientation bound, not the per-`describe`")
    print("number. Cite the range, not a single flat %.")
    print()
    print("| Command | Bounds tokens | Source-equivalent tokens | Reduction |")
    print("|---------|--------------:|-------------------------:|----------:|")

    total_bounds = 0
    total_source = 0
    for cmd, out_text, comparison in rows:
        bt = count(out_text)
        st = count(comparison)
        total_bounds += bt
        total_source += st
        reduction = (1 - bt / st) * 100 if st else 0.0
        print(f"| `{cmd}` | {bt:,} | {st:,} | {reduction:.1f}% |")

    agg = (1 - total_bounds / total_source) * 100 if total_source else 0.0
    print(f"| **aggregate** | **{total_bounds:,}** | **{total_source:,}** | **{agg:.1f}%** |")
    print()
    print(
        "Reduction = tokens saved by reading the Bounds contract instead of the equivalent source. "
        "`bounds list` source-equivalent is every subsystem's combined source (the whole-map "
        "alternative — cheap to orient, but few agents read a whole repo). The honest, repeatable "
        "win is targeted retrieval: one `describe`/`impact` vs the subsystem's source."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
