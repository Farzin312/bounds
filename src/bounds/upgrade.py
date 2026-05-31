"""Opt-in self-upgrade helper for the ``bounds upgrade`` command.

This module is deliberately outside every structural path. It shells out to ``pipx``
only when the user explicitly runs ``bounds upgrade``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import config

_PACKAGE_NAME = "bounds-cli"


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
        "fallback_commands": fallback_commands(ref=ref, local=local, pipx=pipx),
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "note": "",
    }
    if dry_run:
        payload["note"] = "dry run: no upgrade command executed"
        return payload

    first = _run(primary)
    payload["returncode"] = first.returncode
    payload["stdout"] = _tail(first.stdout)
    payload["stderr"] = _tail(first.stderr)
    if first.returncode == 0:
        payload["note"] = "upgrade completed"
        return payload

    # Some pipx versions fail --force when the venv already exists. Fall back to an
    # explicit uninstall/install sequence, still under the user's explicit upgrade command.
    fallback = fallback_commands(ref=ref, local=local, pipx=pipx)
    uninstall = _run(fallback[0])
    install = _run(fallback[1])
    payload["returncode"] = install.returncode
    payload["stdout"] = _tail("\n".join([first.stdout, uninstall.stdout, install.stdout]))
    payload["stderr"] = _tail("\n".join([first.stderr, uninstall.stderr, install.stderr]))
    payload["ok"] = install.returncode == 0
    payload["note"] = "upgrade completed via reinstall fallback" if payload["ok"] else "upgrade failed"
    return payload


def refresh_command() -> str:
    """Human-facing default refresh command, single-sourced from config."""
    return config.UPGRADE_INSTALL_CMD


def _run(command: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))


def _tail(text: str, limit: int = 4000) -> str:
    text = text or ""
    return text[-limit:]
