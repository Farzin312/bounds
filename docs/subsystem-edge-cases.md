# Subsystem edge-case catalog

*Known subsystem and manifest edge cases Bounds covers, with the command that should guide the fix.*

[Docs index](./README.md) | [Testing guide](./testing.md) | [Team workflow](./team-workflow.md)

---

This page is the durable checklist for subsystem edge cases. When a bug fix adds or changes an edge
case, update the relevant row here and add a one-sentence docstring to the regression test that
states what behavior it pins.

## Command scope

| Edge case | Covered behavior | Fix path |
|-----------|------------------|----------|
| `calibrate` run without `--apply` | Prints a diff only; it never writes manifests by default. | Review the diff, then run `bounds calibrate --apply` when the source/manifest change is intentional. |
| Structural drift remains after validate | Source exports/imports no longer match the manifest. | Run `bounds calibrate`, review, then `bounds calibrate --apply`; re-run `bounds validate -H`. |
| Cycle errors remain after calibrate | Cycles are source or boundary design problems, not manifest drift. | Break the dependency, move shared code into a lower library subsystem, or merge false boundaries; then validate again. |
| Coverage gap remains after calibrate | New or dark source files are not owned by any subsystem. | Add supported files to a subsystem `paths:` or hand-author a manifest for unsupported-language source. |
| Unknown `consumes` edge | A manifest names a subsystem that does not exist. | Declare/fix the target name, or use `bounds calibrate --prune-unknown --apply` for stale dangling edges. |
| Needs-review expose | A declared expose is missing from extracted source but still consumed, or belongs to unsupported source. | Update consumers or, for supported-language exports intentionally removed, use `--prune-missing-exports --apply`; re-verify unsupported exposes by hand. |
| Quick validation looks clean | `--quick` skips boundary, contract, cycle, coverage, and orphan checks; `coverage_summary.complete` is `null`. | Use quick after edits; use `bounds coverage -H`, `bounds validate -H`, or `bounds preflight --ci` before claiming full repo health. |

## Ownership and coverage

| Edge case | Covered behavior | Fix path |
|-----------|------------------|----------|
| Supported source outside every subsystem | Reported as `E_COVERAGE_GAP` with sample files. | Add the files to an existing subsystem `paths:` or scaffold/register a new one with `bounds init --subsystem <name> --path <file-or-dir>`. |
| Tool/build config classified as supported source | Reported under `supported.unowned_breakdown.algorithm_miss`, distinct from real source ownership decisions. | Preview exact paths with `bounds fix-coverage --auto`; write them only with `bounds fix-coverage --auto --apply`. |
| Unsupported source no manifest claims | Reported as dark unsupported source, not silently ignored. | Hand-author a subsystem manifest with `paths:` and public `exposes:`. |
| Unsupported source already declared | Counted as covered; hand-authored exposes are durable. | Keep exposes current manually until Bounds has an adapter for that language. |
| Unsupported source changed after confirmation | Reported as `E_UNSUPPORTED_SURFACE_STALE` when a surface baseline exists. | Re-check the hand-authored exposes, then run `bounds calibrate --dump-baseline`. |
| Equal-specificity overlapping paths | Reported as `E_SUBSYSTEM_OVERLAP`; ownership is deterministic but ambiguous. | Narrow one manifest path or assign the shared file explicitly with `files:`. |
| Root entry point outside subsystems | `--fail-on-unowned` reports it as a warning when matched by `entry_points`. | Keep sanctioned bootstrap files in `entry_points`; map real library source into subsystems. |

## Source and contract checks

| Edge case | Covered behavior | Fix path |
|-----------|------------------|----------|
| Boundary violation | A subsystem imports another subsystem's private or undeclared implementation. | Import a declared public interface, or intentionally widen the provider manifest and validate again. |
| Contract missing export | A consumer lists an interface the provider no longer exposes. | Restore the provider export, update the consumer contract, or remove stale consumption with calibration where appropriate. |
| Stale interface impact | Quick mode detects deleted provider files through cached ownership and warns consumers. | Update or remove downstream usage, then validate. |
| Generated source exports | Generated-file exports do not create undeclared-export drift. | Do not add generated internals to manifests unless they are real public contract. |
| Test-case exports | `test_*` functions/classes in test files are not treated as subsystem public surface. | Keep tests linked through `tests:` or conventions; do not promote test cases into `exposes:`. |
| Framework entry exports | Next.js route/page/layout exports are treated as framework-invoked entries. | Keep application components mapped, but do not add framework entry handlers just to silence drift. |

## Documentation and tests

| Edge case | Covered behavior | Fix path |
|-----------|------------------|----------|
| Linked docs/tests | `docs:` and `tests:` are linkage evidence, not generated architecture boundaries. | Link docs/tests to the subsystem they cover; keep source ownership separate. |
| Unlinked docs/tests | Reported as informational coverage buckets, not a blocking source gap. | Add explicit links when the file documents or tests a specific subsystem. |
| New regression test | Every `test_*` function carries a one-sentence docstring. | State what behavior the test pins and why it matters when non-obvious. |
| New subsystem edge case | The behavior is documented here and covered by a focused regression test. | Add or update a row in this catalog in the same PR as the fix. |
