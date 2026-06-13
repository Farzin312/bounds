"""Optional governance policy (`.bounds/policy.yaml`): per-code severity, hard fail-on, suppressions.

The validation gate is otherwise all-or-nothing: any ``error`` fails ``preflight``, so a single
library-gap class (say the orphan-export false positives a DI framework produces) forces a blanket
``allow_failure: true`` on the whole CI job — which then also masks genuine regressions. This module
adds a thin, committed policy layer so a repo can:

* **re-grade a code** — ``severity: {E_ORPHAN_EXPORT: warning}`` demotes a noisy class without
  silencing it (it still prints, just stops blocking);
* **hard-gate specific codes** — ``fail_on: [E_BOUNDARY_VIOLATION]`` blocks those regardless of mode
  or ``enforce``, even in ``warn`` mode, so the findings a team *can* fix stay enforced;
* **suppress a specific known finding** — an ``eslint-disable`` equivalent: name the finding plus a
  ``reason``/``owner`` so an accepted exception is explicit and auditable, and the *next*
  unannotated finding of the same code still fails.

Design constraints honoured:
* **Determinism.** ``expires`` is parsed and surfaced for human/CI review but is NOT evaluated
  against wall-clock here — gating on "today" would make the same inputs produce different results
  on different days. Expiry is advisory metadata; a dated review process enforces it out-of-band.
* **Report hard.** Suppressed findings stay in the report (``suppressed: true`` + ``note``); the
  policy changes whether the gate *blocks*, never whether the user *sees* the finding.
* **Fail soft.** A malformed policy file degrades to warnings and an otherwise-empty policy — it
  never crashes the run or silently swallows the gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from . import config, errors
from .models import Issue

__all__ = ["Policy", "SuppressRule", "load_policy"]

_VALID_SEVERITIES = ("error", "warning", "info")


@dataclass(frozen=True)
class SuppressRule:
    """One accepted-finding rule. ``code`` is required; the optional match fields narrow it to a
    specific finding (omit them to accept every finding of that code). ``reason`` is mandatory so an
    exception is never anonymous."""

    code: str
    reason: str
    subsystem: str | None = None
    file: str | None = None
    message_contains: str | None = None
    owner: str | None = None
    expires: str | None = None  # advisory only — never gated against wall-clock (determinism)

    def matches(self, issue: Issue) -> bool:
        if issue.code != self.code:
            return False
        if self.subsystem is not None and issue.subsystem != self.subsystem:
            return False
        if self.file is not None and issue.file != self.file:
            return False
        if self.message_contains is not None and self.message_contains not in (issue.message or ""):
            return False
        return True

    def note(self) -> str:
        meta = []
        if self.owner:
            meta.append(f"owner: {self.owner}")
        if self.expires:
            meta.append(f"expires: {self.expires}")
        tail = f" ({'; '.join(meta)})" if meta else ""
        return f"suppressed by policy: {self.reason}{tail}"


@dataclass
class Policy:
    severity: dict[str, str] = field(default_factory=dict)
    fail_on: frozenset[str] = frozenset()
    suppress: tuple[SuppressRule, ...] = ()
    # Schema problems found while loading the policy itself, surfaced as (non-blocking) warnings so a
    # fat-fingered policy is visible without hard-failing the gate.
    issues: list[Issue] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.severity and not self.fail_on and not self.suppress

    def apply(self, issues: list[Issue]) -> list[Issue]:
        """Return a new issue list with suppressions and severity overrides applied.

        Suppression wins over a severity override (it is the more specific instruction): a matched
        finding becomes ``info`` + ``suppressed`` + an audit ``note``. Otherwise a code-level
        severity override re-grades the finding. Already-suppressed inputs are left untouched.
        """
        out: list[Issue] = []
        for iss in issues:
            if iss.suppressed:
                out.append(iss)
                continue
            rule = next((r for r in self.suppress if r.matches(iss)), None)
            if rule is not None:
                out.append(replace(iss, severity="info", suppressed=True, note=rule.note()))
                continue
            if iss.code in self.severity:
                out.append(replace(iss, severity=self.severity[iss.code]))
                continue
            out.append(iss)
        return out


def _policy_issue(message: str, fix: str) -> Issue:
    # A policy-file problem is reported as a non-blocking warning (fail soft): the gate still runs
    # with whatever parsed, and the misconfiguration is visible in the report.
    return Issue(errors.E_SCHEMA_INVALID, "warning", message, subsystem=None, file=config.POLICY_FILE, fix=fix)


def _empty() -> Policy:
    return Policy()


def load_policy(project_root: Path) -> Policy:
    """Load ``.bounds/policy.yaml`` if present. Absent ⇒ an empty (no-op) policy. Malformed ⇒ an
    empty policy carrying warning Issues that the engine folds into the report."""
    path = config.config_dir(project_root) / config.POLICY_FILE
    if not path.is_file():
        return _empty()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        p = _empty()
        p.issues.append(_policy_issue(f"could not parse {config.POLICY_FILE}: {exc}",
                                      "fix the YAML syntax in .bounds/policy.yaml"))
        return p
    if data is None:
        return _empty()
    if not isinstance(data, dict):
        p = _empty()
        p.issues.append(_policy_issue(f"{config.POLICY_FILE} must be a YAML mapping",
                                      "rewrite the policy as a top-level mapping (severity:/fail_on:/suppress:)"))
        return p

    known_codes = set(errors.SEVERITY)
    issues: list[Issue] = []

    # ---- severity overrides ----
    severity: dict[str, str] = {}
    raw_sev = data.get("severity")
    if isinstance(raw_sev, dict):
        for code, sev in raw_sev.items():
            code, sev = str(code), str(sev).lower()
            if sev not in _VALID_SEVERITIES:
                issues.append(_policy_issue(
                    f"severity override for '{code}' must be one of {list(_VALID_SEVERITIES)}, got {sev!r}",
                    "use error, warning, or info"))
                continue
            if code not in known_codes:
                issues.append(_policy_issue(
                    f"severity override names unknown code '{code}' (ignored)",
                    "use a code Bounds emits (see ARCHITECTURE.md / errors.py)"))
                continue
            severity[code] = sev
    elif raw_sev is not None:
        issues.append(_policy_issue("policy 'severity' must be a mapping of code -> severity",
                                    "e.g. `severity: { E_ORPHAN_EXPORT: warning }`"))

    # ---- fail_on ----
    fail_on: set[str] = set()
    raw_fail = data.get("fail_on")
    if isinstance(raw_fail, list):
        for code in raw_fail:
            code = str(code)
            if code not in known_codes:
                issues.append(_policy_issue(f"fail_on names unknown code '{code}' (ignored)",
                                            "use a code Bounds emits"))
                continue
            fail_on.add(code)
    elif raw_fail is not None:
        issues.append(_policy_issue("policy 'fail_on' must be a list of codes",
                                    "e.g. `fail_on: [E_BOUNDARY_VIOLATION]`"))

    # ---- suppress ----
    suppress: list[SuppressRule] = []
    raw_sup = data.get("suppress")
    if isinstance(raw_sup, list):
        for i, entry in enumerate(raw_sup):
            if not isinstance(entry, dict):
                issues.append(_policy_issue(f"suppress[{i}] must be a mapping",
                                            "each suppress entry needs at least `code` and `reason`"))
                continue
            code = str(entry.get("code", "")).strip()
            reason = str(entry.get("reason", "")).strip()
            if not code:
                issues.append(_policy_issue(f"suppress[{i}] is missing 'code'",
                                            "name the code to accept, e.g. `code: E_CYCLE_DETECTED`"))
                continue
            if code not in known_codes:
                issues.append(_policy_issue(f"suppress[{i}] names unknown code '{code}' (ignored)",
                                            "use a code Bounds emits"))
                continue
            if not reason:
                issues.append(_policy_issue(f"suppress[{i}] ({code}) is missing 'reason'",
                                            "every accepted finding needs a justification: add `reason:`"))
                continue
            suppress.append(SuppressRule(
                code=code,
                reason=reason,
                subsystem=(str(entry["subsystem"]) if entry.get("subsystem") is not None else None),
                file=(str(entry["file"]) if entry.get("file") is not None else None),
                message_contains=(str(entry["message_contains"]) if entry.get("message_contains") is not None else None),
                owner=(str(entry["owner"]) if entry.get("owner") is not None else None),
                expires=(str(entry["expires"]) if entry.get("expires") is not None else None),
            ))
    elif raw_sup is not None:
        issues.append(_policy_issue("policy 'suppress' must be a list of rules",
                                    "e.g. `suppress: [{ code: E_CYCLE_DETECTED, reason: '...' }]`"))

    return Policy(severity=severity, fail_on=frozenset(fail_on), suppress=tuple(suppress), issues=issues)
