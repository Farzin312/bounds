export const meta = {
  name: 'bounds-cross-language-sweep',
  description: 'Clone 16 diverse OSS repos, run the Bounds pipeline on each, measure token economics + find bugs',
  phases: [
    { title: 'Benchmark', detail: 'one agent per repo: clone, run oss_bench, judge quality + bugs' },
    { title: 'Synthesize', detail: 'dedup bugs, build corpus summary' },
  ],
}

// Diverse corpus: supported langs (Python, TS/JS) across sizes/styles, plus an app repo with
// Prisma+SQL, plus 3 unsupported-language repos to test fail-soft (must NOT crash).
const REPOS = [
  // Python (supported)
  { repo: 'click',     url: 'https://github.com/pallets/click',     lang: 'python', size: 'small',  kind: 'CLI library' },
  { repo: 'flask',     url: 'https://github.com/pallets/flask',     lang: 'python', size: 'medium', kind: 'web framework' },
  { repo: 'requests',  url: 'https://github.com/psf/requests',      lang: 'python', size: 'small',  kind: 'HTTP library' },
  { repo: 'fastapi',   url: 'https://github.com/tiangolo/fastapi',  lang: 'python', size: 'medium', kind: 'web framework' },
  { repo: 'httpie',    url: 'https://github.com/httpie/cli',         lang: 'python', size: 'medium', kind: 'CLI app' },
  // TS / JS (supported)
  { repo: 'axios',     url: 'https://github.com/axios/axios',       lang: 'typescript', size: 'small',  kind: 'HTTP library' },
  { repo: 'express',   url: 'https://github.com/expressjs/express', lang: 'javascript', size: 'small',  kind: 'web framework' },
  { repo: 'lodash',    url: 'https://github.com/lodash/lodash',     lang: 'javascript', size: 'medium', kind: 'utility library (many files)' },
  { repo: 'date-fns',  url: 'https://github.com/date-fns/date-fns', lang: 'typescript', size: 'medium', kind: 'many small modules' },
  { repo: 'zod',       url: 'https://github.com/colinhacks/zod',    lang: 'typescript', size: 'medium', kind: 'one large module (mega-export stress test)' },
  { repo: 'nest',      url: 'https://github.com/nestjs/nest',       lang: 'typescript', size: 'large',  kind: 'modular framework' },
  { repo: 'chalk',     url: 'https://github.com/chalk/chalk',       lang: 'javascript', size: 'tiny',   kind: 'tiny library (overhead test)' },
  // App repo: exercises Prisma + SQL + TS together (real-world monorepo)
  { repo: 'documenso', url: 'https://github.com/documenso/documenso', lang: 'typescript+prisma+sql', size: 'large', kind: 'real app monorepo (Prisma/SQL)' },
  // Unsupported languages: MUST fail soft (0 subsystems, a note), never crash
  { repo: 'cobra',     url: 'https://github.com/spf13/cobra',       lang: 'go',   size: 'medium', kind: 'UNSUPPORTED-lang fail-soft test' },
  { repo: 'ripgrep',   url: 'https://github.com/BurntSushi/ripgrep', lang: 'rust', size: 'medium', kind: 'UNSUPPORTED-lang fail-soft test' },
  { repo: 'gson',      url: 'https://github.com/google/gson',       lang: 'java', size: 'medium', kind: 'UNSUPPORTED-lang fail-soft test' },
]

