# Bounds documentation

Everything beyond the [project README](../README.md): the rationale, day-to-day workflows, the full
command reference, and the deep dives on how Bounds works and why it pays off.

## Start here

- [why-bounds.md](why-bounds.md) — the rationale: giving agents a token-lean verified contract, seeing blast radius before a change, and catching drift in CI.
- [team-workflow.md](team-workflow.md) — how a team adopts and lives with Bounds day to day.
- [use-cases.md](use-cases.md) — concrete workflows: pre-PR safety, dropping an agent into a strange repo, enforcing architecture in CI.

## Reference

- [cli-reference.md](cli-reference.md) — every command, every flag, and the JSON/exit-code contract.
- [ai-agents.md](ai-agents.md) — `bounds agent --sync`, the canonical `AGENTS.md` contract, and why agent compliance is advisory (CI is the only hard enforcement).
- [languages-and-platforms.md](languages-and-platforms.md) — the language support matrix (Python + TS/JS + SQL + Prisma today) and cross-platform notes.
- [install.md](install.md) — all install channels and their current status.

## Deep dives

- [how-it-works.md](how-it-works.md) — the three-tier data model, the validation engine and its architecture diagram, quick mode, and the binary cache.
- [token-economics.md](token-economics.md) — measured token costs, the scaling tables, and the context-rot argument (one repo, one data point).
- [comparison.md](comparison.md) — Bounds vs. code graphs, and what Bounds deliberately does *not* do.

---

Back to the [project README](../README.md) · Engineering contract: [ARCHITECTURE.md](../ARCHITECTURE.md).
