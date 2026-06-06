"""Opt-in release-version check for the ``bounds upgrade-check`` command.

This is a convenience helper that asks the GitHub Releases API for the latest
published ``bounds`` release and compares it against the version installed
locally, so a user (or an agent) can tell at a glance whether they are running a
stale CLI.

It is deliberately **off the structural path**. Unlike ``extract/``,
``validate/``, ``cache/``, and ``manifest/`` — which are pure, deterministic, and
must never touch the network — this module makes a single outbound HTTP request.
Nothing here is imported or invoked by any structural command; it is reached only
through the explicit ``bounds upgrade-check`` entry point. Keep it that way: do
not import this module from the validation, extraction, cache, or describe paths.

The check fails soft in every degraded case (no network, timeout, no release
published, an unparseable response). It never raises to the caller and never
changes an exit code — being outdated is informational, not an error.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .. import __version__
from ..shared import config

# GitHub Releases API for the canonical repository. The "latest" endpoint returns
# the most recent non-draft, non-prerelease release.
_RELEASES_LATEST_URL = "https://api.github.com/repos/Farzin312/bounds/releases/latest"

# Keep the request short: this runs interactively, and a slow or unreachable
# network must not hang the CLI. On timeout we simply report that we couldn't check.
_TIMEOUT_SECONDS = 3.0

# A User-Agent is required by the GitHub API; requests without one are rejected.
_USER_AGENT = "bounds-upgrade-check"

# The documented refresh path for git/pipx installs. Surfaced in `fix` so the
# message is actionable regardless of which degraded branch we land in. Single-sourced
# from config so the agent contract and this command never disagree.
_FIX = config.UPGRADE_INSTALL_CMD


# Sentinel distinguishing "the API answered, but no release is published" from
# "we never got an answer" (offline/timeout). The two render differently, so the
# fetch seam reports which one happened instead of collapsing both to None.
NO_RELEASE = "no-release"


def _fetch_latest_tag(url: str = _RELEASES_LATEST_URL) -> str | None:
    """Return the latest release tag from the GitHub API, with a three-way result.

    Returns one of:

    - a version string (a real ``tag_name`` was found),
    - :data:`NO_RELEASE` (the API answered, but there is no usable published
      release — e.g. a 404 because none has been cut yet), or
    - ``None`` (the lookup never succeeded: offline, DNS failure, or timeout).

    Isolated behind one seam so tests can monkeypatch the network entirely and never
    touch the real API. Fails soft in every branch — never raises.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        # The server answered with an HTTP error. A 404 specifically means the repo
        # has no published release yet — distinct from being unreachable.
        return NO_RELEASE if err.code == 404 else None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        # URLError (non-HTTP, e.g. DNS/connection refused), socket timeouts, and a
        # non-JSON body all mean "couldn't reach the API" — never a crash.
        return None
    tag = payload.get("tag_name") if isinstance(payload, dict) else None
    if not isinstance(tag, str) or not tag.strip():
        # A 200 with no usable tag is effectively "no release published".
        return NO_RELEASE
    return _normalize_tag(tag)


def _normalize_tag(tag: str) -> str:
    """Strip a leading ``v`` from a release tag (``v0.3.1`` -> ``0.3.1``)."""
    tag = tag.strip()
    return tag[1:] if tag[:1] in ("v", "V") else tag


def _is_dev_build(current: str) -> bool:
    """True when the installed version is a development build, not a tagged release.

    A dev build (e.g. ``0.1.dev18+gad1b4cc7e`` from setuptools-scm on a git/pipx
    install) cannot be meaningfully ordered against a released version, so we report
    it separately rather than guessing whether it is ahead or behind.

    Prefers ``packaging.version`` for an accurate verdict, but degrades to a simple
    substring heuristic if ``packaging`` is unavailable so the command still works.
    """
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:
        return "dev" in current or "+g" in current
    try:
        parsed = Version(current)
    except InvalidVersion:
        # An unparseable version string is, for our purposes, not a clean release.
        return "dev" in current or "+g" in current
    return parsed.is_devrelease or parsed.local is not None


