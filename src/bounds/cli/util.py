"""Shared CLI helpers to keep command implementations lean and consistent.
"""

from __future__ import annotations

import itertools
import sys
import threading
from contextlib import nullcontext
from pathlib import Path, PurePosixPath

from ..shared import config, errors, output
from ..core.manifest import loader as manifest_loader

def require_root() -> Path:
    """Return the resolved project root, or raise E_MANIFEST_NOT_FOUND."""
    root = manifest_loader.find_root(Path.cwd())
    if root is None:
        raise errors.BoundsError(
            errors.E_MANIFEST_NOT_FOUND,
            "no .bounds/ directory found in this or any parent directory",
            fix="run 'bounds init --root' to initialize Bounds in this project",
        )
    return root

class _Spinner:
    """Minimal stderr spinner for long-running commands; no-op when stderr is not a TTY."""
    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    _DELAY_SECONDS = 0.12
    _INTERVAL_SECONDS = 0.08

    def __init__(self, message: str) -> None:
        self._msg = message
        self._stop = threading.Event()
        self._drew = False
        self._thread = threading.Thread(target=self._spin, daemon=True)

    def _spin(self) -> None:
        if self._stop.wait(self._DELAY_SECONDS):
            return
        for frame in itertools.cycle(self._FRAMES):
            sys.stderr.write(f"\r{frame} {self._msg}")
            sys.stderr.flush()
            self._drew = True
            if self._stop.wait(self._INTERVAL_SECONDS):
                break

    def __enter__(self):
        if sys.stderr.isatty():
            self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1)
        if self._drew:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()

def progress(message: str):
    """Stderr spinner context manager."""
    return _Spinner(message)

def run_wrapped(human: bool, fn, ci: bool = False):
    """Execute a command body with standard error handling and JSON-first output."""
    try:
        fn()
    except errors.BoundsError as err:
        output.emit(err.to_dict(), human, ci=ci)
        sys.exit(config.EXIT_FATAL)
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        err = errors.BoundsError(
            errors.E_INTERNAL,
            "an unexpected internal error occurred",
            fix="re-run with -H/--human for more context, or file an issue at "
            "https://github.com/Farzin312/bounds/issues",
        )
        output.emit(err.to_dict(), human, ci=ci)
        sys.exit(config.EXIT_FATAL)

def interactive_human(explicit_human: bool) -> bool:
    """Whether an interactive action should announce in a terminal."""
    return explicit_human or sys.stdout.isatty()

def clean_manifest_path(raw: str) -> str:
    """Normalize one user-provided manifest path and reject traversal/absolute paths."""
    path = str(raw).strip().replace("\\", "/")
    if path.startswith("./"):
        path = path[2:]
    posix = PurePosixPath(path)
    if not path or posix.is_absolute() or ".." in posix.parts:
        raise errors.BoundsError(
            errors.E_USAGE,
            f"invalid subsystem path {raw!r}",
            fix="use a repo-relative path such as src/auth or src/auth/index.ts",
        )
    return posix.as_posix()
