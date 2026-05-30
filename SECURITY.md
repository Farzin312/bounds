# Security

Bounds is designed with security as a core principle. This document outlines our security guarantees, vulnerability disclosure process, and distribution integrity.

## Security Principles

1. **No code execution at install time.** Bounds installs as a pure Python wheel from PyPI. There are no setup.py scripts, no post-install hooks, no Makefile invocation. Installation is deterministic and reproducible.

2. **No network calls at runtime.** Bounds runs entirely local. No telemetry, no analytics, no API calls, no phone-home. Not even an opt-out toggle — it simply never connects to a network.

3. **No credential handling.** Bounds never asks for, stores, or transmits API keys, tokens, passwords, or secrets. It reads source files and YAML manifests — nothing more.

4. **No eval/exec.** Bounds uses tree-sitter for parsing (safe C library via Python bindings) and PyYAML for YAML parsing. There is no use of `eval()`, `exec()`, `__import__()`, or any form of dynamic code execution.

5. **Hidden directory safety.** Bounds stores all its data in `.bounds/` — a hidden directory at the project root. It writes only within `.bounds/` or the project root. It never writes to system directories, configuration files, or locations outside its scope.

6. **Dependency minimums.** `pyproject.toml` specifies minimum versions for dependencies, not pinned exact versions. The Python packaging ecosystem handles resolution. No lockfile is shipped — users receive the latest compatible dependency versions.

7. **Signed releases (future).** GitHub Release attestations with sigstore/cosign are planned for v0.2.0. Every release artifact will carry a verifiable signature.

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

| Channel | Command | Status |
|---------|---------|--------|
| **pip** | `pip install bounds` | Implemented — published to PyPI |
| **pipx** | `pipx install bounds` | Works (pipx uses PyPI) |
| **Clone + pip** | `pip install git+https://github.com/Farzin312/bounds.git` | Works |
| **curl** | `curl -sSL https://bounds.dev/install.sh \| bash` | Planned (v0.2.0) |
| **Homebrew** | `brew install bounds` | Planned (v0.2.0) |
| **conda-forge** | `conda install bounds` | Planned (v0.2.0) |
| **Docker** | `docker pull bounds/bounds` | Planned (v0.2.0) |
