"""Tests for the opt-in release check (bounds.update_check + `bounds upgrade-check`).

The real network is never touched here: every test either passes a stub ``fetch`` into
:func:`update_check.check`, or monkeypatches ``urllib.request.urlopen`` at the seam. One
test additionally proves that structural commands make no network call at all.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from bounds.shared import config
from bounds.maintenance import update as update_check, upgrade
from bounds import cli as cli_mod
from bounds.cli import main

# The exact, stable JSON keys every `check()` result must carry (additive contract).
_EXPECTED_KEYS = {
    "current", "latest", "status", "needs_upgrade", "outdated",
    "is_dev_build", "fix", "checked", "note",
}

# Single-sourced from config (the same constant the command surfaces), so the test can't drift.
_FIX = config.UPGRADE_INSTALL_CMD


def _stub_version(monkeypatch, value: str) -> None:
    """Pin the locally-installed version that update_check reads."""
    monkeypatch.setattr(update_check, "__version__", value)


# ---------------------------------------------------------------------------
# check(): the five branches + shape
# ---------------------------------------------------------------------------
def test_shape_is_stable_and_additive(monkeypatch):
    """check() always returns the exact stable key set (additive JSON contract) with fix single-sourced from config, so consumers never see drift."""
    _stub_version(monkeypatch, "0.1.0")
    result = update_check.check(fetch=lambda: "0.1.0")
    assert set(result) == _EXPECTED_KEYS
    assert result["fix"] == _FIX


def test_outdated_when_current_behind_latest(monkeypatch):
    """When the local version is below latest, check() reports status=outdated/needs_upgrade=True and surfaces the upgrade fix in the note."""
    _stub_version(monkeypatch, "0.1.0")
    result = update_check.check(fetch=lambda: "0.2.0")
    assert result["current"] == "0.1.0"
    assert result["latest"] == "0.2.0"
    assert result["outdated"] is True
    assert result["status"] == "outdated"
    assert result["needs_upgrade"] is True
    assert result["is_dev_build"] is False
    assert result["checked"] is True
    assert _FIX in result["note"]


def test_up_to_date_when_equal(monkeypatch):
    """Equal local and latest versions report status=up_to_date with needs_upgrade=False — no spurious upgrade nag when already current."""
    _stub_version(monkeypatch, "0.2.0")
    result = update_check.check(fetch=lambda: "0.2.0")
    assert result["latest"] == "0.2.0"
    assert result["outdated"] is False
    assert result["status"] == "up_to_date"
    assert result["needs_upgrade"] is False
    assert result["is_dev_build"] is False
    assert result["checked"] is True
    assert "up to date" in result["note"]


def test_not_outdated_when_current_ahead(monkeypatch):
    """A local version ahead of the published latest must not be flagged outdated — a maintainer's newer build never gets a bogus downgrade nag."""
    # A locally-built release newer than the published one must not report outdated.
    _stub_version(monkeypatch, "0.3.0")
    result = update_check.check(fetch=lambda: "0.2.0")
    assert result["outdated"] is False
    assert result["checked"] is True


def test_dev_build_leaves_outdated_null(monkeypatch):
    """A setuptools-scm dev version can't be ordered against a release, so outdated stays None (status=dev_build) instead of guessing."""
    # setuptools-scm dev version on a git/pipx install: cannot be ordered vs a release.
    _stub_version(monkeypatch, "0.1.dev18+gad1b4cc7e")
    result = update_check.check(fetch=lambda: "0.2.0")
    assert result["is_dev_build"] is True
    assert result["outdated"] is None
    assert result["status"] == "dev_build"
    assert result["needs_upgrade"] is False
    assert result["latest"] == "0.2.0"
    assert result["checked"] is True
    assert _FIX in result["note"]


