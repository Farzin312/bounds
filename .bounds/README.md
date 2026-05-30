# Bounds

This directory contains Bounds subsystem boundary manifests — machine-readable
YAML files that describe the architecture of this project to AI coding agents.

## Quick start

```bash
pip install bounds
bounds list                   # discover subsystems
bounds describe <name>        # one subsystem in detail
bounds validate --human       # check manifests vs source
```

## Why this directory is hidden

The `.bounds/` directory is deliberately hidden (`.`-prefixed) to prevent
accidental AI discovery. Agents should only load Bounds data when explicitly
asked — never by browsing the filesystem. Use the `bounds` CLI to access
everything.

## Structure

```
.bounds/
  root.yaml               # Global config — project name, languages, enforce mode
  manifests/              # One YAML file per subsystem (~10-20 lines each)
    auth.yaml
    billing.yaml
    ...
  state.json              # Content-addressed extraction cache (gitignored)
```

## For AI agents

Run `bounds list` to see all subsystems. Run `bounds describe <name>` for one.
Run `bounds overview` for project health. The output is JSON by default —
parse it programmatically. Use `--human` for readable terminal output.
