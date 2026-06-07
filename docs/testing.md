# Testing & regression guide (for contributors and AI agents)

Bounds ships correctness as a feature: deterministic, fail-soft, JSON-first structural analysis. The
test suite is how we keep that true as the code changes. This page is the contract for **how to write
tests here**, **what must never regress**, and — the part people get wrong — **how to tell an
intended behavior change from a real regression, and update the baselines correctly.**

If you are an AI agent making a change in this repo: you are expected to add or update tests for every
behavior you touch, run the full suite, and follow the "intended change" rules below before editing
any snapshot/version/baseline. A green suite after a silent baseline bump is not a passing change.

---

## Running the suite

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # first time
.venv/bin/pytest -q                 # everything
.venv/bin/pytest tests/discover/ -q                         # one area
.venv/bin/pytest -k tsconfig -q                             # by keyword
.venv/bin/pytest -q -rxX             # show xfail/xpass reasons (see below)
```

Tests are pure-Python and offline. A handful shell out to `git` and are auto-skipped when git is
absent (`requires_git`). Nothing in the suite hits the network — the cross-repo benchmark
(`benchmarks/oss_bench.py`, `oss_features.py`) is a *separate*, opt-in tool, not part of `pytest`.

---

## Layout — tests are grouped by area

Tests live in `tests/<area>/` so a file stays focused on one subsystem instead of growing into one
giant catch-all. Shared fixtures (`sample_project`, `py_project`, `git_init`, …) live in the single
root `tests/conftest.py`, which pytest makes available to every subfolder.

| Folder | Covers |
|--------|--------|
| `tests/extract/` | language adapters, the import resolver, tsconfig aliases, SQL/schema folds |
| `tests/validate/` | the validation engine, checks, file ownership, mapping coverage, schema flex |
| `tests/discover/` | bootstrap: `discover` + `calibrate` |
| `tests/cli/` | the CLI surface (commands, `guide`) |
| `tests/agent/` | `agent --sync` artifacts + CI config generation |
| `tests/cache/` | the binary SQLite cache |
| `tests/meta/` | versioning + the update/upgrade checks |

Put a new test in the folder for the code it exercises. Keep each file to one area; if a file grows
past a few hundred lines, split it by concern (e.g. `validate/` splits checks vs engine). Give each
test a one-line docstring saying what it pins (and *why*, if non-obvious) — the name says "what," the
docstring says "why this matters."

When a test covers a new subsystem edge case, update
[subsystem-edge-cases.md](subsystem-edge-cases.md) in the same PR. The catalog is the human-readable
index of the weird cases the suite protects, so future fixes do not have to rediscover the reasoning
from individual test files.

## How tests are written here

- **Build a throwaway project under `tmp_path`.** Write source files + a `.bounds/` (root.yaml +
  manifests) and run the real code against it. See `tests/discover/test_discover.py::_project` and
  `tests/validate/test_regression_nested_paths.py::_nested_project` for the pattern. `git`-init the fixture
  (`_git_init`) when the code under test consults `.gitignore`; or pass `include_gitignored=True`.
- **Call the in-process entry points, not the CLI**, where you can:
  `bounds.core.discover.run_discover`, `bounds.core.validate.engine.run(root, mode="full")`,
  `bounds.core.describe.*`. The pre-refactor module paths remain compatibility aliases, but new
  code should use the canonical layered imports. Reserve `subprocess` for
  things that genuinely need the binary (exit codes, `git`).
- **Assert on the data, not the rendering.** Compare the JSON/dataclass fields. Never assert on a
  `--human` string unless the test is specifically about human rendering.
- **Name regressions after the defect.** A test that locks in a fix should say so:
  `test_overlapping_aliases_prefer_longest_prefix`, `test_discover_overwrites_hardcoded_python_default_for_ts`.
  One assertion that would have caught the bug beats five that wouldn't.

---

## What must never regress (the invariants — test these explicitly)

These are the binding constraints from `ARCHITECTURE.md` / `docs/coding-standards.md`. A change that
breaks one is a blocking failure even if every other test is green.

1. **Determinism.** No timestamps, `random`, wall-clock, or set-iteration order in any hash or
   serialized output. Sort at serialization boundaries. *Test it:* run the command twice and assert
   byte-identical output; assert collections come back sorted.
2. **Fail-soft, report hard.** One unparsable/unsupported file becomes an `Issue`
   (`E_EXTRACTION_FAILED` / `E_UNSUPPORTED_LANGUAGE`), never a crash. Only genuinely fatal conditions
   (no `.bounds/`, bad YAML, unknown subsystem) raise `BoundsError`. *Test it:* feed a garbage file
   and assert you get an Issue + a clean exit, not a traceback. (The benchmark's 0-crashes-across-456-
   commands result is this invariant, measured.)
3. **JSON-first.** Every command prints one JSON object; `--human` re-renders the **same** data and
   must never expose a field the JSON omits. *Test it:* if `--human` shows files, assert the JSON
   carries them too. (This is exactly BUG-8 in the cross-language report — a real violation.)
4. **Stable error codes.** Codes in `errors.py` are a public contract: only *add*, never renumber,
   rename, or repurpose. *Test it:* assert on the symbolic code (`errors.E_STRUCTURAL_DRIFT`), and a
   code-existence test guards the registry.
5. **Cross-platform posix paths.** Compare/store repo-relative paths in posix form (`as_posix()`).
   Never hard-code `/` or `\`. *Test it:* assert manifest paths and `where`/`describe` output are
   posix.
6. **Most-specific-wins resolution.** Both tsconfig alias resolution and (once BUG-1 is fixed)
   file-ownership follow the same rule: the longest/deepest matching prefix wins. New resolver code
   must keep a longest-prefix test (see `tests/extract/test_tsconfig.py::test_overlapping_aliases_prefer_longest_prefix`).

---

## Tests for known-but-unfixed bugs (`xfail`)

When you *find* a bug you aren't fixing yet, capture it as a **strict xfail** that asserts the
**correct** behavior — don't leave it undocumented and don't assert the buggy behavior (that would
lock the bug in).

```python
@pytest.mark.xfail(strict=True, reason="BUG-1: nested-path ownership; fix = most-specific-path-wins")
def test_nested_child_keeps_its_own_exports(tmp_path):
    ...
    assert drift == []          # the behavior we WANT
