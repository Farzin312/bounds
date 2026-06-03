#!/usr/bin/env python3
"""Shared benchmark helpers — the ONE home for token counting, the `bounds` runner,
manifest-source resolution, subsystem selection, and the authoritative coverage read.

Every benchmark harness (``dogfood.py``, ``oss_bench.py``, ``oss_features.py``) imports from
here so the three never disagree on how a token is counted, how `bounds` is invoked, or what
"coverage" means. There is no third-party dependency: tiktoken is an OPTIONAL import (exact
cl100k_base when present, a clearly-labeled ~1-token/4-chars estimate otherwise).

Design constraints (mirrors src/bounds and benchmarks/README.md):
  - Deterministic + model-agnostic: no wall-clock, randomness, or set-ordering in anything that
    feeds a serialized table. (Latency lives in oss_bench only, behind an explicit field.)
  - Coverage is read from the CLI's OWN JSON (`bounds validate` -> stats.coverage.mapping), so the
    benchmarks stay decoupled from src/ internals and can never drift from the authoritative
    `scan.mapping_coverage` metric. There is NO benchmark-local coverage walk.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

# benchmarks/ lives directly under the repo root.
BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent

# Extensions counted as source when a manifest path points at a directory. Shared by every harness
# so the token denominator is identical across them. (Coverage % comes from the CLI, not this set;
# this only bounds the "read the equivalent source" comparison files.)
SOURCE_EXTS = {".py", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}


# --------------------------------------------------------------------------- #
# Token counting (optional tiktoken; deterministic char-based fallback)
# --------------------------------------------------------------------------- #
def _make_counter():
    """Return ``(count_fn, tokenizer_label)`` where ``count_fn(str) -> int``.

    Uses tiktoken ``cl100k_base`` (exact) when importable, otherwise a labeled ~1-token/4-chars
    estimate. The label is returned so every harness can print which tokenizer produced its numbers
    (cl100k/Claude/Gemini tokenize differently — ratios are stable, absolute counts are not).
    """
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
# CLI runner
# --------------------------------------------------------------------------- #
def bounds_bin() -> str:
    """Prefer the project venv's bounds; fall back to PATH."""
    venv_bin = REPO_ROOT / ".venv" / "bin" / "bounds"
    return str(venv_bin) if venv_bin.exists() else "bounds"


def run_bounds(args: list[str], cwd: Path) -> tuple[str, str, int, float]:
    """Run ``bounds <args>`` in ``cwd``; return ``(stdout, stderr, returncode, elapsed_seconds)``.

    Never raises on a non-zero exit — callers decide whether a non-zero rc is fatal (a benchmark
    of an unsupported repo deliberately observes failure as a measured outcome). The elapsed time
    is reported separately so it never contaminates a deterministic table.
    """
    t0 = time.perf_counter()
    proc = subprocess.run([bounds_bin(), *args], cwd=cwd, capture_output=True, text=True)
    return proc.stdout, proc.stderr, proc.returncode, time.perf_counter() - t0


def bounds_text(args: list[str], cwd: Path) -> str:
    """Run ``bounds <args>`` and return stdout; raise SystemExit(1) on a non-zero exit.

    For the dogfood harness, where any failure means the measurement is invalid and should stop.
    """
    out, err, rc, _ = run_bounds(args, cwd)
    if rc != 0:
        raise SystemExit(
            f"error: `bounds {' '.join(args)}` exited {rc}\n{err}"
        )
    return out


