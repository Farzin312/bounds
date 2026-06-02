"""Dynamic CalVer version scheme (setup.py:calver).

The version is ``YYYY.M.<build>`` where build is the repo's TOTAL commit count. These tests
pin the properties that keep it safe: clean numbers (no dev/local), strictly increasing on
every commit (so multiple same-day releases never collide), monotonic across months/years,
resilient to release tags (no regression), and a graceful fallback when git is unavailable.
"""

from __future__ import annotations

import datetime
import importlib.util
from pathlib import Path

from packaging.version import Version

# Load setup.py as a module WITHOUT triggering setup() (it's guarded by __name__=="__main__",
# and we load it under a different module name). Find it by walking up to the repo root so this
# test keeps working regardless of how deep under tests/ it lives.
_SETUP_PATH = next(
    p / "setup.py" for p in Path(__file__).resolve().parents if (p / "setup.py").is_file()
)
_spec = importlib.util.spec_from_file_location("bounds_setup", _SETUP_PATH)
setup_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup_mod)


class _FakeScmVersion:
    """Minimal stand-in for setuptools-scm's ScmVersion (only the fields calver reads)."""

    def __init__(self, node_date: datetime.date | None, distance: int = 0):
        self.node_date = node_date
        self.distance = distance


def _calver(node_date, distance=0, *, commit_count):
    """Run calver with a fixed commit_count (bypassing the real git call)."""
    import unittest.mock as mock

    with mock.patch.object(setup_mod, "_commit_count", return_value=commit_count):
        return setup_mod.calver(_FakeScmVersion(node_date, distance))


# ---------------------------------------------------------------------------
# Shape: clean, year-first, numeric — no dev/post/local letters
# ---------------------------------------------------------------------------
def test_calver_is_clean_year_first_number():
    v = _calver(datetime.date(2026, 6, 24), commit_count=24)
    assert v == "2026.6.24"
    # A valid PEP 440 release: no dev/pre/post/local segments, so no "letters".
    parsed = Version(v)
    assert not parsed.is_devrelease and not parsed.is_prerelease and not parsed.is_postrelease
    assert parsed.local is None
    assert not any(c.isalpha() for c in v)


# ---------------------------------------------------------------------------
# THE same-day requirement: multiple updates on one day must not collide
# ---------------------------------------------------------------------------
def test_same_day_multiple_commits_do_not_collide():
    day = datetime.date(2026, 6, 24)
    v1 = _calver(day, commit_count=24)
    v2 = _calver(day, commit_count=25)  # a second commit/update the same day
    v3 = _calver(day, commit_count=26)
    assert v1 != v2 != v3
    # ...and they order strictly increasing, so `pipx upgrade` always sees a newer version.
    assert Version(v1) < Version(v2) < Version(v3)


# ---------------------------------------------------------------------------
# Monotonic across month and year rollovers (build count never resets)
# ---------------------------------------------------------------------------
def test_monotonic_across_month_and_year():
    june = _calver(datetime.date(2026, 6, 30), commit_count=100)
    july = _calver(datetime.date(2026, 7, 1), commit_count=101)
    dec = _calver(datetime.date(2026, 12, 31), commit_count=400)
    jan = _calver(datetime.date(2027, 1, 1), commit_count=401)
    assert Version(june) < Version(july) < Version(dec) < Version(jan)


# ---------------------------------------------------------------------------
# Resilient to tags: total count never resets, so no version regression after tagging
# ---------------------------------------------------------------------------
def test_no_regression_after_a_tag():
    # Before a tag: distance large (e.g. 50). After a tag the scm distance resets to 1, but
    # because calver uses TOTAL commit count (not distance), the build keeps climbing.
    before = _calver(datetime.date(2026, 6, 24), distance=50, commit_count=50)
    after_tag = _calver(datetime.date(2026, 6, 24), distance=1, commit_count=51)
    assert Version(after_tag) > Version(before)  # would FAIL if we used distance for the patch


# ---------------------------------------------------------------------------
# Fallback when git is unavailable (sdist without .git is baked at build, but be safe)
# ---------------------------------------------------------------------------
def test_commit_count_returns_none_when_git_missing(monkeypatch):
    def boom(*a, **k):
        raise OSError("git not found")

    monkeypatch.setattr(setup_mod.subprocess, "run", boom)
    assert setup_mod._commit_count() is None


def test_calver_falls_back_to_distance_without_git(monkeypatch):
    monkeypatch.setattr(setup_mod, "_commit_count", lambda: None)
    v = setup_mod.calver(_FakeScmVersion(datetime.date(2026, 6, 24), distance=7))
    assert v == "2026.6.7"  # distance used as the build number when git is unavailable


def test_calver_without_node_date_uses_a_date(monkeypatch):
    # No commit date available: must still produce a well-formed YYYY.M.N, never crash.
    monkeypatch.setattr(setup_mod, "_commit_count", lambda: 5)
    v = setup_mod.calver(_FakeScmVersion(None, distance=0))
    parts = v.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)
    assert parts[2] == "5"


# ---------------------------------------------------------------------------
# The actually-installed version reflects the scheme (no dev/local in real builds)
# ---------------------------------------------------------------------------
def test_installed_version_is_clean_calver_or_unknown():
    from bounds import __version__

    if __version__ == "unknown":
        return  # not installed as a package (e.g. raw source run) — nothing to assert
    parsed = Version(__version__)
    assert not parsed.is_devrelease and parsed.local is None  # clean release number
    assert parsed.release[0] >= 2026  # year-first CalVer
