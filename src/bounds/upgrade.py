"""Opt-in self-upgrade helper for the ``bounds upgrade`` command.

This module is deliberately outside every structural path. It shells out to ``pipx``
only when the user explicitly runs ``bounds upgrade``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import config

_PACKAGE_NAME = "bounds-cli"
# A pipx install builds from git (clone + wheel build); minutes, not seconds. Generous
# ceiling so a genuinely stuck install fails soft instead of hanging the spinner forever.
_UPGRADE_TIMEOUT_SECONDS = 600


def command_for(ref: str = "main", local: Path | None = None, pipx: str = "pipx") -> list[str]:
    """Return the primary pipx command for the requested upgrade source."""
    if local is not None:
        return [pipx, "install", "--force", "-e", str(local)]
    spec = "git+https://github.com/Farzin312/bounds.git"
    if ref and ref != "main":
        spec = f"{spec}@{ref}"
    return [pipx, "install", "--force", spec]


def fallback_commands(ref: str = "main", local: Path | None = None, pipx: str = "pipx") -> list[list[str]]:
    """Fallback reinstall commands when pipx refuses to reuse an existing venv."""
    install = command_for(ref=ref, local=local, pipx=pipx)
    install = [part for part in install if part != "--force"]
    return [[pipx, "uninstall", _PACKAGE_NAME], install]


def run_upgrade(
    *,
    ref: str = "main",
    local: Path | None = None,
    dry_run: bool = False,
    pipx: str = "pipx",
) -> dict:
    """Upgrade Bounds through pipx and return a stable JSON-serializable report."""
    source = "local" if local is not None else "github"
    primary = command_for(ref=ref, local=local, pipx=pipx)
    payload = {
        "ok": True,
        "source": source,
        "ref": ref if local is None else None,
        "local": local.as_posix() if local is not None else None,
        "dry_run": dry_run,
        "command": primary,
        "returncode": None,
        "version": None,
        # Semantic failure class so a consumer never has to interpret a raw pipx return
        # code: null on success/dry-run, else one of the _ERROR_* codes below.
        "error": None,
        # True when --force was refused and the uninstall+reinstall fallback path was taken
        # (whether or not that fallback then succeeded — check ``ok`` for the final outcome).
        "fallback_used": False,
        "stderr": "",
        "note": "",
    }
    if dry_run:
        payload["note"] = "dry run: no upgrade command executed"
        return payload

    first = _run(primary)
    payload["returncode"] = first.returncode
    if first.returncode == 0:
        payload["version"] = _extract_version(first.stdout)
        payload["note"] = "upgrade completed"
        return payload

    # Some pipx versions fail --force when the venv already exists. Fall back to an
    # explicit uninstall/install sequence, still under the user's explicit upgrade command.
    payload["fallback_used"] = True
    fallback = fallback_commands(ref=ref, local=local, pipx=pipx)
    uninstall = _run(fallback[0])
    install = _run(fallback[1])
    payload["returncode"] = install.returncode
    payload["ok"] = install.returncode == 0
    if payload["ok"]:
        payload["version"] = _extract_version(install.stdout)
        payload["note"] = "upgrade completed (after reinstall)"
    else:
        all_stderr = "\n".join(filter(None, [first.stderr, uninstall.stderr, install.stderr]))
        payload["stderr"] = _tail(all_stderr)
        payload["error"] = _error_class(install.returncode)
        payload["note"] = "upgrade failed"
    return payload


# Stable, machine-readable failure classes derived from the subprocess return code.
_ERROR_PIPX_NOT_FOUND = "pipx_not_found"  # 127 from _run's OSError branch (pipx missing)
_ERROR_TIMEOUT = "timeout"               # 124 from _run's TimeoutExpired branch
_ERROR_INSTALL_FAILED = "install_failed"  # any other non-zero pipx exit


def _error_class(returncode: int | None) -> str:
    """Map a pipx return code to a stable semantic error class for the JSON contract."""
    if returncode == 127:
        return _ERROR_PIPX_NOT_FOUND
    if returncode == 124:
        return _ERROR_TIMEOUT
    return _ERROR_INSTALL_FAILED


def refresh_command() -> str:
    """Human-facing default refresh command, single-sourced from config."""
    return config.UPGRADE_INSTALL_CMD


def _run(command: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command, capture_output=True, text=True, check=False,
            timeout=_UPGRADE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        msg = f"timed out after {_UPGRADE_TIMEOUT_SECONDS}s"
        return subprocess.CompletedProcess(command, 124, "", msg)
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))


def _extract_version(stdout: str) -> str | None:
    # Anchor to the known package name so an unexpected first line (multi-package or
    # reworded pipx output) can't yield the wrong version; fall back to None.
    m = re.search(rf"{re.escape(_PACKAGE_NAME)} ([^\s,]+)", stdout or "")
    return m.group(1) if m else None


def _tail(text: str, limit: int = 4000) -> str:
    text = text or ""
    return text[-limit:]
