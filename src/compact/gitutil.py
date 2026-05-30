"""Git repository detection and changed-file discovery (stdlib only).

These helpers back ``compact validate --quick``: the caller intersects the set of
changed files with subsystem paths to decide what to re-extract. Everything here is
*fail soft* — if ``git`` is missing, the path is not a repo, or a git command errors,
the not-a-repo helpers return ``False``/``None`` and :func:`changed_files` returns an
empty list so the caller can transparently fall back to full mode.

Paths are handled with :mod:`pathlib` only. Git emits POSIX-style repo-relative paths
on every platform, so absolute paths are rebuilt as ``repo / relname``, which yields a
correct native path on Windows as well.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _run_git(repo: Path, args: list[str]) -> str | None:
    """Run ``git <args>`` inside ``repo`` and return stdout, or ``None`` on any failure.

    Failure includes git not being installed (``FileNotFoundError``) and any non-zero
    exit status. This never raises; callers treat ``None`` as "no information".
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError):
        # git binary not installed / not executable.
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def is_git_repo(path: Path) -> bool:
    """Return True if ``path`` is inside a git work tree."""
    out = _run_git(path, ["rev-parse", "--is-inside-work-tree"])
    return out is not None and out.strip() == "true"


def repo_root(path: Path) -> Path | None:
    """Return the absolute repository root containing ``path``, or ``None``.

    Uses ``git -C <path> rev-parse --show-toplevel``. Returns ``None`` if ``path`` is
    not a git repo or git is unavailable.
    """
    out = _run_git(path, ["rev-parse", "--show-toplevel"])
    if out is None:
        return None
    top = out.strip()
    if not top:
        return None
    return Path(top)


def _lines(out: str | None) -> list[str]:
    """Split git stdout into non-blank lines; tolerate ``None``."""
    if not out:
        return []
    return [line for line in out.splitlines() if line.strip()]


def changed_files(repo: Path, base: str = "HEAD") -> list[Path]:
    """Return the absolute paths of files changed in ``repo`` relative to ``base``.

    The result is the de-duplicated union of:
      * tracked modifications  — ``git diff --name-only <base>``
      * staged changes         — ``git diff --name-only --cached <base>``
      * untracked files        — ``git ls-files --others --exclude-standard``
        (respects ``.gitignore``)

    Paths are returned as absolute :class:`~pathlib.Path` objects (``repo / relname``),
    sorted for deterministic output. If ``repo`` is not a git repository, git is not
    installed, or any command errors, an empty list is returned so the caller can fall
    back to full-mode validation. This function never raises for the not-a-repo case.
    """
    if not is_git_repo(repo):
        return []

    relnames: set[str] = set()
    relnames.update(_lines(_run_git(repo, ["diff", "--name-only", base])))
    relnames.update(_lines(_run_git(repo, ["diff", "--name-only", "--cached", base])))
    relnames.update(_lines(_run_git(repo, ["ls-files", "--others", "--exclude-standard"])))

    return [repo / rel for rel in sorted(relnames)]
