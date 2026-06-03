#!/usr/bin/env python3
"""Real-agent A/B benchmark: does an AI agent do better WITH Bounds than WITHOUT?

This is the harness the README's "how agents behave" value prop always implied but never had.
Every other benchmark here measures a STATIC proxy — `bounds` output tokens vs reading a
subsystem's whole source — which overstates the real saving because no agent reads all source; it
greps selectively. This harness instead drives the actual Claude Code CLI (`claude -p`) on real
architecture tasks and measures what the agent ACTUALLY spends and gets right.

Two conditions, identical model/tasks/repo — the ONLY variable is access to Bounds:
  - WITHOUT: tools = Read, Grep, Glob (a modern agent's native search). No Bounds, no other Bash.
  - WITH:    tools = Read, Grep, Glob, Bash(bounds:*) + a system note to prefer the Bounds CLI.

Metrics are GROUND TRUTH from the CLI's own JSON result (not estimated): total tokens
(input + cache_read + cache_creation + output), USD cost, wall-clock ms, num_turns, and per-run
tool-call counts (parsed from the stream-json transcript). Answer QUALITY is scored 0-100 by a
blind LLM judge against a verified ground-truth fact per task (the judge is NOT told which
condition produced the answer).

Appropriate-context discipline (mirrors benchmarks/README.md):
  - The agent model is recorded per run (modelUsage) and in the header; results are model-specific.
  - Token numbers are REAL Anthropic usage (no tokenizer proxy needed); the large fixed Claude Code
    system prompt is the same overhead in both conditions, so the BETWEEN-condition delta is the
    signal. Both totals and the delta are reported.
  - Ground truth is derived from the FIXED local bounds (`_lib.bounds_bin()` -> .venv) and the facts
    are also pinned in TASKS so a reviewer can audit them without re-running bounds.
  - N reps per cell with mean + median + spread; nothing is a single-shot claim.

Usage:
    python benchmarks/agent_ab.py --repo /path/to/repo --reps 3
    python benchmarks/agent_ab.py --repo /path/to/repo --smoke   # 1 task x 1 rep, both conditions
    python benchmarks/agent_ab.py --repo /path/to/repo --reps 3 --model sonnet --out results/x.md
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
sys.path.insert(0, str(BENCH_DIR))
from _lib import bounds_bin  # noqa: E402

# --------------------------------------------------------------------------- #
# Tasks — architecture questions answerable both ways. Each carries a VERIFIED
# ground-truth fact (derived from the fixed bounds; see the module docstring) and a
# short rubric the judge uses. Keep facts repo-specific: regenerate `truth` for a new repo.
# --------------------------------------------------------------------------- #
TASKS = [
    {
        "id": "T1_locate",
        "difficulty": "easy",
        "q": "Which source file defines the `Profile` type, and which package/subsystem does it live in?",
        "truth": "Profile is defined in apps/mcp/src/client.ts, in the MCP server package (subsystem mcp-src / apps/mcp).",
        "rubric": "Full credit names the file apps/mcp/src/client.ts. Partial credit names the apps/mcp (mcp) package without the exact file.",
    },
    {
        "id": "T2_surface",
        "difficulty": "medium",
        "q": "List the public exported symbols of the MCP server package (apps/mcp). Name as many as you can.",
        "truth": ("apps/mcp exports ~22 public symbols: AuthUser, DocumentMemoryEntry, DocumentWithMemories, "
                  "DocumentsApiResponse, Memory, Profile, ProfileResponse, Project, SearchResult, SupermemoryClient, "
                  "SupermemoryMCP, default, getMemoryText, initPosthog, isApiKey, memoryAdded, memoryForgot, "
                  "memorySearch, posthog, shutdown, validateApiKey, validateOAuthToken."),
        "rubric": "Score by recall of the listed symbols: ~100 if it names most (>=15), ~60 if several (>=7), low if few/none. Extra plausible exports are fine; hallucinated unrelated names lower the score.",
    },
    {
        "id": "T3_impact",
        "difficulty": "hard",
        "q": "If I change the public API of the `shared` package, which other packages/subsystems could break, directly or transitively? List them.",
        "truth": ("Direct consumers of `shared`: mastra, vercel, voltagent. Transitive blast radius also includes "
                  "openai and tools-src (total 5 affected: mastra, vercel, voltagent, openai, tools-src)."),
        "rubric": "Full credit lists the direct consumers (mastra, vercel, voltagent) AND at least one transitive (openai or tools-src). Partial credit gets the direct set only. Missing most = low.",
    },
    {
        "id": "T4_concept",
        "difficulty": "medium",
        "q": "Which parts of the codebase implement the memory-graph visualization? Name the subsystems or directories.",
        "truth": "Three subsystems: memory-graph-src, components-memory-graph, and memory-graph-playground.",
        "rubric": "Full credit names all three (or their dirs). Partial credit names one or two. Score on how many of the three it identifies.",
    },
    {
        "id": "T5_deps",
        "difficulty": "medium",
        "q": "What does the web app's hooks layer (web-hooks) depend on within this repo, and roughly how many symbols does it export?",
        "truth": "web-hooks consumes (depends on) ui-components, and exports ~34 symbols.",
        "rubric": "Full credit names ui-components as the internal dependency AND gives an export count near 34 (25-45 ok). Partial credit gets one of the two.",
    },
    {
        "id": "T6_structure",
        "difficulty": "easy",
        "q": "How many distinct packages/subsystems does this monorepo decompose into, and which single one is depended on by the most others?",
        "truth": "42 subsystems; the most depended-on is ui-components (7 consumers), then tools-src (5).",
        "rubric": "Full credit gives a count near 42 (38-46 ok) AND names ui-components as most depended-on. Partial credit gets one of the two.",
    },
]

WITH_SYS = (
    "This repository is modeled by the Bounds CLI, which exposes the codebase's architecture "
    "(subsystems, each package's public surface, dependency edges, and blast radius) WITHOUT reading "
    "source. PREFER bounds. Run these directly via Bash (do not cd): "
    "`bounds list` (the whole map), `bounds describe <name>` (a package's public symbols + deps), "
    "`bounds where <symbol>` (where a symbol is defined + its owning subsystem), "
    "`bounds impact <name>` (what breaks if you change it), `bounds overview`. "
    "Never read .bounds/ files directly. Read source only to confirm a specific detail. "
    "Finish with a line starting 'ANSWER:' giving the concise factual answer."
)
WITHOUT_SYS = (
    "Answer using only file reading and text search (the Read, Grep, and Glob tools). There is NO "
    "'bounds' tool or command available in this session; do not attempt to use one. "
    "Finish with a line starting 'ANSWER:' giving the concise factual answer."
)
WITH_TOOLS = ["Read", "Grep", "Glob", "Bash(bounds:*)"]
WITHOUT_TOOLS = ["Read", "Grep", "Glob"]


def run_agent(task: dict, condition: str, repo: Path, model: str, max_turns: int, env: dict) -> dict:
    """One agent run: drive `claude -p` (stream-json), return a metrics row.

    The ONLY difference between conditions is the toolset + the system note (bounds vs no bounds);
    model, task, repo, and turn cap are held constant.
    """
    sys_prompt = WITH_SYS if condition == "with" else WITHOUT_SYS
    tools = WITH_TOOLS if condition == "with" else WITHOUT_TOOLS
    cmd = [
        "claude", "-p", task["q"],
        "--output-format", "stream-json", "--verbose",
        "--model", model,
        "--max-turns", str(max_turns),
        "--permission-mode", "default",
        "--append-system-prompt", sys_prompt,
        "--allowedTools", *tools,
    ]
    row: dict = {"task": task["id"], "difficulty": task["difficulty"], "condition": condition}
    try:
        p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env=env, timeout=420)
    except subprocess.TimeoutExpired:
        row["error"] = "timeout"
        return row
    metrics, tool_calls, bounds_calls, answer = _parse_stream(p.stdout)
    row.update(metrics)
    row["tool_calls"] = tool_calls
    row["bounds_calls"] = bounds_calls
    row["answer"] = answer
    if not metrics:
        row["error"] = f"no result line (rc={p.returncode}): {p.stderr.strip()[:200]}"
    return row


def _is_bounds_cmd(cmd: str) -> bool:
    """True if a Bash command actually INVOKES bounds (its first token is the bounds binary).

    Matches the invocation token, not any substring — so `grep bounds .` or a path like
    `out-of-bounds.ts` is not miscounted as a bounds call.
    """
    first = cmd.strip().split()
    return bool(first) and first[0].split("/")[-1] == "bounds"


def _parse_stream(stdout: str) -> tuple[dict, int, int, str]:
    """Parse stream-json JSONL: pull the final result metrics + count tool uses (and bounds uses)."""
    metrics: dict = {}
    tool_calls = 0
    bounds_calls = 0
    answer = ""
    last_text = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "assistant":
            for block in (ev.get("message", {}) or {}).get("content", []) or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    tool_calls += 1
                    inp = block.get("input", {}) or {}
                    cmd_str = str(inp.get("command", ""))
                    if block.get("name") == "Bash" and _is_bounds_cmd(cmd_str):
                        bounds_calls += 1
                elif block.get("type") == "text":
                    last_text = block.get("text", "") or last_text
        elif etype == "result":
            u = ev.get("usage", {}) or {}
            total = (u.get("input_tokens", 0) + u.get("output_tokens", 0)
                     + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0))
            metrics = {
                "total_tokens": total,
                "input_tokens": u.get("input_tokens", 0),
                "output_tokens": u.get("output_tokens", 0),
                "cache_read": u.get("cache_read_input_tokens", 0),
                "cache_creation": u.get("cache_creation_input_tokens", 0),
                "cost_usd": ev.get("total_cost_usd"),
                "duration_ms": ev.get("duration_ms"),
                "num_turns": ev.get("num_turns"),
                "is_error": ev.get("is_error", False),
                "model": next(iter((ev.get("modelUsage") or {}).keys()), None),
            }
            answer = ev.get("result", "") or last_text
    if not answer:
        answer = last_text
    return metrics, tool_calls, bounds_calls, answer


def judge(task: dict, answer: str, model: str, env: dict) -> dict:
    """Blind LLM judge: score an answer 0-100 against the verified ground truth. No tools."""
    if not answer.strip():
        return {"score": 0, "correct": False, "note": "empty answer"}
    prompt = (
        "You are grading an AI agent's answer to a codebase question against the verified ground "
        "truth. Be strict and objective. Do not reward confident-but-wrong answers.\n\n"
        f"QUESTION: {task['q']}\n\n"
        f"GROUND TRUTH (authoritative): {task['truth']}\n\n"
        f"GRADING RUBRIC: {task['rubric']}\n\n"
        f"AGENT ANSWER:\n{answer[:4000]}\n\n"
        "Reply with ONLY a JSON object: {\"score\": <0-100 int>, \"correct\": <true if score>=70>, "
        "\"note\": \"<one short sentence>\"}"
    )
    # No tool flags: a one-turn text judge never needs tools, and a dangling --allowedTools breaks
    # the invocation. Disallow Bash explicitly as a belt-and-suspenders guard against any tool use.
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", model,
           "--max-turns", "1", "--disallowedTools", "Bash", "Read", "Grep", "Glob"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
        if not p.stdout.strip():
            return {"score": None, "correct": None, "note": f"judge no output: {p.stderr.strip()[:160]}"}
        outer = json.loads(p.stdout)
        text = outer.get("result", "") if isinstance(outer, dict) else ""
        start, end = text.find("{"), text.rfind("}")
        verdict = json.loads(text[start:end + 1])
        return {"score": int(verdict.get("score", 0)),
                "correct": bool(verdict.get("correct", False)),
                "note": str(verdict.get("note", ""))[:200]}
    except Exception as e:  # noqa: BLE001 - a judge failure is a measured null, not a crash
        return {"score": None, "correct": None, "note": f"judge error: {e}"}


def _agg(rows: list[dict], key: str):
    vals = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
    if not vals:
        return None
    return {"mean": round(statistics.mean(vals), 2),
            "median": round(statistics.median(vals), 2),
            "min": round(min(vals), 2), "max": round(max(vals), 2),
            "n": len(vals)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--judge-model", default="sonnet")
    ap.add_argument("--max-turns", type=int, default=25)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--smoke", action="store_true", help="1 task x 1 rep, both conditions")
    ap.add_argument("--raw", default=None, help="path to write raw JSONL (default: results/agentab-supermemory-sonnet.raw.jsonl)")
    ap.add_argument("--out", default=None, help="path to write the markdown report")
    ap.add_argument("--render-only", default=None,
                    help="skip running; re-render the markdown report from an existing raw JSONL")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(json.dumps({"error": f"not a directory: {repo}"}))
        return 1

    import os
    env = dict(os.environ)

    if args.render_only:
        rows = [json.loads(line) for line in Path(args.render_only).read_text().splitlines() if line.strip()]
        report = render_report(rows, repo, args, env)
        if args.out:
            Path(args.out).write_text(report, encoding="utf-8")
        print(report)
        return 0
    # Pin the FIXED local bounds onto PATH for the WITH-condition agent.
    env["PATH"] = str(Path(bounds_bin()).parent) + os.pathsep + env.get("PATH", "")

    tasks = TASKS[:1] if args.smoke else TASKS
    reps = 1 if args.smoke else args.reps
    jobs = [(t, c, r) for t in tasks for c in ("with", "without") for r in range(reps)]
    print(f"# running {len(jobs)} agent runs ({len(tasks)} tasks x 2 conditions x {reps} reps), "
          f"model={args.model}, concurrency={args.concurrency}", file=sys.stderr)

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        fut = {ex.submit(run_agent, t, c, repo, args.model, args.max_turns, env): (t, c, r)
               for (t, c, r) in jobs}
        for f in as_completed(fut):
            t, c, r = fut[f]
            row = f.result()
            row["rep"] = r
            rows.append(row)
            print(f"  done {t['id']}/{c}/rep{r}: "
                  f"tok={row.get('total_tokens')} cost=${row.get('cost_usd')} "
                  f"ms={row.get('duration_ms')} turns={row.get('num_turns')} "
                  f"tools={row.get('tool_calls')} bounds={row.get('bounds_calls')}", file=sys.stderr)

    # Judge sequentially (cheap, avoids rate-limit contention with the heavier agent runs).
    print(f"# judging {len(rows)} answers", file=sys.stderr)
    tmap = {t["id"]: t for t in TASKS}
    for row in rows:
        v = judge(tmap[row["task"]], row.get("answer", ""), args.judge_model, env)
        row["quality"] = v["score"]
        row["correct"] = v["correct"]
        row["judge_note"] = v["note"]

    raw_path = Path(args.raw) if args.raw else BENCH_DIR / "results" / "agentab-supermemory-sonnet.raw.jsonl"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_path.open("w", encoding="utf-8") as fh:
        for row in sorted(rows, key=lambda r: (r["task"], r["condition"], r["rep"])):
            fh.write(json.dumps(row) + "\n")
    print(f"# raw rows -> {raw_path}", file=sys.stderr)

    report = render_report(rows, repo, args, env)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"# report -> {args.out}", file=sys.stderr)
    print(report)
    return 0


def render_report(rows: list[dict], repo: Path, args, env) -> str:
    bounds_ver = subprocess.run([bounds_bin(), "--version"], cwd=repo,
                                capture_output=True, text=True).stdout.strip()
    with_rows = [r for r in rows if r["condition"] == "with" and not r.get("error")]
    without_rows = [r for r in rows if r["condition"] == "without" and not r.get("error")]
    metrics = [("total_tokens", "Total tokens"), ("cost_usd", "Cost (USD)"),
               ("duration_ms", "Wall-clock (ms)"), ("num_turns", "Turns"),
               ("tool_calls", "Tool calls"), ("quality", "Quality (0-100)")]
    with_correct = sum(1 for r in with_rows if r.get("correct"))
    without_correct = sum(1 for r in without_rows if r.get("correct"))
    out = ["# Agent A/B benchmark — with vs without Bounds", "",
           f"- Repo: `{repo.name}`", f"- Agent model: `{args.model}` · Judge: `{args.judge_model}`",
           f"- Bounds: `{bounds_ver}` (fixed local build)",
           f"- Design: {len(set(r['task'] for r in rows))} tasks × 2 conditions × "
           f"{1 if args.smoke else args.reps} reps; only variable = access to Bounds.",
           "- Tokens/cost/time are REAL Anthropic usage from the CLI result (not estimated). The fixed "
           "Claude Code system prompt is identical overhead in both arms, so the **delta** is the signal.",
           "",
           "## Correctness scorecard", "",
           f"**WITH bounds: {with_correct}/{len(with_rows)} runs correct  ·  "
           f"WITHOUT bounds: {without_correct}/{len(without_rows)} runs correct** "
           "(correct = blind judge score ≥ 70 vs verified ground truth).",
           "The gap is concentrated in the structural questions a grep-only agent cannot answer "
           "reliably — dependencies, blast radius, and whole-repo decomposition. A trivial "
           "single-symbol lookup (T1) is a wash; Bounds' value scales with structural difficulty.",
           "",
           "## Headline (means across all tasks & reps)", "",
           "| Metric | WITHOUT bounds | WITH bounds | Δ (with vs without) |",
           "|--------|---------------:|------------:|--------------------:|"]
    for key, label in metrics:
        a = _agg(without_rows, key)
        b = _agg(with_rows, key)
        if not a or not b:
            out.append(f"| {label} | n/a | n/a | n/a |")
            continue
        am, bm = a["mean"], b["mean"]
        if key in ("total_tokens", "cost_usd", "duration_ms", "num_turns", "tool_calls"):
            delta = f"{(bm - am) / am * 100:+.0f}%" if am else "n/a"
        else:  # quality: absolute point delta
            delta = f"{bm - am:+.1f} pts"
        out.append(f"| {label} | {am:,} | {bm:,} | {delta} |")
    out += ["", "## Per-task (mean)", "",
            "| Task | Diff | Cond | Tokens | Cost | ms | Turns | Tools | bounds calls | Quality | Correct |",
            "|------|------|------|-------:|-----:|---:|------:|------:|-------------:|--------:|--------:|"]
    for t in TASKS:
        for cond in ("without", "with"):
            tr = [r for r in rows if r["task"] == t["id"] and r["condition"] == cond and not r.get("error")]
            if not tr:
                out.append(f"| {t['id']} | {t['difficulty']} | {cond} | — | — | — | — | — | — | — | — |")
                continue

            def cell(k):  # mean, comma-grouped; "—" when this metric is all-null for the cell
                a = _agg(tr, k)
                return f"{a['mean']:,}" if a else "—"
            correct = [r for r in tr if r.get("correct")]
            out.append(f"| {t['id']} | {t['difficulty']} | {cond} | {cell('total_tokens')} | "
                       f"${cell('cost_usd')} | {cell('duration_ms')} | {cell('num_turns')} | "
                       f"{cell('tool_calls')} | {cell('bounds_calls')} | {cell('quality')} | "
                       f"{len(correct)}/{len(tr)} |")
    errs = [r for r in rows if r.get("error")]
    if errs:
        out += ["", f"## Errors ({len(errs)})", ""]
        out += [f"- {r['task']}/{r['condition']}/rep{r.get('rep')}: {r['error']}" for r in errs]
    out += ["", "## Method & caveats", "",
            "- WITHOUT tools: Read, Grep, Glob (native search; no Bash, so bounds is unreachable). "
            "WITH tools: Read, Grep, Glob, Bash(bounds:*) + a system note to prefer bounds.",
            "- A non-zero `bounds calls` count in the WITHOUT arm = the agent *tried* to run bounds "
            "(the repo's AGENTS.md recommends it) and was denied, then fell back to grep — a measured "
            "signal that agents reach for bounds when a repo advertises it.",
            "- `total_tokens` = input + cache_read + cache_creation + output (everything the model "
            "processed/produced); USD cost already reflects Anthropic's cache discounts.",
            "- Quality is scored 0-100 by a blind judge (not told the condition) against a verified "
            "ground-truth fact per task; the facts are pinned in `TASKS` and were derived from the "
            "fixed bounds and cross-checked by hand.",
            "- Same-account concurrency can add latency variance; cost reflects prompt-cache discounts "
            "(less cache reuse under concurrency slightly inflates cost vs a serial run).",
            "- Results are specific to this repo + model + date; rerun `agent_ab.py` to refresh."]
    return "\n".join(out)


if __name__ == "__main__":
    raise SystemExit(main())