const RESULT_SCHEMA = {
  type: 'object',
  required: ['repo', 'lang', 'status'],
  additionalProperties: true,
  properties: {
    repo: { type: 'string' },
    url: { type: 'string' },
    lang: { type: 'string' },
    sha: { type: 'string' },
    size_class: { type: 'string' },
    status: { type: 'string', description: 'measured | no-subsystems | clone-failed | crashed | error' },
    crashed: { type: 'boolean' },
    bench: { type: 'object', additionalProperties: true, description: 'the full JSON object emitted by oss_bench.py' },
    subsystems: { type: ['integer', 'null'] },
    map_reduction_pct: { type: ['number', 'null'] },
    api_reduction_pct: { type: ['number', 'null'] },
    list_tok: { type: ['integer', 'null'] },
    all_source_tok: { type: ['integer', 'null'] },
    key_subsystem: { type: ['string', 'null'] },
    describe_tok: { type: ['integer', 'null'] },
    describe_median_tok: { type: ['integer', 'null'] },
    describe_max_tok: { type: ['integer', 'null'] },
    validate_issue_count: { type: ['integer', 'null'] },
    validate_clean_on_fresh_discover: { type: ['boolean', 'null'] },
    discover_sec: { type: ['number', 'null'] },
    partition_quality: { type: 'string', description: 'good | coarse | fragmented | single-blob | n/a' },
    extraction_spotcheck: { type: 'string', description: 'result of confirming 2-3 described symbols actually exist/are exported in source' },
    validate_interpretation: { type: 'string', description: 'why the validate issues appear: expected third-party-import noise vs a real discover<->extract inconsistency' },
    bugs: {
      type: 'array',
      items: {
        type: 'object',
        required: ['severity', 'what'],
        additionalProperties: true,
        properties: {
          severity: { type: 'string', description: 'high | medium | low' },
          what: { type: 'string' },
          evidence: { type: 'string' },
        },
      },
    },
    friction: { type: 'array', items: { type: 'string' } },
    would_help_agent: { type: 'boolean' },
    helpful_why: { type: 'string' },
    notes: { type: 'string' },
  },
}

function promptFor(r) {
  return `You are benchmarking the Bounds CLI against a real open-source repo to find out whether it actually works on code it was NOT tuned for, and to measure its token economics honestly. Be skeptical and precise — your job includes finding bugs, not just collecting numbers.

REPO: ${r.repo}  (${r.kind})
URL: ${r.url}
Primary language: ${r.lang}   Size hint: ${r.size}

The Bounds CLI lives at /Users/farzin/bounds. Use its venv: \`/Users/farzin/bounds/.venv/bin/bounds\` and \`/Users/farzin/bounds/.venv/bin/python\`. Bounds supports ONLY: Python, TypeScript/JavaScript, SQL, Prisma. For an unsupported language (Go/Rust/Java) the CORRECT behavior is to discover 0 subsystems with a note and exit cleanly — a crash/traceback is a BUG.

STEPS:
1. Clone shallow into a temp dir:
   TMP=$(mktemp -d); git clone --depth 1 ${r.url} "$TMP/${r.repo}"
   Record the SHA: (cd "$TMP/${r.repo}" && git rev-parse --short HEAD). If clone fails (offline/404), return status "clone-failed" and stop.
2. Run the deterministic measurement engine (this is your SOURCE OF NUMBERS — do not invent numbers):
   /Users/farzin/bounds/.venv/bin/python /Users/farzin/bounds/benchmarks/oss_bench.py --repo "$TMP/${r.repo}" --name ${r.repo} --lang ${r.lang}
   It prints ONE JSON object. If it prints a Python traceback instead, that is a HIGH-severity BUG: set crashed=true, status="crashed", paste the traceback into bugs[].evidence. Capture the full JSON into the \`bench\` field and copy its key numbers into the matching top-level fields (subsystems, map_reduction_pct, api_reduction_pct, list_tok, all_source_tok, key_subsystem, describe_tok, describe_median_tok, describe_max_tok, validate_issue_count, validate_clean_on_fresh_discover, discover_sec).
3. QUALITATIVE JUDGMENT (the part that finds bugs — only if subsystems > 0):
   a. Run \`cd "$TMP/${r.repo}" && /Users/farzin/bounds/.venv/bin/bounds list -H\` and eyeball the partition. Set partition_quality: "good" (sensible modules), "coarse" (one giant subsystem swallowing most files → set a bug if it's basically single-blob), "fragmented" (dozens of 1-file subsystems), "single-blob", or "n/a".
   b. EXTRACTION SPOT-CHECK (correctness): from \`bounds describe <key_subsystem>\`, take 2-3 symbols it lists under exposed/exports, and grep the repo source to confirm they actually exist and are genuinely exported (e.g. \`def NAME\`/\`class NAME\` at module top level for Python, \`export\` for TS/JS). Report in extraction_spotcheck whether they checked out. A symbol Bounds claims is exported that isn't really there = a correctness BUG.
   c. VALIDATE INTERPRETATION: run \`bounds validate\` and read ~5 issue messages. Interpret WHY they appear. Distinguish: (i) EXPECTED noise — e.g. E_UNRESOLVED_REFERENCE for imports of third-party/stdlib packages the manifests don't model, or boundary violations that just reflect real coupling; vs (ii) a REAL inconsistency — e.g. E_STRUCTURAL_DRIFT where discover wrote a manifest whose declared exports don't match what extract sees on the SAME unchanged source (that means \`discover\` and \`validate\` disagree on identical input — a genuine bug; flag high-severity). Write your interpretation in validate_interpretation.
   d. Open one generated manifest at \`"$TMP/${r.repo}/.bounds/manifests/"*.yaml\` and note if it looks sane or malformed.
4. VERDICT: would_help_agent = would an AI coding agent genuinely spend fewer tokens / get a more reliable answer using Bounds on THIS repo vs reading source? helpful_why = one honest sentence (include the downside if there is one, e.g. "helps for orientation but the 200 validate issues would confuse a fresh user").
5. Clean up: rm -rf "$TMP".

Record EVERY crash, traceback, non-JSON output, malformed manifest, wrong extraction, or confusing UX in bugs[] (severity high/medium/low with evidence) or friction[]. Set size_class from what you saw (tiny/small/medium/large). Return the structured object. Numbers must come from oss_bench.py output verbatim — never estimate.`
}