def bounds_json(args: list[str], cwd: Path) -> dict | list | None:
    """Run ``bounds <args>`` and parse stdout as JSON; return None on non-JSON / non-zero exit."""
    out, _, rc, _ = run_bounds(args, cwd)
    if rc != 0 and not out.strip():
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------- #
# Manifest -> source files (the "read the equivalent source" denominator)
# --------------------------------------------------------------------------- #
def manifest_source_files(root: Path, name: str) -> list[Path]:
    """Source files a subsystem manifest's ``paths:`` cover, under ``root``.

    The one parser shared by every harness so they never measure a different source set. Minimal
    stdlib YAML: a block-sequence item (``- x``) may sit flush-left or indented — treat it as an
    item, never a key; any other non-blank line is a mapping key that toggles the ``paths`` section.
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
    """A manifest path -> the source files it covers (the dir's sources, or the file itself)."""
    if candidate.is_dir():
        return sorted(f for f in candidate.rglob("*") if f.is_file() and f.suffix in SOURCE_EXTS)
    return [candidate] if candidate.is_file() else []


def source_text(paths: list[Path]) -> str:
    """Concatenated text of ``paths`` (errors ignored so a binary/odd file never crashes a run)."""
    return "".join(p.read_text(encoding="utf-8", errors="ignore") for p in paths)


def source_tokens(root: Path, name: str, count) -> int:
    """Token count of a subsystem's manifest-covered source (the read-the-source comparison)."""
    return count(source_text(manifest_source_files(root, name)))


# --------------------------------------------------------------------------- #
# Subsystem selection
# --------------------------------------------------------------------------- #
def list_subsystems(root: Path) -> list[dict]:
    """`bounds list` -> the subsystem records, normalized to a list of dicts (or [] on failure)."""
    data = bounds_json(["list"], root)
    if not isinstance(data, dict):
        return []
    subs = data.get("subsystems", [])
    if isinstance(subs, list):
        return subs
    if isinstance(subs, dict):
        return [dict(name=k, **(v if isinstance(v, dict) else {})) for k, v in subs.items()]
    return []


def pick_subsystem(subs: list[dict]) -> str | None:
    """The most-depended-on non-test subsystem — the one an agent most needs to understand.

    Ranks by (not-a-test, consumer count, exposed-symbol count). The one selector shared by every
    harness so they all profile the same "key" subsystem.
    """
    def score(s: dict):
        name = s.get("name", "")
        penalty = -1 if ("test" in name.lower()) else 0
        return (penalty, len(s.get("consumed_by", []) or []), _as_int(s.get("exposes", 0)))

    real = [s for s in subs if "test" not in s.get("name", "").lower()] or subs
    return max(real, key=score).get("name") if real else None


def _as_int(x, default: int = 0) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Authoritative coverage (read from the CLI's own JSON — never re-walked here)
# --------------------------------------------------------------------------- #
def read_coverage(root: Path) -> dict:
    """The authoritative mapping-coverage block for ``root``, read from `bounds validate` JSON.

    Returns the dict produced by ``src/bounds/extract/scan.py::mapping_coverage`` exactly as the CLI
    surfaces it at ``stats.coverage.mapping`` — ``mapped_pct`` (top level), the nested
    ``supported:{total,mapped,unowned,unowned_sample}`` and
    ``unsupported:{total,declared,dark,dark_sample,by_language}`` blocks, plus the ``tests`` and
    ``docs`` linkage buckets ``{total,linked,unlinked,unlinked_sample}``. Falls back to `bounds overview`'s
    ``health.validation.mapped_pct`` (+ tests/docs) if validate yields no coverage block. Returns
    ``{}`` if neither is available (e.g. an unsupported-language repo with no manifests) — a benign
    "no coverage measured" rather than a crash.

    Coverage is 3-way per source file — mapped / unowned-supported / unsupported-language. There is
    NO "partial" file tier; partial extraction is a separate signal (extraction_failures), reported
    on its own. Test files are excluded from the source denominator and tracked in ``tests``.
    """
    data = bounds_json(["validate"], root)
    if isinstance(data, dict):
        mapping = (
            data.get("stats", {}).get("coverage", {}).get("mapping")
            if isinstance(data.get("stats"), dict)
            else None
        )
        if isinstance(mapping, dict) and mapping:
            return mapping

    # Fallback: overview surfaces a thinner view (mapped_pct + tests/docs linked/unlinked).
    ov = bounds_json(["overview"], root)
    if isinstance(ov, dict):
        validation = ov.get("health", {}).get("validation", {})
        if isinstance(validation, dict) and "mapped_pct" in validation:
            return {
                "mapped_pct": validation.get("mapped_pct"),
                "tests": validation.get("tests", {}),
                "docs": validation.get("docs", {}),
                "_source": "overview (mapped_pct only; run validate for the full breakdown)",
            }
    return {}


def coverage_summary_lines(cov: dict) -> list[str]:
    """Render a coverage dict into human markdown lines (shared by dogfood.py / oss table footer).

    Reads the ACTUAL nested shape the CLI emits at ``stats.coverage.mapping`` —
    ``supported:{total,mapped,unowned}`` and ``unsupported:{dark,declared,by_language}`` — not the
    flat ``files_mapped``/``files_source_total`` keys an earlier version of this function assumed
    (those never existed in the emitted JSON, so file counts rendered as ``?``). ``mapped_pct`` and
    the ``tests``/``docs`` buckets are top-level and unchanged.
    """
    if not cov:
        return ["_No coverage measured (no .bounds manifests, or unsupported-language repo)._"]
    sup = cov.get("supported") or {}
    unsup = cov.get("unsupported") or {}
    lines = [
        f"- Mapped source: **{_fmt_pct(cov.get('mapped_pct'))}** "
        f"({sup.get('mapped', '?')} / {sup.get('total', '?')} supported non-test source files)",
    ]
    unowned = sup.get("unowned", 0)
    dark = unsup.get("dark", 0)
    declared = unsup.get("declared", 0)
    if unowned or dark or declared:
        lines.append(
            f"- Unmapped: {unowned} unowned-but-supported"
            f"  ·  unsupported-language: {dark} dark, {declared} hand-declared"
        )
    by_lang = unsup.get("by_language") or {}
    if by_lang:
        breakdown = ", ".join(f"{lang}: {n}" for lang, n in sorted(by_lang.items()))
        lines.append(f"- Unmapped by language: {breakdown}")
    tests = cov.get("tests") or {}
    if tests:
        lines.append(
            f"- Tests linkage: {tests.get('linked', '?')} linked / "
            f"{tests.get('unlinked', '?')} unlinked (of {tests.get('total', '?')})"
        )
    docs = cov.get("docs") or {}
    if docs:
        lines.append(
            f"- Docs linkage: {docs.get('linked', '?')} linked / "
            f"{docs.get('unlinked', '?')} unlinked (of {docs.get('total', '?')})"
        )
    return lines


def _fmt_pct(value) -> str:
    if isinstance(value, (int, float)):
        return f"{value:.1f}%"
    return str(value)