def test_no_release_published(monkeypatch):
    """A NO_RELEASE sentinel (API answered but nothing to compare) yields latest=None/status=no_release, distinct from an unreachable network."""
    # The API answered but there is no release to compare against (e.g. a 404).
    _stub_version(monkeypatch, "0.1.0")
    result = update_check.check(fetch=lambda: update_check.NO_RELEASE)
    assert result["latest"] is None
    assert result["outdated"] is None
    assert result["status"] == "no_release"
    assert result["needs_upgrade"] is False
    assert result["checked"] is True
    assert "no published release" in result["note"]


def test_offline_or_timeout_fails_soft(monkeypatch):
    """A failed fetch (None) fails soft: checked=False, status=unreachable, no crash — being offline is never an error (fail-soft)."""
    # The lookup never succeeded: checked=False, latest=None, no crash.
    _stub_version(monkeypatch, "0.1.0")
    result = update_check.check(fetch=lambda: None)
    assert result["latest"] is None
    assert result["outdated"] is None
    assert result["status"] == "unreachable"
    assert result["needs_upgrade"] is False
    assert result["checked"] is False
    assert "couldn't reach" in result["note"].lower() or "offline" in result["note"].lower()


def test_dev_build_offline_still_reports_dev(monkeypatch):
    """A dev build is detected from the local version alone, so is_dev_build=True even when offline (checked=False) — no network needed to classify it."""
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
    """_fetch_latest_tag parses tag_name and strips the leading 'v' so 'v1.4.2' compares as the bare release '1.4.2'."""
    monkeypatch.setattr(
        update_check.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse(json.dumps({"tag_name": "v1.4.2"})),
    )
    assert update_check._fetch_latest_tag() == "1.4.2"


def test_fetch_sends_user_agent(monkeypatch):
    """Every request carries a User-Agent (GitHub rejects requests without one) and a finite timeout, so the check can't hang or be 403'd."""
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
    """A 404 (no release yet) maps to the NO_RELEASE sentinel, distinguishing 'no release exists' from a connection failure (None)."""
    def fake_urlopen(req, timeout=None):
        raise update_check.urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(update_check.urllib.request, "urlopen", fake_urlopen)
    assert update_check._fetch_latest_tag() == update_check.NO_RELEASE


def test_fetch_connection_error_returns_none(monkeypatch):
    """A URLError (no route/DNS failure) returns None so the caller fails soft as unreachable rather than raising."""
    def fake_urlopen(req, timeout=None):
        raise update_check.urllib.error.URLError("no route to host")

    monkeypatch.setattr(update_check.urllib.request, "urlopen", fake_urlopen)
    assert update_check._fetch_latest_tag() is None


