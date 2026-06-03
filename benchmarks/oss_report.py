#!/usr/bin/env python3
"""Combined OSS report for ONE cloned repo: runs oss_bench + oss_features and prints ONE markdown
report covering BOTH mapping coverage and token economics, plus the command-surface health.

This closes the "tables hand-assembled" reproducibility hole: instead of running the two JSON
harnesses and pasting numbers into a table by hand, this runs both and emits a finished markdown
block you can drop straight into a results/ submission.

Deterministic + model-agnostic (no wall-clock in the markdown; latency stays inside the JSON
harnesses' own fields). Backed by the same `_lib` helpers as every other harness.

Usage:
    python benchmarks/oss_report.py --repo /path/to/cloned/repo [--name flask] [--lang python]
    make oss-bench REPO=/path/to/cloned/repo
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR))
import oss_bench  # noqa: E402 - run the per-repo engine in-process
import oss_features  # noqa: E402 - run the full-command smoke matrix in-process
from _lib import _make_counter, coverage_summary_lines  # noqa: E402


def _fmt_int(value, default: str = "?") -> str:
    return f"{value:,}" if isinstance(value, int) else default


def _fmt_pct(value) -> str:
    return f"{value:.1f}%" if isinstance(value, (int, float)) else str(value)


def render(bench: dict, feats: dict) -> str:
    name = bench.get("name", "?")
    lang = bench.get("lang", "?")
    out: list[str] = []
    out.append(f"# Bounds OSS report — `{name}` ({lang})")
    out.append("")
    out.append(f"- Tokenizer: {bench.get('tokenizer', '?')}")
    out.append(f"- Bounds version: `{bench.get('bounds_version', '?')}`")
    out.append(f"- Repo path: `{bench.get('root', '?')}`")
    out.append("")

    if bench.get("subsystems", 0) == 0:
        out.append("> No subsystems discovered (unsupported language or empty repo). "
                   "Coverage/economics are not meaningful here; see notes/errors below.")
        out.append("")
        for note in bench.get("notes", []):
            out.append(f"- note: {note}")
        for err in bench.get("errors", []):
            out.append(f"- error: {err}")
        return "\n".join(out)

    # --- Mapping coverage (authoritative; from `bounds validate`) ---
    out.append("## Mapping coverage")
    out.append("")
    out.append("Authoritative `bounds validate` metric. 3-way per source file — mapped / "
               "unowned-supported / unsupported-language (no partial tier; partial extraction is a "
               "separate signal). Tests are excluded from the denominator and tracked separately.")
    out.append("")
    out.extend(coverage_summary_lines(bench.get("coverage", {})))
    out.append("")

    # --- Token economics ---
    out.append("## Token economics")
    out.append("")
    out.append("| Metric | Value |")
    out.append("|--------|------:|")
    out.append(f"| Subsystems | {_fmt_int(bench.get('subsystems'))} |")
    out.append(f"| `bounds list` tokens (whole-map orientation) | {_fmt_int(bench.get('list_tok'))} |")
    out.append(f"| All-subsystem source tokens | {_fmt_int(bench.get('all_source_tok'))} |")
    out.append(f"| Map reduction | {_fmt_pct(bench.get('map_reduction_pct'))} |")
    key = bench.get("key_subsystem", "?")
    out.append(f"| Key subsystem | `{key}` |")
    out.append(f"| `bounds describe {key}` tokens | {_fmt_int(bench.get('describe_tok'))} |")
    out.append(f"| `bounds impact {key}` tokens | {_fmt_int(bench.get('impact_tok'))} |")
    out.append(f"| `{key}` source tokens | {_fmt_int(bench.get('sub_source_tok'))} |")
    out.append(f"| Surface reduction (`describe` vs source) | {_fmt_pct(bench.get('surface_reduction_pct'))} |")
    out.append("")
    out.append("Per-subsystem `describe` distribution (the honest spread — it scales with exposed "
               "API, not files):")
    out.append("")
    out.append("| sampled | min | median | max |")
    out.append("|--------:|----:|-------:|----:|")
    out.append(
        f"| {_fmt_int(bench.get('describe_sampled'))} "
        f"| {_fmt_int(bench.get('describe_min_tok'))} "
        f"| {_fmt_int(bench.get('describe_median_tok'))} "
        f"| {_fmt_int(bench.get('describe_max_tok'))} |"
    )
    out.append("")
    out.append("> Whole-map `bounds list` reduction is the cheap-orientation bound; the repeatable, "
               "honest win is targeted retrieval (`describe`/`impact`). Cite the range, not a flat %.")
    out.append("")

    # --- Correctness / health (from oss_bench + the convergence test in oss_features) ---
    out.append("## Correctness & health")
    out.append("")
    out.append("| Signal | Value |")
    out.append("|--------|------:|")
    out.append(f"| Pre-setup (no `.bounds/`): `bounds list` rc | {bench.get('presetup_list_rc', '?')} "
               f"({bench.get('presetup_error_code', 'n/a')}) |")
    out.append(f"| Fresh `discover --apply` rc | {bench.get('discover_rc', '?')} |")
    out.append(f"| `validate` issues on fresh discover | {bench.get('validate_issue_count', '?')} "
               f"(errors {bench.get('validate_error_count', '?')}, "
               f"warnings {bench.get('validate_warning_count', '?')}) |")
    out.append(f"| Clean validate on fresh discover? | {bench.get('validate_clean_on_fresh_discover', '?')} |")
    out.append(f"| Cycles | {bench.get('cycles', '?')} |")
    out.append(f"| Schema issues | {bench.get('schema_issues', '?')} |")
    crashes = feats.get("crashes", [])
    n_cmds = len(feats.get("commands", []))
    out.append(f"| Command-surface invocations | {n_cmds} |")
    out.append(f"| Leaked tracebacks (crashes) | {len(crashes)} |")
    rt = feats.get("calibrate_roundtrip", {})
    out.append(f"| `calibrate --apply` -> clean validate? | {rt.get('converged_clean', '?')} "
               f"(before {rt.get('validate_issues_before_calibrate', '?')}, "
               f"after {rt.get('validate_issues_after_calibrate', '?')}) |")
    out.append("")
    if crashes:
        out.append("**Crashes (fail-soft contract violations):**")
        out.append("")
        for c in crashes:
            out.append(f"- `bounds {c.get('cmd')}` — {c.get('err', 'traceback')}")
        out.append("")
    codes = bench.get("validate_codes") or []
    if codes:
        out.append("Validate issue codes on fresh discover: " + ", ".join(f"`{c}`" for c in codes))
        out.append("")
    for note in bench.get("notes", []):
        out.append(f"- note: {note}")
    for err in bench.get("errors", []):
        out.append(f"- error: {err}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="path to an already-cloned repo")
    ap.add_argument("--name", default=None, help="display name (default: dir name)")
    ap.add_argument("--lang", default="?", help="primary language label (informational)")
    args = ap.parse_args()

    root = Path(args.repo).resolve()
    if not root.is_dir():
        sys.stderr.write(f"error: not a directory: {root}\n")
        return 1
    name = args.name or root.name
    count, tokenizer = _make_counter()

    bench = oss_bench.measure(root, name, args.lang, count)
    bench["tokenizer"] = tokenizer
    bench["bounds_version"] = oss_bench._run(["--version"], root)[0].strip()

    # oss_features re-bootstraps init/discover (idempotent — .bounds already exists) and exercises
    # the full command surface, including the calibrate convergence test.
    feats = oss_features.run_matrix(root)

    print(render(bench, feats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
