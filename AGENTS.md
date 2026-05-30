<!-- BOUNDS:START -->
## Bounds

This project uses **Bounds** to model its architecture as subsystem boundary manifests.
Read the architecture through the Bounds CLI, never by opening the raw files.

- `bounds list` — map of all subsystems (roles + dependency counts)
- `bounds describe <name>` — one subsystem's verified public surface (~300 tokens)
- `bounds validate --quick` — catch structural drift after a change

**Never** read `.bounds/cache.db`, `.bounds/*.json`, or `.bounds/manifests/*.yaml` directly —
the cache is binary and the manifests bypass tree-sitter verification. The CLI is the API.
See `BOUNDS.md` for the full contract.
<!-- BOUNDS:END -->
