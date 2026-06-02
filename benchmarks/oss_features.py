#!/usr/bin/env python3
"""Full-command-surface smoke matrix for one repo: does EVERY Bounds command run on real code?

`oss_bench.py` measures token economics + the discover<->validate signal. This complements it by
exercising the whole CLI surface (not just list/describe/impact/validate) on an already-cloned repo,
so we learn which commands work, crash, or misbehave on third-party code — and in particular whether
the documented `discover -> calibrate -> validate` flow actually converges to a clean state.

For each command it records: return code, whether stdout is valid JSON (or the expected shape), and a
one-line note / first error line. Nothing here mutates the Bounds repo; all writes land in the cloned
repo's own `.bounds/` (and a throwaway CI file). One JSON object to stdout.

Usage:
    python benchmarks/oss_features.py --repo /path/to/cloned/repo
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _bounds() -> str:
    venv = REPO_ROOT / ".venv" / "bin" / "bounds"
    return str(venv) if venv.exists() else "bounds"


def _run(args: list[str], cwd: Path, timeout: int = 120) -> dict:
    t0 = time.perf_counter()
    try:
        p = subprocess.run([_bounds(), *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"cmd": " ".join(args), "rc": None, "timeout": True, "sec": timeout}
    out, err = p.stdout, p.stderr
    rec: dict = {"cmd": " ".join(args), "rc": p.returncode, "sec": round(time.perf_counter() - t0, 3)}
    # crash = a Python traceback leaked to stderr (fail-soft contract says this must never happen)
    if "Traceback (most recent call last)" in err:
        rec["crash"] = True
        rec["err"] = err.strip().splitlines()[-1][:200]
        return rec
    parsed = None
    try:
        parsed = json.loads(out) if out.strip() else None
        rec["json_valid"] = True
    except json.JSONDecodeError:
        rec["json_valid"] = False  # --human output is intentionally not JSON
    if isinstance(parsed, dict):
        if "error" in parsed:
            rec["error_code"] = (parsed["error"] or {}).get("code")
        if "issues" in parsed:
            rec["issues"] = len(parsed.get("issues") or [])
    return rec


def _first_symbol(cwd: Path, key: str) -> str | None:
    """A symbol name from `describe <key>` to feed `where`."""
    p = subprocess.run([_bounds(), "describe", key], cwd=cwd, capture_output=True, text=True)
    try:
        d = json.loads(p.stdout)
    except json.JSONDecodeError:
        return None
    for field in ("exposes", "exports"):
        items = d.get(field)
        if isinstance(items, list) and items:
            first = items[0]
            return first.get("name") if isinstance(first, dict) else first
    return None


def _list_subsystems(cwd: Path) -> list[dict]:
    p = subprocess.run([_bounds(), "list"], cwd=cwd, capture_output=True, text=True)
    try:
        d = json.loads(p.stdout)
    except json.JSONDecodeError:
        return []
    subs = d.get("subsystems", []) if isinstance(d, dict) else []
    return subs if isinstance(subs, list) else []


def _issue_count(cwd: Path, args: list[str]) -> int | None:
    p = subprocess.run([_bounds(), *args], cwd=cwd, capture_output=True, text=True)
    try:
        d = json.loads(p.stdout)
    except json.JSONDecodeError:
        return None
    return len(d.get("issues", []) or []) if isinstance(d, dict) else None


def run_matrix(root: Path) -> dict:
    res: dict = {"repo": root.name, "commands": [], "crashes": [], "notes": []}

    # Bootstrap (same as a real user following `bounds guide`).
    res["commands"].append(_run(["init", "--root"], root))
    res["commands"].append(_run(["discover", "--apply"], root))

    subs = _list_subsystems(root)
    if not subs:
        res["notes"].append("no subsystems (unsupported language) — surface-level commands only")

    def pick(*, depended=True):
        if not subs:
            return None
        real = [s for s in subs if "test" not in s.get("name", "").lower()] or subs
        return max(real, key=lambda s: len(s.get("consumed_by", []) or [])).get("name")

    key = pick()
    sym = _first_symbol(root, key) if key else None

    # The full read surface.
    matrix = [
        ["list"], ["list", "--human"],
        ["overview"], ["overview", "--human"],
        ["guide"],
        ["cache", "--inspect"],
        ["agent", "--detect"], ["agent", "--check"],
    ]
    if key:
        matrix += [
            ["describe", key], ["describe", key, "--human"], ["describe", key, "--full"],
            ["impact", key], ["impact", key, "--verify"], ["impact", key, "--include-raw-queries"],
        ]
    if sym:
        matrix += [["where", sym], ["where", sym, "--prefix"]]
    # Validation surface across modes + the blocking gate.
    matrix += [
        ["validate"], ["validate", "--quick"],
        ["validate", "--mode", "preflight"], ["validate", "--mode", "audit"],
        ["validate", "--enforce", "on"],
        ["preflight"], ["preflight", "--ci"],
        ["calibrate", "--dry-run"], ["calibrate", "--check"],
        ["ci", "--install", "--action"], ["ci", "--install", "--precommit"],
    ]
    for args in matrix:
        rec = _run(args, root)
        res["commands"].append(rec)
        if rec.get("crash"):
            res["crashes"].append(rec)

    # --- The convergence test: does discover -> calibrate -> validate reach a clean state? ---
    before = _issue_count(root, ["validate"])
    cal = _run(["calibrate", "--apply"], root)
    res["commands"].append(cal)
    after = _issue_count(root, ["validate"])
    res["calibrate_roundtrip"] = {
        "validate_issues_before_calibrate": before,
        "calibrate_apply_rc": cal.get("rc"),
        "validate_issues_after_calibrate": after,
        "converged_clean": (after == 0) if after is not None else None,
        "reduced": (before is not None and after is not None and after < before),
    }
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    args = ap.parse_args()
    root = Path(args.repo).resolve()
    if not root.is_dir():
        print(json.dumps({"error": f"not a directory: {root}"}))
        return 1
    out = run_matrix(root)
    out["bounds_version"] = subprocess.run([_bounds(), "--version"], cwd=root,
                                           capture_output=True, text=True).stdout.strip()
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
