"""Dynamic CalVer versioning driven by setuptools-scm.

The version is fully git-derived (no static string anywhere): ``YYYY.M.<build>`` where the
year and month come from the current commit's date and ``<build>`` is the repo's *total*
commit count. Using the total count — not setuptools-scm's distance-since-last-tag — keeps
the version strictly increasing for every commit forever, so it never regresses (and
``pipx upgrade`` stays reliable) even after release tags are introduced. No ``dev``/``+local``
suffixes: a build reads as a clean, ordered number like ``2026.6.24``.

A custom version scheme can't be wired through pyproject entry points here (the package
isn't installed during its own build), so it is passed as a callable via ``use_scm_version``.
This is still 100% dynamic — there is no version literal to go stale.
"""

from __future__ import annotations

import datetime
import subprocess

from setuptools import setup


def _commit_count() -> int | None:
    """Total commits reachable from HEAD — a monotonic build number that never resets.

    Runs only at build time inside a git checkout (an sdist install already has the version
    baked into PKG-INFO, so this is never reached there). Returns None on any failure so the
    caller can fall back to setuptools-scm's own distance.
    """
    try:
        out = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return int(out.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def calver(version) -> str:
    """``YYYY.M.<build>`` from the commit date + total commit count (both git-derived).

    The build component is the *total* commit count, so it advances on every commit — multiple
    releases on the same day get distinct, increasing versions (``2026.6.24`` → ``2026.6.25``)
    and never collide. Using the date itself as the patch would collide on same-day commits;
    using distance-since-tag would regress after a tag — the total count avoids both.
    """
    when = version.node_date or datetime.date.today()
    build = _commit_count()
    if build is None:  # git unavailable: fall back to scm distance (still monotonic per tag)
        build = version.distance or 0
    return f"{when.year}.{when.month}.{build}"


# Guarded so the scheme functions above can be imported (and unit-tested) without invoking
# setup(); build backends execute setup.py as the __main__ script, so the real build still runs.
if __name__ == "__main__":
    setup(use_scm_version={"version_scheme": calver, "local_scheme": "no-local-version"})
