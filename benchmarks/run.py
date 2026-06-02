#!/usr/bin/env python3
"""Bounds token-economics benchmark harness.

Deterministic, model-agnostic. Measures how many tokens an AI agent spends to
obtain a subsystem's structural contract via the `bounds` CLI versus reading the
equivalent raw source. Runs against the Bounds repo itself (dogfooding).

Design constraints (see benchmarks/README.md):
  - Pure stdlib + OPTIONAL tiktoken. tiktoken is NOT a project dependency; if it
    is importable we use the cl100k_base encoding, otherwise we fall back to a
    clearly-labeled estimate of ~1 token per 4 characters.
  - No wall-clock, no timestamps, no randomness in the emitted table. Output is
    byte-stable across runs so it can be pasted into a results/ submission.
  - Token counts are tokenizer-specific. The chosen tokenizer is printed in the
    header so a contributor's submission states which tokenizer produced the
    numbers (Claude, Gemini, etc. tokenize differently than cl100k_base).

Usage:
    python benchmarks/run.py          # or: make benchmark

Exits 0 on success.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# benchmarks/ lives directly under the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_DIR = REPO_ROOT / ".bounds" / "manifests"

# The subsystem we profile per-command. `models` is core and stable, so the
# numbers are reproducible across releases.
SUBSYSTEM = "models"

# Extensions counted as source when a manifest path points at a directory. Shared by both
# harnesses (run.py / oss_run.py) so the two never count a different source set.
SOURCE_EXTS = {".py", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}


# --------------------------------------------------------------------------- #
# Token counting (optional tiktoken; deterministic char-based fallback)
# --------------------------------------------------------------------------- #
def _make_counter():
    """Return (count_fn, tokenizer_label). count_fn(str) -> int."""
    try:
        import tiktoken  # type: ignore

        enc = tiktoken.get_encoding("cl100k_base")
        return (lambda s: len(enc.encode(s)), "tiktoken cl100k_base (exact)")
    except Exception:
        # ~1 token per 4 characters is the common cl100k-class heuristic.
        return (
            lambda s: (len(s) + 3) // 4,
            "estimate ~1 token / 4 chars (tiktoken not installed)",
        )


# --------------------------------------------------------------------------- #
# CLI / source helpers
# --------------------------------------------------------------------------- #
def _bounds_bin() -> str:
    """Prefer the project venv's bounds; fall back to PATH."""
    venv_bin = REPO_ROOT / ".venv" / "bin" / "bounds"
    return str(venv_bin) if venv_bin.exists() else "bounds"


def _run_bounds(args: list[str]) -> str:
    """Run `bounds <args>` from the repo root and return stdout (text)."""
    proc = subprocess.run(
        [_bounds_bin(), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(
            f"error: `bounds {' '.join(args)}` exited {proc.returncode}\n"
            f"{proc.stderr}\n"
        )
        raise SystemExit(1)
    return proc.stdout


def _subsystem_sources(name: str) -> list[Path]:
    """Read a subsystem manifest's `paths:` and resolve to existing files.

    Minimal YAML parsing (stdlib only): collect lines under a top-level
    `paths:` key, each `  - some/path`, until the next top-level key.
    """
    manifest = MANIFEST_DIR / f"{name}.yaml"
    if not manifest.exists():
        return []
    return manifest_source_files(REPO_ROOT, name)


def manifest_source_files(root: Path, name: str) -> list[Path]:
    """Source files a subsystem manifest's ``paths:`` cover, under ``root``.

    The one parser shared by both harnesses (imported by oss_run.py) so they never measure a
    different source set. Minimal stdlib YAML: a block-sequence item (``- x``) may sit flush-left
    or indented — treat it as an item, never a key; any other non-blank line is a mapping key that
    toggles the ``paths`` section.
    """
    manifest = root / ".bounds" / "manifests" / f"{name}.yaml"
    if not manifest.exists():
        return []
    paths: list[Path] = []
    in_paths = False
    for raw in manifest.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            if in_paths:
                paths.extend(_expand_source(root / stripped[2:].strip().strip("'\"")))
            continue
        in_paths = stripped.startswith("paths:")
    return paths


def _expand_source(candidate: Path) -> list[Path]:
    """A manifest path → the source files it covers (the dir's sources, or the file itself)."""
    if candidate.is_dir():
        return sorted(f for f in candidate.rglob("*") if f.is_file() and f.suffix in SOURCE_EXTS)
    return [candidate] if candidate.is_file() else []


def _source_text(paths: list[Path]) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in paths)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    count, tokenizer_label = _make_counter()

    sources = _subsystem_sources(SUBSYSTEM)
    source_text = _source_text(sources)
    source_label = ", ".join(p.relative_to(REPO_ROOT).as_posix() for p in sources) or "(none)"

    # (command, bounds-output-text, source-equivalent-text)
    rows = [
        (
            f"bounds describe {SUBSYSTEM}",
            _run_bounds(["describe", SUBSYSTEM]),
            source_text,
        ),
        (
            "bounds list",
            _run_bounds(["list"]),
            # Whole-map equivalent: reading every subsystem's sources.
            _whole_map_source_text(),
        ),
        (
            f"bounds impact {SUBSYSTEM}",
            _run_bounds(["impact", SUBSYSTEM]),
            source_text,
        ),
    ]

    print("# Bounds token-economics benchmark")
    print()
    print("- Repo: bounds (self / dogfood)")
    print(f"- Subsystem profiled: `{SUBSYSTEM}` (source: `{source_label}`)")
    print(f"- Tokenizer: {tokenizer_label}")
    print()
    print("| Command | Bounds tokens | Source-equivalent tokens | Reduction |")
    print("|---------|--------------:|-------------------------:|----------:|")

    total_bounds = 0
    total_source = 0
    for cmd, out_text, src_text in rows:
        bt = count(out_text)
        st = count(src_text)
        total_bounds += bt
        total_source += st
        reduction = (1 - bt / st) * 100 if st else 0.0
        print(f"| `{cmd}` | {bt:,} | {st:,} | {reduction:.1f}% |")

    agg = (1 - total_bounds / total_source) * 100 if total_source else 0.0
    print(f"| **aggregate** | **{total_bounds:,}** | **{total_source:,}** | **{agg:.1f}%** |")
    print()
    print(
        "Reduction = tokens saved by reading the Bounds contract instead of the "
        "equivalent source. `bounds list` source-equivalent is every subsystem's "
        "combined source (the whole-map alternative)."
    )
    return 0


def _whole_map_source_text() -> str:
    """Concatenated source of every subsystem (the `bounds list` alternative)."""
    texts: list[str] = []
    for manifest in sorted(MANIFEST_DIR.glob("*.yaml")):
        texts.append(_source_text(_subsystem_sources(manifest.stem)))
    return "".join(texts)


if __name__ == "__main__":
    raise SystemExit(main())
