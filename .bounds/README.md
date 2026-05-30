# Compact

This directory contains Compact subsystem boundary manifests — machine-readable
YAML files that describe the architecture of this project to AI coding agents.

## Quick start

```bash
pip install compact
compact list                  # discover subsystems
compact describe <name>      # one subsystem in detail
compact validate --human      # check manifests vs source
```

## Why this directory is hidden

The `.compact/` directory is deliberately hidden (`.`-prefixed) to prevent
accidental AI discovery. Agents should only load Compact data when explicitly
asked — never by browsing the filesystem. Use the `compact` CLI to access
everything.

## Structure

```
.compact/
  root.yaml               # Global config — project name, languages, enforce mode
  manifests/              # One YAML file per subsystem (~10-20 lines each)
    auth.yaml
    billing.yaml
    ...
  cache/
    state.json            # Content-addressed extraction cache (gitignored)
```

## For AI agents

Run `compact list` to see all subsystems. Run `compact describe <name>` for one.
Run `compact overview` for project health. The output is JSON by default —
parse it programmatically. Use `--human` for readable terminal output.
