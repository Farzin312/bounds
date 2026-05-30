# Install & onboarding

*Get Bounds installed (git works today), then onboard a repo in one command.*

[← Docs index](./README.md) · [Bounds README](../README.md)

---

## Install

**Works today — install from a git ref.** The PyPI release workflow is configured but the package is
not published yet, so install directly from the repo:

```bash
# Recommended: isolated CLI install from the repo (pipx sidesteps PEP 668)
pipx install "git+https://github.com/Farzin312/bounds.git"

# Or into the current environment
pip install "git+https://github.com/Farzin312/bounds.git"
```

**From a local clone (development):**

```bash
git clone https://github.com/Farzin312/bounds.git
cd bounds
pip install -e ".[dev]"     # editable install + pytest
```

**Bootstrap installer (`install.sh`)** — the PEP-668-safe bootstrap (pipx-preferred). It targets the
PyPI package by default (so it fully works once `bounds` is published), but you can point it at a git
ref today:

```bash
BOUNDS_REF=main ./install.sh   # installs git+https://github.com/Farzin312/bounds@main
```

The installer never does `curl | sh` remote execution, `eval`, or `sudo` — it only runs `pipx`/`pip`
against the package name.

### Verify

```bash
bounds --help    # verify the install
```

---

## Install channels

| Channel | Command | Status |
|---------|---------|--------|
| **pipx (git)** | `pipx install "git+https://github.com/Farzin312/bounds.git"` | **Works today** |
| **pip (git)** | `pip install "git+https://github.com/Farzin312/bounds.git"` | **Works today** |
| **Clone + editable** | `pip install -e ".[dev]"` | **Works today** |
| **install.sh** | `BOUNDS_REF=main ./install.sh` (git ref) | **Works today** (PyPI default mode pending publish) |
| **pip / pipx (PyPI)** | `pipx install bounds` | Release workflow configured — pending PyPI publish |
| **Homebrew** | `brew install Farzin312/bounds/bounds` | Bootstrapped — depends on PyPI publish |
| **curl** | `curl -sSL .../install.sh \| bash` | Bootstrapped — depends on PyPI publish |
| **Standalone signed binary** | (no Python required) | Planned (v0.2.0) |
| **conda-forge / Docker** | `conda install` / `docker pull` | Planned (v0.2.0) |

The Homebrew tap formula (`Formula/bounds.rb`) and `curl | bash` flow are wired up but resolve the
package from PyPI, so both depend on the PyPI publish landing first.

---

## Onboard a project (one command)

```bash
cd your-project
bounds discover                     # preview auto-generated manifests   (dry-run)
bounds discover --apply             # write root.yaml + manifests
bounds agent --sync                 # wire Bounds into your coding agents
```

`bounds discover` groups source files by directory, scores candidates, tree-sitter-extracts each
subsystem's verified `exposes`, infers `consumes` from the cross-candidate import graph, and seeds a
`role`/`criticality` from graph degree. It never overwrites existing manifests.

See [./ai-agents.md](./ai-agents.md) for what `bounds agent --sync` writes and how compliance works.

### Or scaffold manually

```bash
bounds init --root                  # scaffold .bounds/root.yaml
bounds init --subsystem auth        # add .bounds/manifests/auth.yaml
# edit the manifest to declare paths, exposes, consumes...
```

---

## Explore, validate, keep honest

```bash
bounds list                         # whole-system map: every subsystem  (JSON)
bounds describe auth                # one subsystem's verified surface    (JSON)
bounds impact auth                  # who breaks if auth's surface changes
bounds validate --quick             # fast incremental check              (JSON)
bounds validate --human             # same data, human-readable
bounds preflight                    # 6 pre-PR checks, blocking
bounds overview                     # project health dashboard
bounds calibrate                    # reconcile manifests vs source (diff; --apply to write)
```

> `.bounds/` is hidden and **only** touched by the `bounds` CLI — nothing auto-loads it. Its
> extraction cache is a **binary SQLite file** (`.bounds/cache.db`), so a tool that blindly `cat`s
> every file in a directory gets binary bytes rather than a parseable token blob. This is an
> *accidental-context-burn* defense, **not** access control: the manifests in
> `.bounds/manifests/*.yaml` are plain human-readable YAML and any agent can still read them
> directly.
