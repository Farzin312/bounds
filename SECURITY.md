# Security

Compact is designed with security as a core principle. This document outlines our security guarantees, vulnerability disclosure process, and distribution integrity.

## Security Principles

1. **No code execution at install time.** Compact installs as a pure Python wheel from PyPI. There are no setup.py scripts, no post-install hooks, no Makefile invocation. Installation is deterministic and reproducible.

2. **No network calls at runtime.** Compact runs entirely local. No telemetry, no analytics, no API calls, no phone-home. Not even an opt-out toggle — it simply never connects to a network.

3. **No credential handling.** Compact never asks for, stores, or transmits API keys, tokens, passwords, or secrets. It reads source files and YAML manifests — nothing more.

4. **No eval/exec.** Compact uses tree-sitter for parsing (safe C library via Python bindings) and PyYAML for YAML parsing. There is no use of `eval()`, `exec()`, `__import__()`, or any form of dynamic code execution.

5. **Hidden directory safety.** Compact stores all its data in `.compact/` — a hidden directory at the project root. It writes only within `.compact/` or the project root. It never writes to system directories, configuration files, or locations outside its scope.

6. **Dependency minimums.** `pyproject.toml` specifies minimum versions for dependencies, not pinned exact versions. The Python packaging ecosystem handles resolution. No lockfile is shipped — users receive the latest compatible dependency versions.

7. **Signed releases (future).** GitHub Release attestations with sigstore/cosign are planned for v0.2.0. Every release artifact will carry a verifiable signature.

## Vulnerability Disclosure

If you discover a security vulnerability in Compact, please report it privately before public disclosure.

**Contact:** Open a [GitHub Security Advisory](https://github.com/Farzin312/compact/security/advisories) or email the maintainers directly.

**Disclosure window:** Compact follows a 90-day disclosure window — 90 days from report to public fix, regardless of severity. No exploits, no bug bounties (v0.x).

## Download Verification (v0.2.0+)

Future GitHub Releases will include:

- `compact-{version}.tar.gz` — source distribution
- `compact-{version}-py3-none-any.whl` — pure Python wheel
- `compact-{version}.tar.gz.asc` — GPG signature
- `compact-{version}.tar.gz.sha256` — checksum
- `checksums.txt` — SHA-256 of all artifacts, signed

## Install Channels

| Channel | Command | Status |
|---------|---------|--------|
| **pip** | `pip install compact` | Implemented — published to PyPI |
| **pipx** | `pipx install compact` | Works (pipx uses PyPI) |
| **Clone + pip** | `pip install git+https://github.com/Farzin312/compact.git` | Works |
| **curl** | `curl -sSL https://compact.dev/install.sh \| bash` | Planned (v0.2.0) |
| **Homebrew** | `brew install compact` | Planned (v0.2.0) |
| **conda-forge** | `conda install compact` | Planned (v0.2.0) |
| **Docker** | `docker pull compact/compact` | Planned (v0.2.0) |