```

`strict=True` is the key: while the bug exists the test `XFAIL`s (suite stays green); the moment
someone fixes it, the test passes, which under strict xfail is reported as an **`XPASS` failure**.
That failure is the signal — the fixer deletes the `xfail` marker and the test becomes a permanent
regression guard. `tests/validate/test_regression_nested_paths.py` is the worked example. List open ones in
the cross-language report's bug section so they're discoverable.

---

## Intended behavior changes — how to update baselines (read before you "fix a failing test")

Some failures are *correct*: your change deliberately changed an output. The wrong move is to edit
the test until it's green. The right move depends on **what** changed:

| You changed… | The failure you'll see | The correct update |
|--------------|------------------------|--------------------|
| **Extraction output** for unchanged source (new symbol kind, different export detection, a new adapter) | content-hash / cache tests differ | **Bump `config.STATE_VERSION`** so caches re-extract; update the affected extraction assertions to the new, intended output. Do *not* pin the old hash. |
| **A subsystem's declared contract** vs source (you intentionally widened a public API) | `E_STRUCTURAL_DRIFT` in `validate` | Re-run `bounds calibrate --apply` to reconcile the manifest, or `bounds calibrate --dump-baseline` to record the accepted drift for the `calibrate --check` CI gate. Commit the updated manifest/baseline. |
| **Added a new diagnostic / error code** | new Issue appears in outputs | **Append** to `errors.py` (never renumber); add a test asserting the new code fires on the triggering input; update any "expected issue count" assertions intentionally. |
| **Changed JSON shape** of a command | downstream/`--human` parity tests differ | Update the JSON fixture *and* the `--human` renderer together so invariant #3 holds; note the shape change in `ARCHITECTURE.md`. |
| **A token-economics number** moved (new tokenizer, new repo) | benchmark numbers differ | These live in `benchmarks/results/*.md`, not `pytest`. Regenerate with the harness, record the cited commit + tokenizer, and update the README/docs numbers in the same change. |

**Decision rule:** before changing a test/baseline, ask *"is the new output what a correct Bounds
should produce?"* If yes → update the baseline by the table above and say so in the commit. If you're
not sure → it's a regression; fix the code, not the test. When a fix and an intended change land
together, keep them in separate commits so a reviewer can tell which is which.

---

## Before you open a PR

- `.venv/bin/pytest -q` is green (no new `XPASS` from a strict xfail you didn't mean to fix).
- Every behavior you changed has a test; every bug you fixed has a named regression test.
- Determinism/fail-soft/JSON-first/error-code invariants still hold for the surface you touched.
- If you changed any *number* a doc cites, you regenerated it and updated the doc + the cited commit.

See also: [coding-standards.md](coding-standards.md) (the PR-checkable invariants),
[../CONTRIBUTING.md](../CONTRIBUTING.md) (dev setup), and
[../benchmarks/results/oss-cross-language.md](../benchmarks/results/oss-cross-language.md) (the
cross-repo correctness/economics baseline and the open bugs these tests track).
