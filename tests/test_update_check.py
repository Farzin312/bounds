"""Tests for the opt-in release check (bounds.update_check + `bounds upgrade-check`).

The real network is never touched here: every test either passes a stub ``fetch`` into
:func:`update_check.check`, or monkeypatches ``urllib.request.urlopen`` at the seam. One
test additionally proves that structural commands make no network call at all.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from bounds import config, update_check
from bounds.cli import main

# The exact, stable JSON keys every `check()` result must carry (additive contract).
_EXPECTED_KEYS = {"current", "latest", "outdated", "is_dev_build", "fix", "checked", "note"}

# Single-sourced from config (the same constant the command surfaces), so the test can't drift.
_FIX = config.UPGRADE_INSTALL_CMD


def _stub_version(monkeypatch, value: str) -> None:
    """Pin the locally-installed version that update_check reads."""
    monkeypatch.setattr(update_check, "__version__", value)


# ---------------------------------------------------------------------------
# check(): the five branches + shape
# ---------------------------------------------------------------------------
def test_shape_is_stable_and_additive(monkeypatch):
    _stub_version(monkeypatch, "0.1.0")
    result = update_check.check(fetch=lambda: "0.1.0")
    assert set(result) == _EXPECTED_KEYS
    assert result["fix"] == _FIX


def test_outdated_when_current_behind_latest(monkeypatch):
    _stub_version(monkeypatch, "0.1.0")
    result = update_check.check(fetch=lambda: "0.2.0")
    assert result["current"] == "0.1.0"
    assert result["latest"] == "0.2.0"
    assert result["outdated"] is True
    assert result["is_dev_build"] is False
    assert result["checked"] is True
    assert _FIX in result["note"]


def test_up_to_date_when_equal(monkeypatch):
    _stub_version(monkeypatch, "0.2.0")
    result = update_check.check(fetch=lambda: "0.2.0")
    assert result["latest"] == "0.2.0"
    assert result["outdated"] is False
    assert result["is_dev_build"] is False
    assert result["checked"] is True
    assert "up to date" in result["note"]


def test_not_outdated_when_current_ahead(monkeypatch):
    # A locally-built release newer than the published one must not report outdated.
    _stub_version(monkeypatch, "0.3.0")
    result = update_check.check(fetch=lambda: "0.2.0")
    assert result["outdated"] is False
    assert result["checked"] is True


def test_dev_build_leaves_outdated_null(monkeypatch):
    # setuptools-scm dev version on a git/pipx install: cannot be ordered vs a release.
    _stub_version(monkeypatch, "0.1.dev18+gad1b4cc7e")
    result = update_check.check(fetch=lambda: "0.2.0")
    assert result["is_dev_build"] is True
    assert result["outdated"] is None
    assert result["latest"] == "0.2.0"
    assert result["checked"] is True
    assert _FIX in result["note"]


def test_no_release_published(monkeypatch):
    # The API answered but there is no release to compare against (e.g. a 404).
    _stub_version(monkeypatch, "0.1.0")
    result = update_check.check(fetch=lambda: update_check.NO_RELEASE)
    assert result["latest"] is None
    assert result["outdated"] is None
    assert result["checked"] is True
    assert "no published release" in result["note"]


def test_offline_or_timeout_fails_soft(monkeypatch):
    # The lookup never succeeded: checked=False, latest=None, no crash.
    _stub_version(monkeypatch, "0.1.0")
    result = update_check.check(fetch=lambda: None)
    assert result["latest"] is None
    assert result["outdated"] is None
    assert result["checked"] is False
    assert "couldn't reach" in result["note"].lower() or "offline" in result["note"].lower()


def test_dev_build_offline_still_reports_dev(monkeypatch):
    # Even with no network, a dev build is detectable from the local version alone.
    _stub_version(monkeypatch, "0.1.dev1+gabc")
    result = update_check.check(fetch=lambda: None)
    assert result["is_dev_build"] is True
    assert result["checked"] is False
    assert result["outdated"] is None


# ---------------------------------------------------------------------------
# The network seam: fully mocked, never hits the real API
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_parses_tag_and_strips_v_prefix(monkeypatch):
    monkeypatch.setattr(
        update_check.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse(json.dumps({"tag_name": "v1.4.2"})),
    )
    assert update_check._fetch_latest_tag() == "1.4.2"


def test_fetch_sends_user_agent(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        seen["timeout"] = timeout
        return _FakeResponse(json.dumps({"tag_name": "0.9.0"}))

    monkeypatch.setattr(update_check.urllib.request, "urlopen", fake_urlopen)
    update_check._fetch_latest_tag()
    assert seen["ua"]  # a User-Agent is always sent (GitHub rejects requests without one)
    assert seen["timeout"] is not None  # a finite timeout is always passed


def test_fetch_404_reports_no_release(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise update_check.urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(update_check.urllib.request, "urlopen", fake_urlopen)
    assert update_check._fetch_latest_tag() == update_check.NO_RELEASE


def test_fetch_connection_error_returns_none(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise update_check.urllib.error.URLError("no route to host")

    monkeypatch.setattr(update_check.urllib.request, "urlopen", fake_urlopen)
    assert update_check._fetch_latest_tag() is None


def test_fetch_timeout_returns_none(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(update_check.urllib.request, "urlopen", fake_urlopen)
    assert update_check._fetch_latest_tag() is None


def test_fetch_bad_json_returns_none(monkeypatch):
    monkeypatch.setattr(
        update_check.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse("<html>not json</html>"),
    )
    assert update_check._fetch_latest_tag() is None


def test_fetch_missing_tag_reports_no_release(monkeypatch):
    monkeypatch.setattr(
        update_check.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse(json.dumps({"message": "no tag here"})),
    )
    assert update_check._fetch_latest_tag() == update_check.NO_RELEASE


# ---------------------------------------------------------------------------
# CLI wiring: JSON + human, always exit 0
# ---------------------------------------------------------------------------
def test_cli_json_outdated(monkeypatch):
    _stub_version(monkeypatch, "0.1.0")
    monkeypatch.setattr(update_check, "_fetch_latest_tag", lambda: "0.2.0")
    res = CliRunner().invoke(main, ["upgrade-check"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert set(data) == _EXPECTED_KEYS
    assert data["outdated"] is True
    assert data["latest"] == "0.2.0"


def test_cli_human_outdated_line(monkeypatch):
    _stub_version(monkeypatch, "0.1.0")
    monkeypatch.setattr(update_check, "_fetch_latest_tag", lambda: "0.2.0")
    res = CliRunner().invoke(main, ["upgrade-check", "--human"])
    assert res.exit_code == 0
    assert "newer release" in res.output
    assert _FIX in res.output
    # Human view is a short summary, not the full JSON dump.
    assert "{" not in res.output


def test_cli_human_up_to_date_line(monkeypatch):
    _stub_version(monkeypatch, "0.2.0")
    monkeypatch.setattr(update_check, "_fetch_latest_tag", lambda: "0.2.0")
    res = CliRunner().invoke(main, ["upgrade-check", "-H"])
    assert res.exit_code == 0
    assert "up to date" in res.output


def test_cli_offline_exits_zero(monkeypatch):
    # Being unable to check is informational, never an error.
    _stub_version(monkeypatch, "0.1.0")
    monkeypatch.setattr(update_check, "_fetch_latest_tag", lambda: None)
    res = CliRunner().invoke(main, ["upgrade-check"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["checked"] is False


# ---------------------------------------------------------------------------
# Isolation: structural commands must never make a network call
# ---------------------------------------------------------------------------
def test_structural_command_makes_no_network_call(monkeypatch, py_project):
    """`bounds validate` must not touch the network seam update_check uses."""
    import urllib.request

    def boom(*args, **kwargs):  # pragma: no cover - only runs on a regression
        raise AssertionError("structural command attempted a network request")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.chdir(py_project)
    res = CliRunner().invoke(main, ["validate"])
    # Exit code is 0 (clean) or 1 (blocking issues) — never a crash from a network call.
    assert res.exit_code in (0, 1)


def test_importing_bounds_makes_no_network_call(monkeypatch):
    """Importing the package / structural modules must not open any network connection.

    Guards at the socket layer (not just ``urllib``) so a future switch to a different transport
    can't slip a network call into the import path unnoticed, and drops the cached modules first
    so the imports genuinely re-execute their top-level code.
    """
    import socket
    import sys

    opened = []

    def boom(*args, **kwargs):  # pragma: no cover - only runs on a regression
        opened.append(args)
        raise AssertionError("import path opened a network connection")

    # The lowest common seam: any outbound socket (urllib, http.client, requests, …) lands here.
    monkeypatch.setattr(socket.socket, "connect", boom)

    # Drop cached bounds modules so the imports below truly re-run their module-level code.
    for name in [n for n in sys.modules if n == "bounds" or n.startswith("bounds.")]:
        del sys.modules[name]

    import bounds  # noqa: F401
    import bounds.cli  # noqa: F401
    import bounds.validate.engine  # noqa: F401

    assert opened == [], "no network connection should be opened during import"
