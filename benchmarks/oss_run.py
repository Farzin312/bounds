#!/usr/bin/env python3
"""Cross-repo token-economics benchmark on real open-source projects.

`run.py` dogfoods on the Bounds repo; this runs the same measurement on external OSS repos so the
numbers aren't self-selected. For each configured repo it shallow-clones, runs `bounds init` +
`bounds discover --apply`, then measures the tokens an agent spends to (a) orient on the whole repo
and (b) learn one subsystem's API — via Bounds versus reading the equivalent source.

Deterministic + model-agnostic (same constraints as run.py): optional tiktoken, else a labeled
~1-token/4-chars estimate; no wall-clock in the table. The cloned commit SHA is recorded so a run
is reproducible. Network-guarded: a repo that won't clone is skipped with a note, never a crash.

Usage:
    python benchmarks/oss_run.py                  # default repo set
    python benchmarks/oss_run.py --repo <path>    # measure an already-checked-out repo
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
sys.path.insert(0, str(BENCH_DIR))
from run import _make_counter  # noqa: E402 - reuse the one token counter (DRY)

SOURCE_EXTS = {".py", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}

# Pinned-by-clone OSS repos. Kept small + dependency-light so a contributor can re-run quickly; the
# exact commit measured is recorded in the output. Add a (name, url, language) row to extend.
DEFAULT_REPOS = [
    ("click", "https://github.com/pallets/click", "python"),
    ("axios", "https://github.com/axios/axios", "typescript"),
]


def _bounds() -> str:
    venv = REPO_ROOT / ".venv" / "bin" / "bounds"
    return str(venv) if venv.exists() else "bounds"


def _run(args: list[str], cwd: Path) -> str:
    proc = subprocess.run([_bounds(), *args], cwd=cwd, capture_output=True, text=True)
    return proc.stdout


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _clone(url: str, dest: Path) -> str | None:
    """Shallow-clone ``url`` into ``dest``; return the cloned commit SHA, or None on failure."""
    res = _git(["clone", "--depth", "1", url, str(dest)])
    if res.returncode != 0:
        return None
    head = _git(["rev-parse", "--short", "HEAD"], cwd=dest)
    return head.stdout.strip() or "unknown"


def _manifest_paths(root: Path, name: str) -> list[Path]:
    manifest = root / ".bounds" / "manifests" / f"{name}.yaml"
    if not manifest.is_file():
        return []
    out: list[Path] = []
    in_paths = False
    for raw in manifest.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("- "):  # a sequence item — YAML allows it flush-left or indented
            if in_paths:
                cand = root / s[2:].strip().strip("'\"")
                if cand.is_dir():
                    out.extend(sorted(f for f in cand.rglob("*")
                                      if f.is_file() and f.suffix in SOURCE_EXTS))
                elif cand.is_file():
                    out.append(cand)
            continue
        in_paths = s.startswith("paths:")  # any non-sequence line is a key line
    return out


def _source_tokens(root: Path, name: str, count) -> int:
    text = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in _manifest_paths(root, name))
    return count(text)


def _pick_subsystem(subs: list[dict]) -> str | None:
    """The most-depended-on non-test subsystem (the one an agent most needs to understand)."""
    def score(s):
        name = s.get("name", "")
        penalty = -1 if ("test" in name.lower()) else 0
        return (penalty, len(s.get("consumed_by", []) or []), s.get("exposes", 0))
    real = [s for s in subs if "test" not in s.get("name", "").lower()] or subs
    return max(real, key=score).get("name") if real else None


def _measure(root: Path, count) -> dict | None:
    """Run discover, then return token economics for the whole map + the key subsystem."""
    _run(["init", "--root"], root)
    _run(["discover", "--apply"], root)
    raw = _run(["list"], root)
    try:
        listing = json.loads(raw)
    except json.JSONDecodeError:
        return None
    subs = listing.get("subsystems", [])
    subs = subs if isinstance(subs, list) else [dict(name=k, **v) for k, v in subs.items()]
    if not subs:
        return None
    name = _pick_subsystem(subs)
    all_source = sum(_source_tokens(root, s.get("name", ""), count) for s in subs)
    list_tok = count(raw)
    describe_tok = count(_run(["describe", name], root))
    sub_source = _source_tokens(root, name, count)
    return {
        "subsystems": len(subs),
        "subsystem": name,
        "list_tok": list_tok,
        "all_source_tok": all_source,
        "describe_tok": describe_tok,
        "sub_source_tok": sub_source,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", help="measure an already-checked-out repo path instead of cloning")
    args = ap.parse_args()

    count, tokenizer = _make_counter()
    rows: list[tuple[str, str, str, dict]] = []  # (name, url, sha, measurement)
    skipped: list[str] = []

    if args.repo:
        root = Path(args.repo).resolve()
        m = _measure(root, count)
        if m:
            rows.append((root.name, str(root), "local", m))
        else:
            skipped.append(f"{root} (no subsystems discovered)")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            for name, url, _lang in DEFAULT_REPOS:
                dest = Path(tmp) / name
                sha = _clone(url, dest)
                if sha is None:
                    skipped.append(f"{name} (clone failed — offline?)")
                    continue
                m = _measure(dest, count)
                if m:
                    rows.append((name, url, sha, m))
                else:
                    skipped.append(f"{name} (no subsystems discovered)")

    print("# Bounds OSS token-economics benchmark\n")
    print(f"- Tokenizer: {tokenizer}")
    print("- Measures tokens to (a) orient on the whole repo and (b) learn one subsystem's API,")
    print("  via Bounds vs reading the equivalent source.\n")
    print("| Repo | Commit | Subsystems | `bounds list` | All source | Map reduction "
          "| `describe` | Subsystem source | API reduction |")
    print("|------|--------|-----------:|-------------:|-----------:|--------------:"
          "|-----------:|-----------------:|--------------:|")
    for name, _url, sha, m in rows:
        map_red = (1 - m["list_tok"] / m["all_source_tok"]) * 100 if m["all_source_tok"] else 0.0
        api_red = (1 - m["describe_tok"] / m["sub_source_tok"]) * 100 if m["sub_source_tok"] else 0.0
        print(f"| {name} | {sha} | {m['subsystems']} | {m['list_tok']:,} | {m['all_source_tok']:,} "
              f"| {map_red:.1f}% | {m['describe_tok']:,} (`{m['subsystem']}`) "
              f"| {m['sub_source_tok']:,} | {api_red:.1f}% |")
    if skipped:
        print("\nSkipped: " + "; ".join(skipped))

    # Citations — every measured subject is attributable + reproducible (repo URL @ commit).
    if rows:
        print("\n## Test subjects (cited)\n")
        for name, url, sha, _m in rows:
            print(f"- **{name}** — {url} @ `{sha}`")
    print("\n*Map reduction* = `bounds list` vs reading every subsystem's source. "
          "*API reduction* = `bounds describe <name>` vs reading that subsystem's source. "
          "Numbers are reproducible by re-cloning each repo at the cited commit and re-running "
          "`python benchmarks/oss_run.py`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
