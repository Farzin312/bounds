<div align="center">
  <a href="../README.md">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="../assets/bounds-wordmark.svg">
      <img src="../assets/bounds-wordmark-light.svg" alt="Bounds" width="220">
    </picture>
  </a>
</div>

# Bounds documentation

Everything beyond the [project README](../README.md): the rationale, day-to-day workflows, the full
command reference, and the deep dives on how Bounds works and why it pays off.

## Start here

- [why-bounds.md](why-bounds.md) — the rationale: giving agents a token-lean verified contract, seeing blast radius before a change, and catching drift in CI.
- [team-workflow.md](team-workflow.md) — how a team adopts and lives with Bounds day to day.
- [use-cases.md](use-cases.md) — concrete workflows: pre-PR safety, dropping an agent into a strange repo, enforcing architecture in CI.
- [sdd.md](sdd.md) — optional Spec-Driven Development integration: where Bounds grounds each phase and how manifests stay fresh with the spec.

## Reference

- [cli-reference.md](cli-reference.md) — every command, every flag, and the JSON/exit-code contract.
- [coverage.md](coverage.md) — the mapping-coverage signal, what "aiming for 100%" means, and the human-or-AI workflow for closing a gap.
- [ai-agents.md](ai-agents.md) — `bounds agent --sync`, the canonical `AGENTS.md` contract, the opt-in invocation levels (`off`/`nudge`/`strict`) that nudge or gate agents toward Bounds, and why CI stays the hard enforcement.
- [sdd.md](sdd.md) — the opt-in `root.yaml` SDD block, `bounds guide --sdd`, per-agent wiring, and freshness contract.
- [languages-and-platforms.md](languages-and-platforms.md) — the language support matrix (Python + TS/JS + SQL + Prisma today) and cross-platform notes.
- [install.md](install.md) — all install channels and their current status.

## Deep dives

- [how-it-works.md](how-it-works.md) — the three-tier data model, the validation engine and its architecture diagram, quick mode, and the binary cache.
- [token-economics.md](token-economics.md) — measured token costs (16-repo cross-language corpus, exact tiktoken), the scaling tables, and the context-rot argument.
- [comparison.md](comparison.md) — Bounds vs. code graphs, and what Bounds deliberately does *not* do.
- [testing.md](testing.md) — how to write tests here, the invariants that must never regress, the `xfail` pattern for known bugs, and how to update baselines for *intended* behavior changes.

---

Back to the [project README](../README.md) · Engineering contract: [ARCHITECTURE.md](../ARCHITECTURE.md).
