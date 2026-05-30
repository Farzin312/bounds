#!/usr/bin/env bash
#
# install.sh — Bounds bootstrap installer
# ----------------------------------------
# This is the BOOTSTRAP installer for Bounds (https://github.com/Farzin312/bounds).
#
# What it does today (v0.1.x):
#   - Installs the published Python package `bounds` from PyPI using an isolated,
#     PEP-668-safe method (pipx preferred, `pip install --user` fallback).
#   - Honest about the macOS PEP 668 "externally-managed-environment" failure and
#     guides you to the method that actually works.
#
# What it does NOT do (deliberately):
#   - No `curl | sh`-style remote code execution, no `eval`, no `sudo`, no telemetry.
#   - It only ever runs `pipx`/`pip` against the package name. Nothing else executes.
#
# PLANNED (v0.2.0 — NOT available yet):
#   - Standalone, code-signed single-file binaries published via GitHub Releases.
#   - A hosted, checksum-verified installer at https://bounds.dev/install.sh.
#   These do not exist today; this script will NOT fabricate or download them.
#
# Optional override:
#   BOUNDS_REF=<git-ref>  Install from a git ref instead of PyPI, e.g.:
#       BOUNDS_REF=main ./install.sh
#   installs:  git+https://github.com/Farzin312/bounds@main
#
# Usage:
#   chmod +x install.sh
#   ./install.sh
#
set -eu

REPO_URL="https://github.com/Farzin312/bounds"
PKG_NAME="bounds"
PYPI_SPEC="bounds"            # change to e.g. bounds==0.1.0 to pin
GIT_BASE="git+${REPO_URL}"

# ---- helpers --------------------------------------------------------------

info()  { printf '  %s\n' "$1"; }
step()  { printf '\n==> %s\n' "$1"; }
warn()  { printf '\n[!] %s\n' "$1" >&2; }
fail()  { printf '\n[x] %s\n' "$1" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

# Build the install target: either a git ref (if BOUNDS_REF is set) or PyPI spec.
install_target() {
  if [ -n "${BOUNDS_REF:-}" ]; then
    printf '%s@%s' "$GIT_BASE" "$BOUNDS_REF"
  else
    printf '%s' "$PYPI_SPEC"
  fi
}

# ---- detect OS ------------------------------------------------------------

step "Bounds installer"
OS_NAME="$(uname -s 2>/dev/null || echo unknown)"
ARCH_NAME="$(uname -m 2>/dev/null || echo unknown)"
case "$OS_NAME" in
  Darwin) PRETTY_OS="macOS" ;;
  Linux)  PRETTY_OS="Linux" ;;
  *)      PRETTY_OS="$OS_NAME" ;;
esac
info "Detected OS:   $PRETTY_OS ($ARCH_NAME)"

TARGET="$(install_target)"
if [ -n "${BOUNDS_REF:-}" ]; then
  info "Install source: git ref '${BOUNDS_REF}' -> ${TARGET}"
else
  info "Install source: PyPI package '${PYPI_SPEC}'"
fi

# Already installed? Idempotent: report and exit cleanly.
if have bounds; then
  step "bounds is already on your PATH"
  bounds --version 2>/dev/null || true
  info "Nothing to do. To upgrade with pipx:  pipx upgrade ${PKG_NAME}"
  info "To force a reinstall from this script, remove it first:  pipx uninstall ${PKG_NAME}"
  exit 0
fi

# ---- strategy 1: pipx (preferred — isolated, sidesteps PEP 668) -----------

if have pipx; then
  step "Installing with pipx (isolated environment)"
  info "Running: pipx install ${TARGET}"
  if pipx install "$TARGET"; then
    step "${PKG_NAME} installed — run: bounds --help"
    info "(If 'bounds' is not found, run 'pipx ensurepath' and open a new shell.)"
    exit 0
  fi
  fail "pipx install failed. See the output above for details."
fi

# ---- strategy 2: pip --user, with PEP 668 detection -----------------------

if have python3 && python3 -m pip --version >/dev/null 2>&1; then
  step "pipx not found — trying: python3 -m pip install --user ${TARGET}"
  PIP_LOG="$(python3 -m pip install --user "$TARGET" 2>&1)" && PIP_OK=1 || PIP_OK=0
  printf '%s\n' "$PIP_LOG"

  if [ "$PIP_OK" -eq 1 ]; then
    step "${PKG_NAME} installed via pip --user — run: bounds --help"
    info "If 'bounds' is not on PATH, add your user scripts dir, e.g.:"
    info "  python3 -m site --user-base   # then add its 'bin' to PATH"
    exit 0
  fi

  # Detect the PEP 668 externally-managed failure and steer to pipx.
  case "$PIP_LOG" in
    *externally-managed-environment*|*externally\ managed*)
      warn "Your Python is an 'externally-managed-environment' (PEP 668)."
      info "This is expected on fresh macOS (Homebrew Python) and modern Linux distros."
      info "pip --user is blocked here on purpose. The clean fix is pipx:"
      info ""
      info "  # macOS:"
      info "  brew install pipx && pipx ensurepath"
      info "  # Linux (Debian/Ubuntu):"
      info "  sudo apt install pipx && pipx ensurepath   # (your call on sudo for the package manager)"
      info "  # or, anywhere with pip:"
      info "  python3 -m pip install --user pipx && python3 -m pipx ensurepath"
      info ""
      info "Then re-run this script, or directly:  pipx install ${TARGET}"
      fail "Install blocked by PEP 668. Install pipx (above) and retry."
      ;;
    *)
      fail "pip install failed. See the output above. A virtualenv or pipx is recommended:
    python3 -m venv .venv && . .venv/bin/activate && pip install ${TARGET}"
      ;;
  esac
fi

# ---- no usable installer found --------------------------------------------

fail "No supported installer found (need 'pipx' or a working 'python3 -m pip').
Install Python 3.10+ and pipx, then re-run:
    # macOS:  brew install pipx && pipx ensurepath
    # then:   pipx install ${TARGET}"
