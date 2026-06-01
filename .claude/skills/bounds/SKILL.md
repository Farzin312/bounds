---
name: bounds
description: Read this codebase's architecture with the Bounds CLI instead of grepping or opening files. Use when exploring an unfamiliar area, when you need a subsystem's public API or database tables, before changing a shared or core subsystem or a migration (check blast radius first), when asked what depends on X or what breaks if X changes, or to verify drift after an edit. Never read .bounds/ files directly.
---

<!-- BOUNDS:START -->
<!-- BOUNDS:GENERATED v=2026.6.24 h=dc2b1e91 -->
# Bounds — architecture navigation

Read this project's architecture through the Bounds CLI; never open `.bounds/` files or grep for
structure first. Output is JSON by default; add `-H` for human-readable.

## Which command for which task
- Find the right subsystem / get the map → `bounds list`
- A subsystem's verified public API or DB tables → `bounds describe <name>`
- Where a symbol or table is defined → `bounds where <symbol>`
- Blast radius before changing a subsystem or migration → `bounds impact <name>`
- Catch drift after an edit → `bounds validate --quick`

Run `bounds guide` for setup; see `AGENTS.md` for the full contract.
<!-- BOUNDS:END -->
