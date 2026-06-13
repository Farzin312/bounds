"""Governance layer: per-code severity policy, --fail-on, per-finding suppression, cycle baseline.

These features let a repo hard-gate the findings it can fix while demoting/accepting known
library-gap classes, instead of disabling the whole gate with a blanket allow_failure.
"""

from __future__ import annotations

import json

from bounds.shared import errors
from bounds.shared.models import Consumes, Issue, RootManifest, SubsystemCompact as Sub
from bounds.shared.policy import Policy, SuppressRule, load_policy
from bounds.core.validate.checks import check_cycles, current_cycle_keys, cycle_key
from bounds.core.validate import engine
from _validate_helpers import _ctx


# ---------------------------------------------------------------------------
# Policy module (unit)
# ---------------------------------------------------------------------------
def _iss(code, severity="error", message="m", subsystem=None):
    return Issue(code, severity, message, subsystem=subsystem)


def test_policy_severity_override_regrades_without_silencing():
    pol = Policy(severity={errors.E_CYCLE_DETECTED: "warning"})
    out = pol.apply([_iss(errors.E_CYCLE_DETECTED)])
    assert out[0].severity == "warning"
    assert out[0].suppressed is False  # still reported, just non-blocking


def test_policy_suppress_requires_match_and_records_reason():
    rule = SuppressRule(code=errors.E_CYCLE_DETECTED, reason="legacy", owner="me", subsystem="rankers")
    pol = Policy(suppress=(rule,))
    matched = pol.apply([_iss(errors.E_CYCLE_DETECTED, subsystem="rankers")])[0]
    assert matched.suppressed and matched.severity == "info"
    assert "legacy" in matched.note and "owner: me" in matched.note
    # A different subsystem is NOT suppressed by a subsystem-scoped rule.
    other = pol.apply([_iss(errors.E_CYCLE_DETECTED, subsystem="auth")])[0]
    assert other.suppressed is False


def test_policy_suppression_wins_over_severity_override():
    pol = Policy(
        severity={errors.E_ORPHAN_EXPORT: "error"},
        suppress=(SuppressRule(code=errors.E_ORPHAN_EXPORT, reason="external API"),),
    )
    out = pol.apply([_iss(errors.E_ORPHAN_EXPORT, severity="warning")])[0]
    assert out.suppressed and out.severity == "info"


def test_load_policy_absent_is_empty(py_project):
    pol = load_policy(py_project)
    assert pol.is_empty() and pol.issues == []


def test_load_policy_parses_all_sections(py_project):
    (py_project / ".bounds" / "policy.yaml").write_text(
        "version: 1\n"
        "severity:\n  E_ORPHAN_EXPORT: warning\n"
        "fail_on:\n  - E_BOUNDARY_VIOLATION\n"
        "suppress:\n  - code: E_CYCLE_DETECTED\n    reason: accepted\n    owner: farzin\n",
        encoding="utf-8",
    )
    pol = load_policy(py_project)
    assert pol.severity == {errors.E_ORPHAN_EXPORT: "warning"}
    assert pol.fail_on == frozenset({errors.E_BOUNDARY_VIOLATION})
    assert len(pol.suppress) == 1 and pol.suppress[0].reason == "accepted"
    assert pol.issues == []


def test_load_policy_malformed_degrades_to_warnings(py_project):
    (py_project / ".bounds" / "policy.yaml").write_text(
        "version: 1\n"
        "severity:\n  E_CYCLE_DETECTED: explode\n"   # invalid severity value
        "suppress:\n  - code: E_CYCLE_DETECTED\n",     # missing reason
        encoding="utf-8",
    )
    pol = load_policy(py_project)
    assert pol.severity == {}            # bad override dropped
    assert pol.suppress == ()            # reason-less rule dropped
    assert pol.issues                    # but the problems are surfaced as warnings
    assert all(i.severity == "warning" for i in pol.issues)


def test_load_policy_unknown_code_is_ignored_with_warning(py_project):
    (py_project / ".bounds" / "policy.yaml").write_text(
        "version: 1\nfail_on:\n  - E_NOT_A_REAL_CODE\n", encoding="utf-8")
    pol = load_policy(py_project)
    assert pol.fail_on == frozenset()
    assert any("unknown code" in i.message for i in pol.issues)