def _compare(current: str, latest: str) -> bool:
    """Return True when ``current`` is strictly older than ``latest``.

    Uses ``packaging.version`` for correct semantic ordering when available; falls
    back to plain string inequality otherwise (a coarse but safe signal: equal
    strings are never reported as outdated).
    """
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:
        return current != latest
    try:
        return Version(current) < Version(latest)
    except InvalidVersion:
        return current != latest


def check(fetch=None) -> dict:
    """Compare the installed version against the latest published release.

    ``fetch`` is the network seam; tests pass a stub so the real network is never
    touched. When ``None`` (the CLI's call), the seam is resolved at call time as
    :func:`_fetch_latest_tag` — looked up on the module rather than bound as a default
    arg, so monkeypatching ``update_check._fetch_latest_tag`` takes effect.

    Returns a JSON-serializable dict with a stable, additive shape::

        {
            "current": <str>,            # installed version (bounds.__version__)
            "latest": <str|null>,        # latest released version, or null if unknown
            "status": <str>,             # one machine verdict (see below) — switch on this
            "needs_upgrade": <bool>,     # True only when a newer release supersedes current
            "outdated": <bool|null>,     # True if current < latest; null if undecidable
            "is_dev_build": <bool>,      # True for a git/pipx dev build
            "fix": <str>,                # how to refresh a git install
            "checked": <bool>,           # False if the network lookup didn't succeed
            "note": <str>,               # human-readable explanation of the result
        }

    ``status`` is the single field an agent should branch on; the rest stay for
    backward compatibility and human messaging. It is exactly one of:

    - ``"up_to_date"``  — a release exists and the installed version is current
    - ``"outdated"``    — a newer release supersedes the installed version (``needs_upgrade``)
    - ``"dev_build"``   — a git/pipx build with no stable ordering against the release
    - ``"no_release"``  — the API answered but no release has been published yet
    - ``"unreachable"`` — the lookup never succeeded (offline / DNS / timeout)

    Degraded cases all fail soft (``checked``/``latest``/``outdated`` reflect what we
    could and couldn't determine); this function never raises.
    """
    if fetch is None:
        fetch = _fetch_latest_tag
    current = __version__
    is_dev = _is_dev_build(current)

    result: dict = {
        "current": current,
        "latest": None,
        "status": "unreachable",
        "needs_upgrade": False,
        "outdated": None,
        "is_dev_build": is_dev,
        "fix": _FIX,
        "checked": False,
        "note": "",
    }

    latest = fetch()

    if latest is None:
        # The lookup never succeeded (offline, DNS failure, or timeout): checked stays
        # False and latest stays null, since we genuinely don't know.
        result["status"] = "unreachable"
        result["note"] = "couldn't reach the GitHub Releases API (offline or timed out); try again later"
        return result

    if latest == NO_RELEASE:
        # The API answered, so the lookup itself succeeded — there simply isn't a
        # release to compare against yet. latest stays null; outdated is undecidable.
        result["checked"] = True
        result["status"] = "no_release"
        result["note"] = "no published release yet to compare against"
        return result

    # The lookup succeeded and returned a real release tag.
    result["latest"] = latest
    result["checked"] = True

    if is_dev:
        # A dev build has no stable ordering against a release, so leave outdated=null
        # and point the user at the refresh command for git/pipx installs.
        result["status"] = "dev_build"
        result["note"] = (
            f"running a development build ({current}); the latest release is {latest}. "
            f"git/pipx installs can refresh with: {_FIX}"
        )
        return result

    outdated = _compare(current, latest)
    result["outdated"] = outdated
    if outdated:
        result["status"] = "outdated"
        result["needs_upgrade"] = True
        result["note"] = f"a newer release ({latest}) is available; run: {_FIX}"
    else:
        result["status"] = "up_to_date"
        result["note"] = f"bounds {current} is up to date"
    return result
