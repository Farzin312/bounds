# OSS fresh-discover trust benchmark

This benchmark records whether Bounds is useful immediately on a real open-source repo, and whether
it is honest when the generated map is incomplete. It covers both sides of the first-run experience:
the recovery path before `.bounds/` exists, and the token/health signals after `discover --apply`.

- **Scope:** deterministic CLI benchmark, no model-in-the-loop judgment
- **Tokenizer:** `tiktoken cl100k_base (exact)`
- **Bounds version:** `bounds 2026.6.24`
- **Harness:** `python benchmarks/oss_bench.py --repo <temp clone> --name click --lang python`

## OSS repo: Click

Fresh temp clone of `https://github.com/pallets/click`.

| Signal | Result |
|--------|-------:|
| Without `.bounds`: `bounds list` | exit `2`, `E_MANIFEST_NOT_FOUND`, fix: `run 'bounds init --root' to initialize Bounds in this project` |
| `bounds init --root` | exit `0` |
| `bounds discover --apply` | exit `0` |
| Subsystems discovered | 4 |
| `bounds list` tokens | 243 |
| Source-equivalent map tokens | 196,257 |
| Whole-map reduction | **99.9%** |
| Key subsystem | `click` |
| `bounds describe click` tokens | 6,954 |
| `click` source tokens | 93,020 |
| Surface reduction | **92.5%** |
| Fresh validation errors | **0** |
| Fresh boundary violations | **0** |
| Fresh warnings | 1 (`E_COVERAGE_GAP`) |
| Mapped source | 60.6% |

Finding fixed during the audit: Click tests intentionally import private internals. Bounds previously
reported those as production `E_BOUNDARY_VIOLATION` errors on a fresh generated map. The validator now
uses the shared test-file predicate for boundary enforcement: tests still count for ownership,
coverage, and dependency context, but private test imports no longer make a generated model look
broken.

The remaining `E_COVERAGE_GAP` is the intended product behavior: Bounds is immediately useful for the
mapped Python package, but it does not claim the whole repository is covered. `bounds overview`
surfaces the same trust boundary through `health.validation.mapped_pct`, `trust_note`, and
`next_steps`.

## Dogfood repo: Bounds

`python benchmarks/run.py` on the Bounds repo:

| Command | Bounds tokens | Source-equivalent tokens | Reduction |
|---------|--------------:|-------------------------:|----------:|
| `bounds describe models` | 457 | 2,930 | 84.4% |
| `bounds list` | 1,597 | 118,604 | **98.7%** |
| `bounds impact models` | 380 | 2,930 | 87.0% |
| Aggregate | 2,434 | 124,464 | **98.0%** |

## Product claim

The release-safe claim is narrow and strong:

> Use Bounds before broad repo search to find the owner, contract, and blast radius. Trust verified
> mapped symbols. When coverage, overlap, drift, schema, or cycle warnings appear, Bounds says exactly
> what is not trustworthy yet and what to do next.

Regression coverage added with this benchmark locks:

- `overview.health.ok` follows error-severity validation even under `enforce=off`.
- `overview` reports duplicate same-path ownership and gives de-duplication next steps.
- `overview` reports `trust_note` and `next_steps` for partial maps.
- Test files can import internals without production boundary errors.
- The OSS benchmark records both the no-Bounds recovery UX and fresh-with-Bounds health metrics.