phase('Benchmark')
const results = await parallel(
  REPOS.map((r) => () =>
    agent(promptFor(r), {
      label: `bench:${r.repo}`,
      phase: 'Benchmark',
      schema: RESULT_SCHEMA,
      agentType: 'general-purpose',
    }).then((res) => res || { repo: r.repo, lang: r.lang, status: 'error', notes: 'agent returned null (skipped or failed)' })
  )
)

phase('Synthesize')
const valid = results.filter(Boolean)
const allBugs = valid.flatMap((r) =>
  (r.bugs || []).map((b) => ({ repo: r.repo, lang: r.lang, ...b }))
)
const summary = await agent(
  `You are consolidating a cross-language benchmark of the Bounds CLI run over ${valid.length} OSS repos.
Here are the raw per-repo results as JSON:

${JSON.stringify(valid, null, 2)}

Produce a tight synthesis as a JSON object with these fields:
- "headline": 2-3 sentence honest summary of whether Bounds works beyond its own repo and where it struggles.
- "supported_lang_repos_measured": integer count of repos with subsystems>0.
- "failsoft_ok": boolean — did ALL unsupported-language repos (go/rust/java) discover 0 subsystems WITHOUT crashing? List any that crashed.
- "token_economics": object with map_reduction range (min/median/max across supported repos), api_reduction range, and a one-line honest interpretation (note that map-reduction compares against reading ALL source, which is a generous baseline).
- "describe_spread": honest statement of how per-subsystem describe token cost varies (cite min/median/max examples) — this is the "it depends on exposed API size" reality.
- "validate_on_fresh_discover": how many supported repos had a CLEAN validate right after discover vs how many showed issues; and the consolidated interpretation of whether those issues are expected third-party noise or a real discover<->validate inconsistency bug.
- "confirmed_bugs": deduplicated array of distinct bugs (merge the same bug seen across repos), each {severity, title, affected_repos:[...], detail}. Only include things that are genuinely wrong/broken/misleading, not expected behavior.
- "ux_friction": deduplicated array of friction strings that would trip up a new user.
- "marketing_guidance": 3-5 bullet strings on how the README/docs numbers should be framed to stay HONEST given this corpus (e.g. what to qualify, what range to cite, what NOT to claim).
Return ONLY the JSON object.`,
  {
    label: 'synthesize',
    phase: 'Synthesize',
    schema: {
      type: 'object',
      additionalProperties: true,
      required: ['headline', 'confirmed_bugs'],
      properties: {
        headline: { type: 'string' },
        supported_lang_repos_measured: { type: 'integer' },
        failsoft_ok: { type: 'boolean' },
        token_economics: { type: 'object', additionalProperties: true },
        describe_spread: { type: 'string' },
        validate_on_fresh_discover: { type: 'string' },
        confirmed_bugs: { type: 'array', items: { type: 'object', additionalProperties: true } },
        ux_friction: { type: 'array', items: { type: 'string' } },
        marketing_guidance: { type: 'array', items: { type: 'string' } },
      },
    },
  }
)

return { results: valid, allBugs, summary }
