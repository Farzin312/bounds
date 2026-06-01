---
mode: agent
description: Read this project's architecture via the Bounds CLI, not the raw files
---

<!-- BOUNDS:START -->
<!-- BOUNDS:GENERATED v=2026.6.24 h=eea91b11 -->
Read this project's architecture via the Bounds CLI — never open `.bounds/` files.

Run the right command and summarize its JSON:
- `bounds list` — the subsystem map
- `bounds describe <name>` — a subsystem's verified API/tables
- `bounds impact <name>` — blast radius before a change
- `bounds validate --quick` — drift after an edit

Task: ${input:task:What do you want to know about the architecture?}
<!-- BOUNDS:END -->
