# Security

Bounds is designed with security as a core principle. This document outlines our security guarantees, vulnerability disclosure process, and distribution integrity.

## Security Principles

1. **No code execution at install time.** Bounds installs as a pure Python wheel. The shipped `Makefile` is a developer convenience (`make install/dev/test/validate/benchmark`) and is **never** invoked by the installer; it runs no network code-exec, telemetry, or sudo. The `install.sh` bootstrap only shells out to `pip`/`pipx`. There are no `setup.py` scripts and no post-install hooks. Installation is deterministic and reproducible.

2. **No network calls at runtime.** Bounds runs entirely local. No telemetry, no analytics, no API calls, no phone-home. Not even an opt-out toggle — it simply never connects to a network.

3. **No credential handling.** Bounds never asks for, stores, or transmits API keys, tokens, passwords, or secrets. It reads source files and YAML manifests — nothing more.

4. **No eval/exec.** Bounds uses tree-sitter for parsing (safe C library via Python bindings) and PyYAML for YAML parsing. The binary extraction cache (`.bounds/cache.db`) uses **SQLite, which is part of the Python standard library — so no new third-party dependency was added** and the security posture is unchanged. There is no use of `eval()`, `exec()`, `__import__()`, or any form of dynamic code execution.

5. **Hidden directory safety.** Bounds stores all its data in `.bounds/` — a hidden directory at the project root. It writes only within `.bounds/` or the project root. It never writes to system directories, configuration files, or locations outside its scope. The binary cache `.bounds/cache.db` (and its journal) is gitignored, so cache state is never committed.

6. **Dependency minimums.** `pyproject.toml` specifies minimum versions for dependencies, not pinned exact versions. The Python packaging ecosystem handles resolution. No lockfile is shipped — users receive the latest compatible dependency versions.

7. **Signed releases (future).** Standalone signed binaries with sigstore/cosign attestations are planned for v0.2.0. A PyPI release workflow is already configured for OIDC ("trusted publishing") on tag push, which removes long-lived publish tokens from the release path.

## Vulnerability Disclosure

If you discover a security vulnerability in Bounds, please report it privately before public disclosure.

**Contact:** Open a [GitHub Security Advisory](https://github.com/Farzin312/bounds/security/advisories) or email the maintainers directly.

**Disclosure window:** Bounds follows a 90-day disclosure window — 90 days from report to public fix, regardless of severity. No exploits, no bug bounties (v0.x).

## Download Verification (v0.2.0+)

Future GitHub Releases will include:

- `bounds-{version}.tar.gz` — source distribution
- `bounds-{version}-py3-none-any.whl` — pure Python wheel
- `bounds-{version}.tar.gz.asc` — GPG signature
- `bounds-{version}.tar.gz.sha256` — checksum
- `checksums.txt` — SHA-256 of all artifacts, signed

## Install Channels

A PyPI **release workflow is configured** (OIDC / trusted publishing, triggered on tag push). Until and after that publish completes, install via pipx/pip or from a clone — the `install.sh` bootstrap supports git installs via the `BOUNDS_REF` environment variable. The brew formula and the `curl | bash` bootstrap both depend on the PyPI publish, so they are not yet operational. Standalone signed binaries remain a v0.2.0 item.

| Channel | Command | Status |
|---------|---------|--------|
| **pipx** | `pipx install bounds` | Works once PyPI publish lands (OIDC workflow configured) |
| **pip** | `pip install bounds` | Works once PyPI publish lands (OIDC workflow configured) |
| **Clone + pip (git)** | `pip install git+https://github.com/Farzin312/bounds.git` | Works now |
| **install.sh (git ref)** | `BOUNDS_REF=main ./install.sh` | Works now (git install via `BOUNDS_REF`) |
| **install.sh (PyPI)** | `curl -sSL https://bounds.dev/install.sh \| bash` | Pending PyPI publish |
| **Homebrew** | `brew install bounds` | Pending PyPI publish (`Formula/bounds.rb` shipped) |
| **conda-forge** | `conda install bounds` | Planned (v0.2.0) |
| **Docker** | `docker pull bounds/bounds` | Planned (v0.2.0) |
