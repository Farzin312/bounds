#!/usr/bin/env python3
"""Single-repo cross-language benchmark: token economics + correctness health signals.

`oss_run.py` measures two repos and prints a markdown table. This is the per-repo engine behind a
larger cross-language sweep: given one already-cloned repo, it runs the full Bounds pipeline
(`list` before setup -> `init` -> `discover --apply` ->
`list`/`describe`/`impact`/`overview`/`validate`) and emits ONE JSON object capturing both:

  (a) token economics — orient-on-whole-repo and learn-one-subsystem cost via Bounds vs source, plus
      the full per-subsystem `describe` distribution (min/median/max) so the spread is honest, and
  (b) correctness/health — did discover crash? does `validate` immediately report drift on the
      manifests discover just wrote (discover<->validate disagreement)? overview health, cycles,
      schema issues, supported-vs-total file coverage, and per-command latency.

Deterministic + model-agnostic (same as the other harnesses): optional tiktoken (exact cl100k_base),
else a labeled ~1-token/4-chars estimate. Fail-soft: a repo with no supported files yields a result
with `subsystems: 0` and a note, never a crash — that is itself a measured outcome (unsupported
language handling). One JSON object to stdout so a caller can capture it cleanly.

Usage:
    python benchmarks/oss_bench.py --repo /path/to/cloned/repo
    python benchmarks/oss_bench.py --repo /path/to/repo --name flask --lang python
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
sys.path.insert(0, str(BENCH_DIR))
from run import _make_counter, manifest_source_files  # noqa: E402 - reuse the one counter + parser

# Extensions Bounds has an adapter for (registry: python, typescript/js, sql, prisma). Used only to
# report supported-vs-total file coverage; never to gate extraction (the CLI owns that).
SUPPORTED_EXTS = {".py", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".sql", ".prisma"}
# Per-subsystem `describe` distribution can be expensive on huge maps; cap the sampled count.
DESCRIBE_SAMPLE_CAP = 60


def _bounds() -> str:
    venv = REPO_ROOT / ".venv" / "bin" / "bounds"
    return str(venv) if venv.exists() else "bounds"


def _run(args: list[str], cwd: Path) -> tuple[str, str, int, float]:
    """Run `bounds <args>` in cwd; return (stdout, stderr, returncode, elapsed_seconds)."""
    t0 = time.perf_counter()
    proc = subprocess.run([_bounds(), *args], cwd=cwd, capture_output=True, text=True)
    return proc.stdout, proc.stderr, proc.returncode, time.perf_counter() - t0


def _error_payload(stdout: str) -> dict:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    err = data.get("error")
    return err if isinstance(err, dict) else {}


def _source_tokens(root: Path, name: str, count) -> int:
    text = "".join(p.read_text(encoding="utf-8", errors="ignore")
                   for p in manifest_source_files(root, name))
    return count(text)


def _pick_subsystem(subs: list[dict]) -> str | None:
    """The most-depended-on non-test subsystem (the one an agent most needs to understand)."""
    def score(s):
        name = s.get("name", "")
        penalty = -1 if ("test" in name.lower()) else 0
        return (penalty, len(s.get("consumed_by", []) or []), s.get("exposes", 0))
    real = [s for s in subs if "test" not in s.get("name", "").lower()] or subs
    return max(real, key=score).get("name") if real else None


def _file_coverage(root: Path) -> dict:
    """Total source-ish files in the repo vs how many a supported adapter could handle."""
    ignore = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".bounds"}
    total = supported = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in ignore for part in p.parts):
            continue
        total += 1
        if p.suffix in SUPPORTED_EXTS:
            supported += 1
    return {"files_total": total, "files_supported_ext": supported}


def _as_int(x, default=0) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def measure(root: Path, name: str, lang: str, count) -> dict:
    result: dict = {"name": name, "lang": lang, "root": str(root), "errors": [], "notes": []}
    result.update(_file_coverage(root))

    # --- without Bounds: the command should fail loud and tell users how to recover ---
    pre_out, pre_err, pre_rc, pre_t = _run(["list"], root)
    result["without_bounds_list_rc"] = pre_rc
    result["without_bounds_list_sec"] = round(pre_t, 3)
    pre_error = _error_payload(pre_out)
    if pre_error:
        result["without_bounds_error_code"] = pre_error.get("code")
        result["without_bounds_error_message"] = pre_error.get("message")
        result["without_bounds_error_fix"] = pre_error.get("fix")
    elif pre_rc == 0:
        result["notes"].append("preflight list succeeded before init; repo already had .bounds")
    else:
        result["errors"].append(f"preflight list emitted non-JSON failure (rc={pre_rc}): {pre_err.strip()[:200]}")

    # --- init + discover (the bootstrap; capture crashes/latency) ---
    _, init_err, init_rc, init_t = _run(["init", "--root"], root)
    result["init_rc"] = init_rc
    result["init_sec"] = round(init_t, 3)
    if init_rc != 0:
        result["errors"].append(f"init exited {init_rc}: {init_err.strip()[:200]}")
    out, disc_err, disc_rc, disc_t = _run(["discover", "--apply"], root)
    result["discover_rc"] = disc_rc
    result["discover_sec"] = round(disc_t, 3)
    if disc_rc != 0:
        result["errors"].append(f"discover exited {disc_rc}: {disc_err.strip()[:200]}")

    # --- list (the whole-map orientation artifact) ---
    raw_list, list_err, list_rc, list_t = _run(["list"], root)
    try:
        listing = json.loads(raw_list)
    except json.JSONDecodeError:
        result["errors"].append(f"list emitted non-JSON (rc={list_rc}): {raw_list[:120]}")
        result["subsystems"] = 0
        return result
    if isinstance(listing, dict) and "error" in listing:
        result["errors"].append(f"list error: {json.dumps(listing['error'])[:200]}")
        result["subsystems"] = 0
        return result
    subs = listing.get("subsystems", []) if isinstance(listing, dict) else []
    subs = subs if isinstance(subs, list) else [dict(name=k, **v) for k, v in subs.items()]
    result["subsystems"] = len(subs)
    result["list_sec"] = round(list_t, 3)
    if not subs:
        result["notes"].append("no subsystems discovered (unsupported language or empty repo)")
        return result

    # --- map reduction: bounds list vs reading every subsystem's source ---
    all_source = sum(_source_tokens(root, s.get("name", ""), count) for s in subs)
    list_tok = count(raw_list)
    result["list_tok"] = list_tok
    result["all_source_tok"] = all_source
    result["map_reduction_pct"] = round((1 - list_tok / all_source) * 100, 1) if all_source else None

    # --- shape signals: is the partition sensible or one blob / all singletons? ---
    file_counts = []
    for s in subs:
        n = len(manifest_source_files(root, s.get("name", "")))
        file_counts.append(n)
    result["files_in_manifests"] = sum(file_counts)
    result["max_subsystem_files"] = max(file_counts) if file_counts else 0
    result["singleton_subsystems"] = sum(1 for n in file_counts if n <= 1)
    result["exposes_max"] = max((_as_int(s.get("exposes", 0)) for s in subs), default=0)

    # --- per-subsystem describe distribution (the honest spread) ---
    sample = subs[:DESCRIBE_SAMPLE_CAP]
    describe_toks = []
    for s in sample:
        d_out, _, d_rc, _ = _run(["describe", s.get("name", "")], root)
        if d_rc == 0 and d_out.strip():
            describe_toks.append(count(d_out))
    if describe_toks:
        result["describe_sampled"] = len(describe_toks)
        result["describe_min_tok"] = min(describe_toks)
        result["describe_median_tok"] = int(statistics.median(describe_toks))
        result["describe_max_tok"] = max(describe_toks)

    # --- key subsystem: describe + impact vs its own source (API reduction) ---
    key = _pick_subsystem(subs)
    result["key_subsystem"] = key
    d_out, _, _, d_t = _run(["describe", key], root)
    i_out, _, _, i_t = _run(["impact", key], root)
    sub_source = _source_tokens(root, key, count)
    result["describe_tok"] = count(d_out)
    result["impact_tok"] = count(i_out)
    result["sub_source_tok"] = sub_source
    result["api_reduction_pct"] = round((1 - count(d_out) / sub_source) * 100, 1) if sub_source else None
    result["describe_sec"] = round(d_t, 3)

    # --- overview health ---
    ov_out, _, _, _ = _run(["overview"], root)
    try:
        ov = json.loads(ov_out)
        health = ov.get("health", {}) if isinstance(ov, dict) else {}
        result["overview_health_ok"] = bool(health.get("ok"))
        result["cycles"] = _as_int(health.get("cycles", len(ov.get("cycles", []) or [])))
        result["schema_issues"] = len(ov.get("schema_issues", []) or [])
        validation = health.get("validation", {}) if isinstance(health, dict) else {}
        if isinstance(validation, dict):
            result["overview_validation_errors"] = _as_int(validation.get("errors"))
            result["overview_validation_warnings"] = _as_int(validation.get("warnings"))
            result["overview_structural_drift"] = _as_int(validation.get("structural_drift"))
            result["overview_boundary_violations"] = _as_int(validation.get("boundary_violations"))
            result["overview_ownership_overlaps"] = _as_int(validation.get("ownership_overlaps"))
            result["overview_mapped_pct"] = validation.get("mapped_pct")
    except json.JSONDecodeError:
        result["errors"].append("overview emitted non-JSON")

    # --- the key correctness test: does validate report drift on fresh discover output? ---
    val_out, _, val_rc, val_t = _run(["validate"], root)
    result["validate_rc"] = val_rc
    result["validate_sec"] = round(val_t, 3)
    try:
        val = json.loads(val_out)
        issues = val.get("issues", []) if isinstance(val, dict) else []
        result["validate_issue_count"] = len(issues)
        result["validate_error_count"] = sum(
            1 for i in issues if isinstance(i, dict) and i.get("severity") == "error"
        )
        result["validate_warning_count"] = sum(
            1 for i in issues if isinstance(i, dict) and i.get("severity") == "warning"
        )
        # Codes give a fingerprint of WHAT drifts immediately after discover (ideally nothing).
        codes = sorted({i.get("code", "?") for i in issues if isinstance(i, dict)})
        result["validate_codes"] = codes[:12]
        result["validate_clean_on_fresh_discover"] = (len(issues) == 0)
        result["validate_error_clean_on_fresh_discover"] = (result["validate_error_count"] == 0)
    except json.JSONDecodeError:
        result["errors"].append("validate emitted non-JSON")

    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="path to an already-cloned repo")
    ap.add_argument("--name", default=None, help="display name (default: dir name)")
    ap.add_argument("--lang", default="?", help="primary language label (informational)")
    args = ap.parse_args()

    root = Path(args.repo).resolve()
    if not root.is_dir():
        print(json.dumps({"error": f"not a directory: {root}"}))
        return 1
    name = args.name or root.name
    count, tokenizer = _make_counter()
    result = measure(root, name, args.lang, count)
    result["tokenizer"] = tokenizer
    result["bounds_version"] = _run(["--version"], root)[0].strip()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