def test_fetch_timeout_returns_none(monkeypatch):
    """A TimeoutError returns None (fail soft), so a slow GitHub never makes the check hang or crash."""
    def fake_urlopen(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(update_check.urllib.request, "urlopen", fake_urlopen)
    assert update_check._fetch_latest_tag() is None


def test_fetch_bad_json_returns_none(monkeypatch):
    """A non-JSON body (e.g. an HTML error page) returns None rather than raising — malformed responses fail soft."""
    monkeypatch.setattr(
        update_check.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse("<html>not json</html>"),
    )
    assert update_check._fetch_latest_tag() is None


def test_fetch_missing_tag_reports_no_release(monkeypatch):
    """Valid JSON without a tag_name key maps to NO_RELEASE (release exists but unnamed), not None — distinct from a transport failure."""
    monkeypatch.setattr(
        update_check.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse(json.dumps({"message": "no tag here"})),
    )
    assert update_check._fetch_latest_tag() == update_check.NO_RELEASE


# ---------------------------------------------------------------------------
# CLI wiring: JSON + human, always exit 0
# ---------------------------------------------------------------------------
class _FakeStream:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_interactive_human_defaults_to_tty(monkeypatch):
    """_interactive_human defaults to JSON when stdout is piped (agent/script) and human only on a real TTY, with --human always overriding — protects the JSON-first contract."""
    # Piped/redirected (agent or script) → JSON contract; a real terminal → human announcement.
    monkeypatch.setattr(cli_mod.sys, "stdout", _FakeStream(tty=False))
    assert cli_mod._interactive_human(False) is False     # non-TTY → JSON
    assert cli_mod._interactive_human(True) is True        # --human always wins
    monkeypatch.setattr(cli_mod.sys, "stdout", _FakeStream(tty=True))
    assert cli_mod._interactive_human(False) is True       # interactive terminal → human


def test_cli_json_outdated(monkeypatch):
    """`bounds upgrade-check` emits one JSON object carrying the full stable key set with outdated=True — the default machine contract for agents."""
    _stub_version(monkeypatch, "0.1.0")
    monkeypatch.setattr(update_check, "_fetch_latest_tag", lambda: "0.2.0")
    res = CliRunner().invoke(main, ["upgrade-check"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert set(data) == _EXPECTED_KEYS
    assert data["outdated"] is True
    assert data["latest"] == "0.2.0"


def test_cli_human_outdated_line(monkeypatch):
    """--human renders a short summary (newer-release line + fix) and emits no JSON braces — the human view re-renders the same data, never raw JSON."""
    _stub_version(monkeypatch, "0.1.0")
    monkeypatch.setattr(update_check, "_fetch_latest_tag", lambda: "0.2.0")
    res = CliRunner().invoke(main, ["upgrade-check", "--human"])
    assert res.exit_code == 0
    assert "newer release" in res.output
    assert _FIX in res.output
    # Human view is a short summary, not the full JSON dump.
    assert "{" not in res.output


def test_cli_human_up_to_date_line(monkeypatch):
    """The -H short flag for --human prints the 'up to date' summary line when local equals latest — both human flag spellings are wired."""
    _stub_version(monkeypatch, "0.2.0")
    monkeypatch.setattr(update_check, "_fetch_latest_tag", lambda: "0.2.0")
    res = CliRunner().invoke(main, ["upgrade-check", "-H"])
    assert res.exit_code == 0
    assert "up to date" in res.output


def test_cli_offline_exits_zero(monkeypatch):
    """`bounds upgrade-check` exits 0 with checked=False when offline — being unable to check is informational, never a failure exit code."""
    # Being unable to check is informational, never an error.
    _stub_version(monkeypatch, "0.1.0")
    monkeypatch.setattr(update_check, "_fetch_latest_tag", lambda: None)
    res = CliRunner().invoke(main, ["upgrade-check"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["checked"] is False


# ---------------------------------------------------------------------------
# bounds upgrade: opt-in self-upgrade command, no real subprocess in tests
# ---------------------------------------------------------------------------
def test_upgrade_dry_run_reports_command():
    """A dry-run reports the exact pipx install --force git+... command without executing it, so users can preview the self-upgrade."""
    result = upgrade.run_upgrade(dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["command"] == ["pipx", "install", "--force", "git+https://github.com/Farzin312/bounds.git"]


def test_upgrade_local_command_uses_editable_path(tmp_path):
    """A local upgrade builds an editable install (pipx ... -e <path>) with source='local', so contributors upgrade from a working tree."""
    result = upgrade.run_upgrade(local=tmp_path, dry_run=True)
    assert result["source"] == "local"
    assert result["command"] == ["pipx", "install", "--force", "-e", str(tmp_path)]


def test_upgrade_fallback_reinstalls_when_force_fails(monkeypatch):
    """When pipx install --force fails (existing venv), the upgrade falls back to uninstall+reinstall in order and still reports ok — recovers a wedged venv."""
    calls = []

    def fake_run(command, capture_output=True, text=True, check=False, timeout=None):
        calls.append(command)
        if "--force" in command:
            return upgrade.subprocess.CompletedProcess(command, 1, "", "venv exists")
        return upgrade.subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    result = upgrade.run_upgrade()
    assert result["ok"] is True
    assert calls == [
        ["pipx", "install", "--force", "git+https://github.com/Farzin312/bounds.git"],
        ["pipx", "uninstall", "bounds-cli"],
        ["pipx", "install", "git+https://github.com/Farzin312/bounds.git"],
    ]


def test_upgrade_success_captures_version_and_drops_noise(monkeypatch):
    """On success the report carries the installed version and omits stdout/fallback noise."""
    stdout = (
        "Installing to existing venv 'bounds-cli'\n"
        "  installed package bounds-cli 0.1.dev21+g31f72715d, installed using Python 3.14.5\n"
    )

    def fake_run(command, capture_output=True, text=True, check=False, timeout=None):
        return upgrade.subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    result = upgrade.run_upgrade()
    assert result["ok"] is True
    assert result["version"] == "0.1.dev21+g31f72715d"
    assert result["stderr"] == ""
    # Internal mechanics no longer leak into the consumer-facing JSON contract.
    assert "fallback_commands" not in result
    assert "stdout" not in result


def test_upgrade_times_out_soft_instead_of_hanging(monkeypatch):
    """A stuck pipx install fails soft (returncode 124, ok=False), never hangs."""

    def fake_run(command, capture_output=True, text=True, check=False, timeout=None):
        raise upgrade.subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    result = upgrade.run_upgrade()
    assert result["ok"] is False
    assert "timed out" in result["stderr"]


def test_upgrade_failure_surfaces_stderr_only(monkeypatch):
    """A failed upgrade reports stderr (for debugging) but no version."""

    def fake_run(command, capture_output=True, text=True, check=False, timeout=None):
        return upgrade.subprocess.CompletedProcess(command, 1, "", "boom")

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    result = upgrade.run_upgrade()
    assert result["ok"] is False
    assert result["version"] is None
    assert "boom" in result["stderr"]
    assert result["note"] == "upgrade failed"
    assert result["error"] == "install_failed"  # semantic class, not a raw return code


def test_upgrade_error_class_maps_pipx_not_found(monkeypatch):
    """A missing pipx (OSError) surfaces as the stable semantic error='pipx_not_found' with fallback_used=True, so consumers never parse a raw return code."""
    # _run maps a missing pipx (OSError) to returncode 127; the JSON must carry the
    # stable semantic class so a consumer never interprets the raw number.
    def fake_run(command, capture_output=True, text=True, check=False, timeout=None):
        raise OSError("pipx: command not found")

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    result = upgrade.run_upgrade()
    assert result["ok"] is False
    assert result["error"] == "pipx_not_found"
    assert result["fallback_used"] is True


def test_upgrade_timeout_error_class(monkeypatch):
    """A TimeoutExpired during install reports the stable semantic error='timeout', so a hung pipx is classified, not surfaced as a raw code."""
    def fake_run(command, capture_output=True, text=True, check=False, timeout=None):
        raise upgrade.subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    result = upgrade.run_upgrade()
    assert result["error"] == "timeout"


def test_upgrade_cli_dry_run(monkeypatch):
    """`bounds upgrade --dry-run` exits 0 and emits JSON with dry_run=True and a pipx command — previewing never executes or errors."""
    res = CliRunner().invoke(main, ["upgrade", "--dry-run"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["dry_run"] is True
    assert data["command"][0] == "pipx"


def test_upgrade_cli_failure_exits_blocked(monkeypatch):
    """A failed upgrade (ok=False) makes the CLI exit 1, so a failed self-upgrade is a non-zero exit a caller can detect."""
    monkeypatch.setattr(
        upgrade,
        "run_upgrade",
        lambda **kwargs: {"ok": False, "source": "github", "dry_run": False, "command": ["pipx"], "note": "upgrade failed"},
    )
    res = CliRunner().invoke(main, ["upgrade"])
    assert res.exit_code == 1


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


def test_importing_bounds_makes_no_network_call():
    """Importing the package / structural modules must not open any network connection.

    Run the cold-import check in a subprocess. Deleting live ``bounds`` modules from this
    test process invalidates references collected by other tests and makes later monkeypatches
    target stale module objects.
    """
    import subprocess
    import sys

    code = """
import socket

def boom(*args, **kwargs):
    raise AssertionError("import path opened a network connection")

socket.socket.connect = boom
import bounds
import bounds.cli
import bounds.core.validate.engine
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
