# CLI reference

*Every command, its flags, the CI gate generator, and custom roles/criticality.*

[← Docs index](./README.md) · [Bounds README](../README.md)

---

## Output & error contract

Every command prints **one JSON object to stdout by default** and accepts `--human` for readable
terminal output — `--human` re-renders the *same* data and never exposes anything the JSON omits.
Fatal errors print `{"error": {"code", "message", "fix"}}` and exit `2`; blocking failures exit `1`.
Error codes are a stable public contract — see [../ARCHITECTURE.md](../ARCHITECTURE.md) for the full
list and severity/exit-code mapping.

---

## Commands

| Command | What it returns |
|---------|-----------------|
| `bounds init` | Scaffolds `.bounds/`. `--root`, `--subsystem <name>`, `--namespace <ns>`, `--root <path>` |
| `bounds list` | All subsystems with role, criticality, exposes, consumes, consumed_by. `--namespace <ns>` filters |
| `bounds describe <name>` | One subsystem's merged Tier-1+2 surface as JSON (`verified`/`file`/`entry_point` per expose). `--namespace <ns>` describes a whole group; `--deep` adds the (stubbed) Tier-3 LLM tier |
| `bounds impact <name>` | Transitive consumer blast radius + which interfaces each direct consumer relies on. Zero LLM |
| `bounds validate` | Full validation — all 6 checks. `--quick`, `--mode quick\|full\|preflight\|hotfix\|audit`, `--enforce on\|off`, `--base <ref>` |
| `bounds preflight` | 6 pre-PR checks in blocking mode |
| `bounds overview` | Project dashboard: subsystem health, file counts, language breakdown |
| `bounds discover` | Auto-generate candidate manifests from un-bounded source. `--apply`, `--namespace <ns>`, `--merge-into 'name=p1,p2'` |
| `bounds calibrate` | Reconcile manifests vs tree-sitter reality (ADD / REMOVE / NEEDS_REVIEW / `consumes` fixes). `--apply`, `--subsystem <n>` |
| `bounds agent` | Wire Bounds into eight coding agents. `--sync`, `--detect`, `--check`, per-agent flags |
| `bounds ci` | Generate CI gate config. `--install`, `--action`, `--precommit`, `--gitlab`, `--all` |
| `bounds cache` | Manage the binary `.bounds/cache.db`. `--inspect`, `--prune`, `--migrate` |

---

## `validate` / `preflight` flags

Both take file-selection and output toggles (all default off):

| Flag | Effect |
|------|--------|
| `--include-ignored` | Scan files normally excluded by `.boundsignore` |
| `--include-gitignored` | Scan files excluded by `.gitignore` |
| `--follow-symlinks` | Follow external symlinks instead of skipping them with a warning |
| `--fail-on-unowned` | Treat tracked source files outside every subsystem as a blocking error |
| `--ci` | CI plaintext output: one tab-delimited issue per line, for log grepping |

---

## CI gates in one command

`bounds ci --install` generates ready-to-commit gate config (idempotent, path-gated):

- **`.github/workflows/bounds.yml`** — runs `bounds preflight --ci`, and uses `actions/cache@v4`
  keyed on `root.yaml` + the manifests so a fresh branch reuses main's warm cache.
- **`.pre-commit-config.yaml`** — a local `bounds validate --quick --ci` hook.
- **`.gitlab-ci.yml`** — the GitLab equivalent.

`--action` / `--precommit` / `--gitlab` / `--all` select targets. Putting `[skip bounds]` in a commit
message is the documented skip convention.

CI is the **one hard enforcement point** — it runs in your pipeline, not in the agent. See
[./team-workflow.md](./team-workflow.md) for the enforced loop.

---

## Custom roles & criticality

By default the four built-in roles (`service` / `platform` / `connector` / `library`) and the
`core` / `connector` / `leaf` criticality levels apply. `root.yaml` can declare custom `roles:`
(each `extends:` a built-in base, optionally overriding `orphan_exposes`) and custom `criticality:`
levels (each `{depth: <int>}`; `-1` unbounded, `0` none, `N` hops). With no custom block the
built-ins apply, so this is fully backward compatible; an invalid label gets a typo suggestion in
the error `fix`.