# ---------------------------------------------------------------------------
# Cycle baseline (check_cycles + dump)
# ---------------------------------------------------------------------------
def _cyclic_subs():
    return {
        "a": Sub(name="a", paths=["src/a"], consumes=[Consumes("b")]),
        "b": Sub(name="b", paths=["src/b"], consumes=[Consumes("a")]),
    }


def test_cycle_key_is_canonical_rotation():
    assert cycle_key(["b", "a", "c"]) == cycle_key(["a", "c", "b"])  # rotation-invariant


def test_current_cycle_keys_matches_reported_cycle():
    subs = _cyclic_subs()
    keys = current_cycle_keys(subs)
    assert keys == [cycle_key(["a", "b"])]


def test_baselined_cycle_is_suppressed_new_cycle_is_not():
    subs = _cyclic_subs()
    baseline = set(current_cycle_keys(subs))
    issues = check_cycles(_ctx(subs, cycle_baseline=baseline))
    cyc = [i for i in issues if i.code == errors.E_CYCLE_DETECTED]
    # The one known cycle is reported but suppressed (non-blocking).
    assert cyc and all(i.suppressed for i in cyc)
    assert all(i.severity == "info" for i in cyc)


def test_new_cycle_above_baseline_still_blocks():
    subs = _cyclic_subs()
    # Baseline only knows about a DIFFERENT cycle, so a->b->a is new and must be reported as error.
    issues = check_cycles(_ctx(subs, cycle_baseline={cycle_key(["x", "y"])}))
    cyc = [i for i in issues if i.code == errors.E_CYCLE_DETECTED and not i.suppressed]
    assert cyc and any(i.severity == "error" for i in cyc)


# ---------------------------------------------------------------------------
# Engine end-to-end: --fail-on and baseline through the real gate
# ---------------------------------------------------------------------------
def _write_cyclic_python_repo(root):
    (root / "src" / "a").mkdir(parents=True, exist_ok=True)
    (root / "src" / "b").mkdir(parents=True, exist_ok=True)
    (root / "src" / "a" / "mod.py").write_text("from src.b.mod import bee\ndef ay():\n    return bee()\n")
    (root / "src" / "b" / "mod.py").write_text("from src.a.mod import ay\ndef bee():\n    return 1\n")
    b = root / ".bounds"
    (b / "manifests").mkdir(parents=True, exist_ok=True)
    (b / "root.yaml").write_text("version: '1'\nproject: t\nenforce: 'on'\nsubsystems: [a, b]\n")
    (b / "manifests" / "a.yaml").write_text(
        "name: a\nrole: library\npaths: [src/a]\nconsumes: [{subsystem: b}]\nexposes: [{name: ay}]\n")
    (b / "manifests" / "b.yaml").write_text(
        "name: b\nrole: library\npaths: [src/b]\nconsumes: [{subsystem: a}]\nexposes: [{name: bee}]\n")


def test_engine_cycle_blocks_then_baseline_clears_then_new_fails(tmp_path):
    _write_cyclic_python_repo(tmp_path)
    assert engine.run(tmp_path, mode="preflight").ok is False  # cycle blocks
    # Write the cycle baseline and confirm the gate now passes (fails only on NEW cycles).
    (tmp_path / ".bounds" / "cycle-baseline.json").write_text(
        json.dumps({"version": 1, "cycles": [cycle_key(["a", "b"])]}), encoding="utf-8")
    assert engine.run(tmp_path, mode="preflight").ok is True


def test_engine_fail_on_overrides_warn_enforce(tmp_path):
    _write_cyclic_python_repo(tmp_path)
    # enforce=warn would let everything pass, but --fail-on forces the cycle code to block.
    assert engine.run(tmp_path, mode="preflight", enforce="warn").ok is True
    assert engine.run(tmp_path, mode="preflight", enforce="warn",
                      fail_on=("E_CYCLE_DETECTED",)).ok is False
