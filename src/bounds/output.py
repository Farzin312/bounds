"""Output layer: JSON / human rendering and exit-code mapping.

Every command prints a JSON object to stdout by default; ``--human`` re-renders the
*same* data for people. Fatal errors are JSON too. All rendering is deterministic:
issues are emitted via ``ValidationReport.to_dict()`` (already sorted) and dict views
sort their keys so output is byte-stable across runs.
"""

from __future__ import annotations

import json
import sys

from .errors import BoundsError

__all__ = ["emit"]

# Severity groups, in the order they render and rank.
_SEVERITY_ORDER = ("error", "warning", "info")
_BULLETS = {"error": "✗", "warning": "⚠", "info": "ℹ"}
# Suffix tag for a symbol a subsystem declares in `exposes` (shared by the where renderers).
_EXPOSED_TAG = " [exposed]"


def emit(payload: dict, human: bool, stream=None, ci: bool = False) -> None:
    """Write ``payload`` to ``stream`` as JSON (default), a human view, or CI plaintext.

    JSON path preserves insertion order (``sort_keys=False``) since producers already
    build their dicts deterministically. The human path detects payload type by keys:
    a validation report (``validation_status`` + ``mode``), a namespace-filtered describe
    (``namespace`` + ``subsystems``), a subsystem compact (``name`` + ``role``), or a
    generic key/value listing. The ``ci`` path renders a report as one issue per line.
    """
    # Resolve sys.stdout at call time, not as a default arg: a default binds the stream at
    # import and ignores later redirection (CliRunner, contextlib.redirect_stdout), which
    # would silently drop all CLI output.
    if stream is None:
        stream = sys.stdout
    if ci:
        stream.write(_render_report_ci(payload))
        return
    if not human:
        json.dump(payload, stream, indent=2, sort_keys=False)
        stream.write("\n")
        return

    renderer = _select_human_renderer(payload) if isinstance(payload, dict) else None
    stream.write((renderer or _render_generic_human)(payload))
    stream.write("\n")


# Human-renderer dispatch table: (predicate, renderer), tried in order; first match wins.
# Table-driven so adding a command's view is one entry, not another branch in emit(). Each
# predicate is a cheap key/shape check on the payload dict (discriminators are disjoint).
def _has(*keys):
    return lambda p: set(keys) <= p.keys()


_HUMAN_RENDERERS = (
    (lambda p: "validation_status" in p and "mode" in p, lambda p: _render_report_dict_human(p)),
    (_has("symbol", "match", "results"), lambda p: _render_where_human(p)),
    (lambda p: p.get("query_kind") == "file" and "results" in p, lambda p: _render_where_file_human(p)),
    (_has("current", "checked", "outdated"), lambda p: _render_upgrade_check_human(p)),
    (_has("command", "dry_run", "source"), lambda p: _render_upgrade_human(p)),
    (lambda p: p.get("mode") == "guide", lambda p: _render_guide_human(p)),
    (lambda p: str(p.get("mode", "")).startswith("coverage"), lambda p: _render_coverage_human(p)),
    (_has("path", "status", "command"), lambda p: _render_coverage_human(p)),
    (lambda p: str(p.get("mode", "")).startswith("sdd-"), lambda p: _render_sdd_human(p)),
    (lambda p: p.get("mode") == "discover", lambda p: _render_discover_human(p)),
    (lambda p: p.get("mode") in ("calibrate-check", "calibrate-baseline"), lambda p: _render_drift_human(p)),
    (_has("health", "edges"), lambda p: _render_overview_human(p)),
    (lambda p: "blast_radius" in p, lambda p: _render_impact_human(p)),
    (lambda p: p.get("mode") == "calibrate", lambda p: _render_calibrate_human(p)),
    (lambda p: p.get("mode") == "edit", lambda p: _render_edit_human(p)),
    (lambda p: "project" in p and isinstance(p.get("subsystems"), list), lambda p: _render_list_human(p)),
    (lambda p: "bounds_dir" in p, lambda p: _render_init_human(p)),
    (_has("created", "targets"), lambda p: _render_ci_human(p)),
    (lambda p: "detected" in p or "canonical" in p or {"missing", "configured"} <= p.keys(),
     lambda p: _render_agent_human(p)),
    (lambda p: "backend" in p or "migrated" in p or "pruned" in p, lambda p: _render_cache_human(p)),
    (lambda p: "namespace" in p and "subsystems" in p, lambda p: _render_namespace_human(p)),
    (lambda p: "name" in p and "role" in p, lambda p: _render_subsystem_human(p)),
)


def _select_human_renderer(payload: dict):
    """Return the first renderer whose predicate matches ``payload``, or None for the generic dump."""
    for predicate, renderer in _HUMAN_RENDERERS:
        if predicate(payload):
            return renderer
    return None


def _render_report_ci(payload: dict) -> str:
    """Render a report as CI plaintext: one ``severity\tcode\tlocation\tmessage`` per line.

    Stable, tab-delimited, sorted by the report's own issue order (severity then code).
    Designed for ``grep``/``awk`` in CI logs: the bare error code is its own field, so
    ``grep E_BOUNDARY_VIOLATION`` matches cleanly. A clean report emits a single
    ``ok\t<status>`` line so a passing run is never indistinguishable from a crash.
    A fatal ``BoundsError`` payload (``{"error": {...}}``) emits one ``fatal`` line so a
    CI parser sees the failure code instead of a misleading ``ok``.
    """
    err = payload.get("error")
    if isinstance(err, dict):
        return f"fatal\t{err.get('code', '')}\t-\t{err.get('message', '')}\n"
    issues = payload.get("issues", []) or []
    if not issues:
        return f"ok\t{payload.get('validation_status', 'fresh')}\n"
    lines: list[str] = []
    for issue in issues:
        code = issue.get("code", "")
        sev = issue.get("severity", "")
        location = "/".join(part for part in (issue.get("subsystem"), issue.get("file")) if part) or "-"
        message = issue.get("message", "")
        lines.append(f"{sev}\t{code}\t{location}\t{message}")
    return "\n".join(lines) + "\n"


def _render_where_human(payload: dict) -> str:
    """Render a ``bounds where`` result (symbol + match mode + per-definition rows)."""
    sym = payload.get("symbol", "")
    match = payload.get("match", "exact")
    results = payload.get("results", []) or []
    head = f"where {sym}  ({match} match · {len(results)} result{'s' if len(results) != 1 else ''})"
    if not results:
        return head + "\n\n(no matches)" + _render_where_suggestions(payload.get("suggestions"))
    lines = [head, ""]
    for r in results:
        tag = _EXPOSED_TAG if r.get("exposed") else ""
        lines.append(
            f"  {r.get('symbol')} ({r.get('kind')})  {r.get('file')}:{r.get('line')}"
            f"  → {r.get('owning_subsystem')}{tag}"
        )
    return "\n".join(lines)


def _render_where_suggestions(suggestions: dict | None) -> str:
    """Render the miss-recovery block a 0-result ``where`` carries (parity with the JSON).

    Re-renders the SAME fields ``locate._suggest_for_missing_symbol`` emits — the note, the
    "did you mean" symbols, the candidate subsystems, the --prefix broaden, and the fallback —
    each as a copy-pasteable next command so the human view never drops a hint the JSON has.
    """
    if not suggestions:
        return ""
    lines = ["", suggestions["note"]]
    for s in suggestions.get("did_you_mean", []) or []:
        tag = _EXPOSED_TAG if s.get("exposed") else ""
        lines.append(f"  did you mean: {s['symbol']} ({s['kind']}) → {s['owning_subsystem']}{tag}"
                     f"   ·   {s['try']}")
    for s in suggestions.get("subsystems", []) or []:
        lines.append(f"  subsystem (matched {s['matched_on']}): {s['try']}")
    if suggestions.get("broaden"):
        lines.append(f"  broaden: {suggestions['broaden']}")
    if suggestions.get("fallback"):
        lines.append(f"  {suggestions['fallback']}")
    return "\n" + "\n".join(lines)


def _render_where_file_human(payload: dict) -> str:
    """Render a ``bounds where <path>`` result (file → owning subsystem + the symbols it defines)."""
    rel = payload.get("file", "")
    owner = payload.get("owning_subsystem", "") or "(unowned)"
    results = payload.get("results", []) or []
    head = f"where {rel}  → {owner}  ({len(results)} symbol{'s' if len(results) != 1 else ''})"
    if not results:
        return head + "\n\n(no symbols)"
    lines = [head, ""]
    for r in results:
        tag = _EXPOSED_TAG if r.get("exposed") else ""
        lines.append(f"  {r.get('symbol')} ({r.get('kind')})  :{r.get('line')}{tag}")
    return "\n".join(lines)


def _render_upgrade_check_human(payload: dict) -> str:
    """Render a ``bounds upgrade-check`` result as one short line.

    Re-renders the same data the JSON carries — the ``note`` already holds the
    human-facing summary for every branch (up to date / newer release / dev build /
    couldn't check / no release yet), so we surface it verbatim rather than
    re-deriving the verdict and risking drift from the JSON.
    """
    note = payload.get("note", "")
    return note or "upgrade check complete"


def _render_upgrade_human(payload: dict) -> str:
    """Render a ``bounds upgrade`` result from the same JSON fields."""
    ok = payload.get("ok", True)
    dry_run = payload.get("dry_run", False)

    if dry_run:
        command = " ".join(str(p) for p in payload.get("command", []))
        return f"dry run — would run: {command}"

    if ok:
        version = payload.get("version")
        source = payload.get("source", "github")
        if source == "local":
            where = f"local clone ({payload.get('local')})"
        else:
            where = f"GitHub ({payload.get('ref') or 'main'})"
        ver_str = f" → {version}" if version else ""
        return f"bounds upgraded from {where}{ver_str}"

    reason = {
        "pipx_not_found": "pipx is not installed or not on PATH",
        "timeout": "the install timed out",
        "install_failed": "pipx could not install the package",
    }.get(payload.get("error"), "the upgrade command failed")
    lines = [f"upgrade failed — {reason}"]
    command = " ".join(str(p) for p in payload.get("command", []))
    if command:
        lines.append(f"try manually: {command}")
    stderr = (payload.get("stderr") or "").strip()
    if stderr:
        lines.append(stderr)
    return "\n".join(lines)


def _render_namespace_human(payload: dict) -> str:
    """Render a namespace-filtered describe (``namespace`` + a list of subsystem dicts)."""
    ns = payload.get("namespace", "")
    subs = payload.get("subsystems", []) or []
    header = f"namespace: {ns}  ({len(subs)} subsystem{'s' if len(subs) != 1 else ''})"
    if not subs:
        return header + "\n\n(no subsystems in this namespace)"
    blocks = [_render_subsystem_human(sub) for sub in subs]
    return header + "\n\n" + ("\n\n" + ("-" * 40) + "\n\n").join(blocks)


def _render_discover_human(payload: dict) -> str:
    """Render a ``bounds discover`` proposal: kept subsystems, schema dirs, dropped, writes.

    Re-renders the same data the JSON carries. Schema subsystems are called out with their
    folded table count so a run that maps a migration set reads as a concrete win rather than
    an empty-``exposes`` subsystem.
    """
    candidates = payload.get("candidates", []) or []
    kept = [c for c in candidates if not c.get("dropped")]
    dropped = [c for c in candidates if c.get("dropped")]
    applied = payload.get("applied", False)
    verb = "wrote" if applied else "would map"
    lines = [f"discover: {verb} {len(kept)} subsystem{'s' if len(kept) != 1 else ''}"
             + (" (dry-run — pass --apply to write)" if not applied else "")]
    for cand in sorted(kept, key=lambda c: c.get("name", "")):
        name = cand.get("name", "")
        if cand.get("schema"):
            tables = cand.get("tables", 0)
            detail = f"schema · {cand.get('files', 0)} migration(s) → {tables} table(s)"
        else:
            detail = (f"{cand.get('role', '')}/{cand.get('criticality', '')} · "
                      f"{cand.get('files', 0)} file(s) · {len(cand.get('exposes', []))} export(s)")
        lines.append(f"  {name}  ({detail})")
    if dropped:
        # The full dropped list lives in the JSON; here we summarize so a repo with hundreds
        # of tiny leaf dirs (route folders, fixtures) doesn't bury the signal under names.
        names = sorted(c.get("name", "") for c in dropped)
        shown = ", ".join(names[:12])
        more = f", … and {len(names) - 12} more" if len(names) > 12 else ""
        lines.append(f"dropped {len(dropped)} low-signal dir(s): {shown}{more}")
    notice = payload.get("notice")
    if notice:
        lines.append(notice)
    skipped = payload.get("skipped", []) or []
    if applied and skipped:
        lines.append(f"skipped {len(skipped)} existing manifest(s) — run `bounds calibrate` to reconcile")
    return "\n".join(lines)


def _render_drift_human(payload: dict) -> str:
    """Render a ``calibrate --check`` / ``--dump-baseline`` result from the same JSON."""
    if payload.get("mode") == "calibrate-baseline":
        return payload.get("note", "drift baseline written")
    lines = [payload.get("note", "")]
    for item in payload.get("new_drift", []) or []:
        change = item.get("change", "")
        lines.append(f"  + {item.get('subsystem', '')}: {change} {item.get('target', '')}".rstrip())
    if not payload.get("has_baseline"):
        lines.append("(no baseline committed — run `bounds calibrate --dump-baseline` to set one)")
    return "\n".join(line for line in lines if line)


def _render_list_human(payload: dict) -> str:
    """Render `bounds list`: one aligned row per subsystem (role/criticality + edge degree)."""
    subs = payload.get("subsystems", []) or []
    lines = [f"{payload.get('project', '')}: {len(subs)} subsystem{'s' if len(subs) != 1 else ''}"]
    for s in subs:
        ns = f" @{s['namespace']}" if s.get("namespace") else ""
        lines.append(
            f"  {s.get('name', '?')}{ns}  [{s.get('role', '')}/{s.get('criticality', '')}]  "
            f"exposes {s.get('exposes', 0)}, consumes {s.get('consumes', 0)}, "
            f"consumed_by {len(s.get('consumed_by', []))}"
        )
    return "\n".join(lines)


def _overview_coverage_lines(v: dict) -> list[str]:
    """Description coverage + tests/docs linkage lines for the overview (re-renders the same JSON).

    Description coverage is the concept-discovery health signal; tests/docs linkage is informational
    (tracked, never a gap). Kept out of ``_render_overview_human`` so that renderer stays simple.
    """
    out: list[str] = []
    described = v.get("described") or {}
    if described:
        out.append(f"  described: {described.get('with_description', 0)}/"
                   f"{described.get('total', 0)} subsystems ({described.get('pct', 0.0)}%)")
    for label in ("tests", "docs"):
        bucket = v.get(label) or {}
        if bucket:
            out.append(f"  {label}: {bucket.get('linked', 0)} linked / "
                       f"{bucket.get('unlinked', 0)} unlinked")
    return out


def _render_overview_human(payload: dict) -> str:
    """Render `bounds overview`: a health line + counts; the full edge list stays in the JSON."""
    h = payload.get("health", {}) or {}
    v = h.get("validation", {}) or {}
    status = "ok" if h.get("ok") else "needs attention"
    if h.get("ok") and v.get("warnings", 0):
        status = "ok with warnings"
    lines = [
        f"{payload.get('project', '')}: {payload.get('subsystems', 0)} subsystems — {status}",
        f"  roles: {_kv_inline(payload.get('roles', {}))}",
        f"  criticality: {_kv_inline(payload.get('criticality', {}))}",
        f"  cycles: {h.get('cycles', 0)}   schema errors: {h.get('schema_errors', 0)}"
        f"   dependency edges: {len(payload.get('edges', []))}",
        f"  validation: {v.get('errors', 0)} errors, {v.get('warnings', 0)} warnings"
        f"   (drift: {v.get('structural_drift', 0)}, boundaries: {v.get('boundary_violations', 0)}, "
        f"overlaps: {v.get('ownership_overlaps', 0)})"
        f"   source mapped: {v.get('mapped_pct', 0.0)}%",
    ]
    lines.extend(_overview_coverage_lines(v))
    if v.get("trust_note"):
        lines.append(f"  trust: {v['trust_note']}")
    for step in v.get("next_steps", []) or []:
        lines.append(f"  next: {step}")
    for cyc in payload.get("cycles", []) or []:
        lines.append(f"  cycle: {cyc}")
    return "\n".join(lines)


def _render_impact_human(payload: dict) -> str:
    """Render `bounds impact`: blast radius + the consumers, for a subsystem or an interface."""
    target = payload.get("subsystem") or payload.get("interface") or "?"
    label = "subsystem" if "subsystem" in payload else "interface"
    lines = [f"impact of {label} '{target}': blast radius {payload.get('blast_radius', 0)}"
             + (" (lower bound)" if payload.get("blast_radius_is_lower_bound") else "")]
    # Re-render the honesty fields the JSON carries (parity rule): criticality + basis tell the
    # reader *how* the radius was derived; both are present on the subsystem payload, basis on both.
    crit = payload.get("criticality")
    basis = payload.get("basis")
    meta = [bit for bit in ((f"criticality {crit}" if crit else None),
                            (f"basis {basis}" if basis else None)) if bit]
    if meta:
        lines.append("  " + " · ".join(meta))
    if payload.get("providers"):
        lines.append(f"  provided by: {', '.join(payload['providers'])}")
    direct = payload.get("direct_consumers", []) or []
    lines.append(f"  direct consumers: {', '.join(direct) if direct else '(none)'}")
    transitive = payload.get("transitive_consumers", []) or []
    if transitive:
        lines.append(f"  transitive: {', '.join(transitive)}")
    for c in payload.get("consumers", []) or []:
        ifaces = ", ".join(c.get("interfaces", []))
        via = f" via {c['via']}" if c.get("via") else ""
        lines.append(f"    {c.get('name', '?')}{via}" + (f" [{ifaces}]" if ifaces else ""))
    undeclared = payload.get("undeclared_consumer_edges")
    if undeclared:
        lines.append(f"  undeclared importers (from --verify): {len(undeclared)}")
        for e in undeclared:
            lines.append(f"    {e.get('consumer', '?')}: {', '.join(e.get('files', []))}")
    # The note carries the "this is a lower bound — run --verify" guidance the JSON includes;
    # surface it last so the human view never drops information the JSON has (parity rule).
    note = payload.get("note")
    if note:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def _render_calibrate_human(payload: dict) -> str:
    """Render `bounds calibrate` (diff/apply): a summary line + per-subsystem add/remove."""
    s = payload.get("summary", {}) or {}
    verb = "applied" if payload.get("applied") else "proposed"
    subs = payload.get("subsystems", {}) or {}
    header = (
        f"calibrate: {verb} {s.get('added', 0)} add / {s.get('removed', 0)} remove / "
        f"{s.get('needs_review', 0)} needs-review across {len(subs)} subsystem(s) "
        f"(consumes +{s.get('consumes_added', 0)}/-{s.get('consumes_removed', 0)}, "
        f"interfaces +{s.get('consume_interfaces_added', 0)}/"
        f"-{s.get('consume_interfaces_removed', 0)})"
    )
    if s.get("consumes_unknown"):
        header += f"; {s['consumes_unknown']} unknown consumes edge(s)"
    lines = [header]
    if not payload.get("applied") and subs:
        lines.append("  (diff only — pass --apply to write)")
    for name in sorted(subs):
        p = subs[name]
        bits = []
        if p.get("add_exposes"):
            bits.append("+" + ", +".join(e["name"] for e in p["add_exposes"]))
        if p.get("remove_exposes"):
            bits.append("-" + ", -".join(p["remove_exposes"]))
        if p.get("needs_review"):
            bits.append("review: " + ", ".join(p["needs_review"]))
        if p.get("add_consumes"):
            bits.append("consumes+: " + ", ".join(c["subsystem"] for c in p["add_consumes"]))
        if p.get("add_consume_interfaces"):
            bits.append(
                "interfaces+: "
                + ", ".join(
                    f"{c['subsystem']}({', '.join(c.get('interfaces') or [])})"
                    for c in p["add_consume_interfaces"]
                )
            )
        if p.get("coarsen_consume_interfaces"):
            bits.append(
                "interfaces-: "
                + ", ".join(
                    f"{c['subsystem']}({', '.join(c.get('interfaces') or [])})"
                    for c in p["coarsen_consume_interfaces"]
                )
            )
        if p.get("remove_consume_edges"):
            bits.append("consumes-: " + ", ".join(p["remove_consume_edges"]))
        if p.get("unknown_consumes"):
            bits.append("consumes? " + ", ".join(p["unknown_consumes"]))
        lines.append(f"  {name}: " + " | ".join(bits) if bits else f"  {name}")
    if s.get("consumes_unknown"):
        hint = "pruned" if payload.get("applied") else "rename them, or re-run with --prune-unknown to remove"
        lines.append(f"  (consumes? = consumes a subsystem that doesn't exist — {hint})")
    next_steps = payload.get("next_steps") or []
    if next_steps:
        lines.append("")
        lines.append("next steps:")
        for step in next_steps:
            lines.append(f"  - {step}")
    return "\n".join(lines)


def _render_edit_human(payload: dict) -> str:
    """Render `bounds edit` as a concise metadata update summary."""
    fields = ", ".join(payload.get("updated", []) or [])
    return f"edit: updated {payload.get('subsystem')} ({fields})"


def _render_init_human(payload: dict) -> str:
    """Render `bounds init`: what scaffolding was created vs already present."""
    created = payload.get("created", []) or []
    skipped = payload.get("skipped", []) or []
    updated = payload.get("updated", []) or []
    lines = [f"init: {'created ' + ', '.join(created) if created else 'nothing to create'}"]
    if updated:
        lines.append(f"  updated: {', '.join(updated)}")
    if skipped:
        lines.append(f"  already present: {', '.join(skipped)}")
    if payload.get("hint"):
        lines.append(f"  {payload['hint']}")
    return "\n".join(lines)


def _render_ci_human(payload: dict) -> str:
    """Render `bounds ci --install`: which CI configs were written."""
    created = payload.get("created", []) or []
    skipped = payload.get("skipped", []) or []
    lines = [f"ci: {'installed ' + ', '.join(created) if created else 'nothing new to install'}"]
    if skipped:
        lines.append(f"  already configured: {', '.join(skipped)}")
    return "\n".join(lines)


def _render_guide_human(payload: dict) -> str:
    """Render `bounds guide` as a setup checklist + daily-command reference."""
    steps = payload.get("steps", []) or []
    done = sum(1 for s in steps if s.get("done"))
    head = f"bounds setup — {done}/{len(steps)} steps done"
    if payload.get("complete"):
        head += "  ✓ all set"
    lines = [head, ""]
    manifest_error = payload.get("manifest_error")
    if manifest_error:
        message = manifest_error.get("message", "") if isinstance(manifest_error, dict) else manifest_error
        fix = manifest_error.get("fix", "") if isinstance(manifest_error, dict) else ""
        lines.append(f"manifest error: {message}")
        if fix:
            lines.append(f"  fix: {fix}")
        lines.append("")
    nxt = payload.get("next")
    for i, step in enumerate(steps, 1):
        box = "x" if step.get("done") else " "
        is_next = not step.get("done") and step.get("command") == nxt
        lines.append(f"  [{box}] {i}. {step.get('title', '')}" + ("   ← next" if is_next else ""))
        lines.append(f"         {step.get('command', '')}")
        if not step.get("done") and step.get("why"):
            lines.append(f"         {step['why']}")
    daily = payload.get("daily", []) or []
    if daily:
        width = max((len(d.get("command", "")) for d in daily), default=0)
        lines.append("")
        lines.append("daily commands:")
        for d in daily:
            lines.append(f"  {d.get('command', ''):<{width}}  — {d.get('use', '')}")
    sdd = payload.get("sdd") or {}
    if sdd:
        agent = sdd.get("agent", "generic")
        forced = " (preview)" if sdd.get("forced") else ""
        lines.append("")
        lines.append(f"sdd track: {agent}{forced}")
        for step in sdd.get("steps", []) or []:
            lines.append(f"  {step.get('phase', ''):<9} {step.get('command', '')}")
            lines.append(f"            {step.get('use', '')}")
        freshness = sdd.get("freshness") or {}
        if freshness:
            lines.append(f"  freshness: {freshness.get('contract', '')}")
    return "\n".join(lines)


def _render_coverage_human(payload: dict) -> str:
    """Render coverage reports around decisions, automated fixes, and next action."""
    mode = payload.get("mode")
    if mode == "coverage-auto-fix":
        verb = "added" if payload.get("applied") else "would add"
        proposed = payload.get("added") if payload.get("applied") else payload.get("proposed")
        lines = [f"coverage auto-fix: {verb} {len(proposed or [])} exact path(s)"]
        lines.extend(f"  - {path}" for path in proposed or [])
        lines.append(f"next: {payload.get('next_step', '')}")
        return "\n".join(lines)
    if mode == "coverage-diagnose":
        decisions = payload.get("user_decision_needed", {})
        misses = payload.get("algorithm_miss", {})
        dark = payload.get("unsupported_dark", {})
        return "\n".join(
            [
                f"coverage: {payload.get('mapped_pct', 0.0)}% supported source",
                f"  needs ownership decision: {decisions.get('count', 0)}",
                f"  deterministic algorithm miss: {misses.get('count', 0)}",
                f"  dark unsupported source: {dark.get('count', 0)}",
                f"next: {payload.get('next_step', '')}",
            ]
        )
    if mode == "coverage-summary":
        return "\n".join(
            [
                f"coverage: {payload.get('mapped_pct', 0.0)}% "
                f"({payload.get('supported_mapped', 0)}/{payload.get('supported_total', 0)} supported files)",
                f"  needs ownership decision: {payload.get('user_decision_needed', 0)}",
                f"  deterministic algorithm miss: {payload.get('algorithm_miss', 0)}",
                f"  dark unsupported source: {payload.get('unsupported_dark', 0)}",
                f"next: {payload.get('next_step', '')}",
            ]
        )
    if mode == "coverage":
        supported = payload.get("supported", {})
        breakdown = supported.get("unowned_breakdown", {})
        return "\n".join(
            [
                f"coverage: {payload.get('mapped_pct', 0.0)}% "
                f"({supported.get('mapped', 0)}/{supported.get('total', 0)} supported files)",
                f"  needs ownership decision: "
                f"{breakdown.get('user_decision_needed', {}).get('count', 0)}",
                f"  deterministic algorithm miss: {breakdown.get('algorithm_miss', {}).get('count', 0)}",
                f"  dark unsupported source: {payload.get('unsupported', {}).get('dark', 0)}",
                f"next: {payload.get('next_step', '')}",
            ]
        )
    if "status" in payload and "path" in payload:
        return "\n".join(
            [
                f"{payload.get('path')}: {payload.get('status')}",
                f"  {payload.get('reason', '')}",
                f"next: {payload.get('command', '')}",
            ]
        )
    return _render_generic_human(payload)


def _render_sdd_human(payload: dict) -> str:
    """Render SDD as a command map or readiness report, never inferred progress."""
    mode = payload.get("mode")
    if mode == "sdd-phase":
        configured = "configured" if payload.get("configured") else "not configured"
        return "\n".join(
            [
                f"sdd {payload.get('phase')}: {configured}",
                f"  {payload.get('command', '')}",
                f"  {payload.get('use', '')}",
            ]
        )
    if mode == "sdd-doctor":
        lines = [f"sdd doctor: {'ready' if payload.get('ok') else 'needs attention'}"]
        for check in payload.get("checks", []) or []:
            lines.append(f"  [{'x' if check.get('ok') else ' '}] {check.get('name')}: {check.get('detail')}")
        lines.append(f"next: {payload.get('next_step', '')}")
        return "\n".join(lines)
    lines = [f"sdd: {'enabled' if payload.get('enabled') else 'disabled'} ({payload.get('agent', 'generic')})"]
    for step in payload.get("steps", []) or []:
        lines.append(f"  {step.get('phase', ''):<9} {step.get('command', '')}")
    lines.append(payload.get("note", ""))
    lines.append(f"next: {payload.get('next_step', '')}")
    return "\n".join(lines)


def _render_agent_human(payload: dict) -> str:
    """Render `bounds agent` (detect/sync/check) as a short, action-guided summary.

    Dispatches by payload shape to a per-mode helper so each stays small and single-purpose.
    """
    if "detected" in payload:  # --detect (also the bare `bounds agent` default)
        return _render_agent_detect_human(payload)
    if {"missing", "configured"} <= payload.keys():  # --check
        return _render_agent_check_human(payload)
    return _render_agent_sync_human(payload)  # --sync


def _invocation_note(level: str) -> str:
    """One-line gloss of the agent-invocation level for the human view (with the opt-out)."""
    notes = {
        "off": "advisory files only (no hook)",
        "nudge": "Claude reminder hook on architecture prompts",
        "strict": "Claude pauses before a broad search Bounds can answer",
    }
    note = notes.get(level)
    if note is None:
        return ""
    suffix = "" if level == "off" else " · disable: bounds agent --invocation off"
    return f"  invocation: {level} — {note}{suffix}"


def _render_agent_detect_human(payload: dict) -> str:
    """`bounds agent --detect`: which agents are present + the obvious next step."""
    det = payload.get("detected", []) or []
    inv = _invocation_note(payload.get("invocation", ""))
    if not det:
        body = ("agents detected: (none)\n"
                "  next: 'bounds agent --sync' to wire agents into this repo")
    else:
        body = ("agents detected: " + ", ".join(det) + "\n"
                "  next: 'bounds agent --check' to verify wiring · "
                "'bounds agent --sync' to (re)wire")
    return body + ("\n" + inv if inv else "")


def _render_agent_check_human(payload: dict) -> str:
    """`bounds agent --check`: wiring status, with the fix hint when something needs a sync."""
    lines = [f"agent wiring: {'up to date' if payload.get('ok') else 'needs sync'}"]
    inv = _invocation_note(payload.get("invocation", ""))
    if inv:
        lines.append(inv)
    for label in ("configured", "missing", "stale"):
        if payload.get(label):
            lines.append(f"  {label}: {', '.join(payload[label])}")
    if payload.get("fix"):
        lines.append(f"  fix: {payload['fix']}")
    return "\n".join(lines)


def _render_agent_sync_human(payload: dict) -> str:
    """`bounds agent --sync`: what was written/left alone, with honest, reason-tagged skips."""
    created = payload.get("created", []) or []
    updated = payload.get("updated", []) or []
    unchanged = payload.get("unchanged", []) or []
    skipped = payload.get("skipped_custom", []) or []
    reasons = payload.get("skip_reasons", {}) or {}
    wrote = len(created) + len(updated)
    head = f"agent sync: wrote {wrote} config(s)"
    if not wrote and not skipped and unchanged:
        head += " — everything already current"
    lines = [head]
    inv = _invocation_note(payload.get("invocation", ""))
    if inv:
        lines.append(inv)
    if created:
        lines.append(f"  created: {', '.join(created)}")
    if updated:
        lines.append(f"  updated: {', '.join(updated)}")
    if unchanged:
        lines.append(f"  already current: {', '.join(unchanged)}")
    # Honest skip wording: a file the human wrote ("authored") is not the same as one whose
    # managed block they edited ("hand-edited"). Files with no recorded reason fall back to the
    # neutral "you maintain these" group rather than mislabeling them as edited.
    edited = [p for p in skipped if reasons.get(p) == "hand-edited"]
    yours = [p for p in skipped if reasons.get(p) != "hand-edited"]
    if yours:
        lines.append(f"  left alone (you maintain these): {', '.join(yours)}")
        # The marker hint only applies to files we DON'T already manage (no BOUNDS block yet).
        lines.append("  to let Bounds manage a section in one of these, add empty "
                     "BOUNDS:START / BOUNDS:END markers where you want it, then re-sync")
    if edited:
        lines.append(f"  left alone (you edited the bounds block): {', '.join(edited)}")
        lines.append("  to refresh one of these, revert your in-block edit (or delete the block) "
                     "and re-sync")
    return "\n".join(lines)


def _render_cache_human(payload: dict) -> str:
    """Render `bounds cache` (inspect/migrate/prune) as a summary-first line."""
    if "backend" in payload:  # --inspect
        by_lang = payload.get("by_language", {}) or {}
        return (f"cache: {payload.get('files', 0)} files, "
                f"{len(payload.get('by_subsystem', {}) or {})} subsystems "
                f"[{payload.get('backend', '?')}]"
                + (f"  languages: {_kv_inline(by_lang)}" if by_lang else ""))
    if "migrated" in payload:
        return payload.get("note") or f"cache: migrated {payload.get('files', 0)} file record(s)"
    if "pruned" in payload:
        return f"cache: pruned {payload.get('pruned', 0)}, {payload.get('remaining', 0)} remaining"
    return _render_generic_human(payload)


def _kv_inline(mapping: dict) -> str:
    """Render a small {key: count} mapping as a compact inline string."""
    return ", ".join(f"{k} {v}" for k, v in (mapping or {}).items()) or "(none)"


def emit_error(err: BoundsError, human: bool, stream=None) -> None:
    """Render a fatal ``BoundsError`` as JSON (default) or a one-line human message."""
    if stream is None:
        stream = sys.stderr
    if not human:
        json.dump(err.to_dict(), stream, indent=2, sort_keys=False)
        stream.write("\n")
        return

    stream.write(f"Error [{err.code}]: {err.message}\n")
    if err.fix:
        stream.write(f"  fix: {err.fix}\n")


# ---------------------------------------------------------------------------
# Internal renderers (operate on plain dicts so JSON and human paths agree)
# ---------------------------------------------------------------------------
def _render_report_dict_human(payload: dict) -> str:
    """Render a report dict (shape of ``ValidationReport.to_dict()``) as text."""
    status = payload.get("validation_status", "unknown")
    mode = payload.get("mode", "unknown")
    ok = bool(payload.get("ok", False))
    issues = payload.get("issues", []) or []
    stats = payload.get("stats", {}) or {}

    lines: list[str] = []
    lines.append(f"status: {status}")
    lines.append(f"mode:   {mode}")
    lines.append(_format_stats_line(stats))
    # JSON-first parity: surface the source-coverage % and the docs/tests linkage the JSON carries in
    # stats.coverage.mapping as clean lines (not buried in the stats repr). Tests/docs are tracked,
    # never a blocking gap.
    mapping = (stats.get("coverage") or {}).get("mapping") or {}
    lines.extend(_format_coverage_line(mapping))
    lines.extend(_format_linkage_lines(mapping))

    by_severity: dict[str, list[dict]] = {sev: [] for sev in _SEVERITY_ORDER}
    other: list[dict] = []
    for issue in issues:
        sev = issue.get("severity", "")
        by_severity.get(sev, other).append(issue)

    rendered_any = False
    for sev in _SEVERITY_ORDER:
        group = by_severity[sev]
        if not group:
            continue
        rendered_any = True
        bullet = _BULLETS.get(sev, "-")
        lines.append("")
        lines.append(f"{bullet} {sev}s ({len(group)}):")
        for issue in group:
            lines.extend(_format_issue_lines(issue))

    if other:
        rendered_any = True
        lines.append("")
        lines.append(f"- other ({len(other)}):")
        for issue in other:
            lines.extend(_format_issue_lines(issue))

    if not rendered_any:
        lines.append("")
        lines.append("no issues.")

    next_steps = payload.get("next_steps") or []
    if next_steps:
        lines.append("")
        lines.append("next steps:")
        for step in next_steps:
            lines.append(f"  - {step}")

    error_count = len(by_severity["error"])
    lines.append("")
    if ok and error_count:
        mode = stats.get("enforce", "off")
        lines.append(f"COMPLETED WITH ERRORS (non-enforcing mode: enforce={mode})")
    elif ok:
        lines.append("OK")
    else:
        lines.append(f"FAILED ({error_count} error{'s' if error_count != 1 else ''})")

    return "\n".join(lines)


def _format_stats_line(stats: dict) -> str:
    """Render the stats mapping as a compact, deterministic ``key=value`` line.

    The nested ``coverage.mapping`` block is rendered on its own clean lines (``_format_coverage_line``
    / ``_format_linkage_lines``), so here we flatten ``coverage``'s scalar fields and omit ``mapping``
    rather than dumping a brace-soup dict repr that buries ``mapped_pct``.
    """
    if not stats:
        return "stats:  (none)"
    parts: list[str] = []
    for key in sorted(stats):
        val = stats[key]
        if key == "coverage" and isinstance(val, dict):
            parts.extend(f"{ck}={val[ck]}" for ck in sorted(val) if ck != "mapping")
        else:
            parts.append(f"{key}={val}")
    return "stats:  " + " ".join(parts)


def _format_coverage_line(mapping: dict) -> list[str]:
    """One clean line for SOURCE mapping coverage (re-renders ``stats.coverage.mapping``).

    Labeled "source mapped" to distinguish it from "schema parse coverage" (the ``E_SCHEMA_UNPARSED``
    signal) — two different things both once called "coverage". Omitted when there is no mapping
    block (e.g. ``--quick``, which skips the coverage scan).
    """
    if not mapping:
        return []
    sup = mapping.get("supported") or {}
    unsup = mapping.get("unsupported") or {}
    line = (f"source mapped: {mapping.get('mapped_pct', 0.0)}% "
            f"({sup.get('mapped', 0)}/{sup.get('total', 0)} supported files)")
    extra: list[str] = []
    if unsup.get("declared"):
        extra.append(f"{unsup['declared']} declared")
    if unsup.get("dark"):
        extra.append(f"{unsup['dark']} dark")
    if extra:
        line += " — unsupported: " + ", ".join(extra)
    return [line]


def _format_linkage_lines(mapping: dict) -> list[str]:
    """Render the tests/docs linkage buckets as one short line each (re-renders the same JSON).

    Each bucket is ``{total, linked, unlinked, ...}``; a bucket with no files is omitted so a repo
    without tests/docs stays quiet. Informational only — tests/docs are tracked, never a gap.
    """
    out: list[str] = []
    for label in ("tests", "docs"):
        bucket = mapping.get(label) or {}
        if bucket.get("total"):
            out.append(f"{label}:  {bucket.get('linked', 0)} linked / "
                       f"{bucket.get('unlinked', 0)} unlinked")
    return out


def _format_issue_lines(issue: dict) -> list[str]:
    """Render a single issue as ``  [CODE] subsystem/file: message`` (+ optional fix)."""
    code = issue.get("code", "")
    subsystem = issue.get("subsystem")
    file = issue.get("file")
    message = issue.get("message", "")
    fix = issue.get("fix")

    location = "/".join(part for part in (subsystem, file) if part)
    if location:
        head = f"  [{code}] {location}: {message}"
    else:
        head = f"  [{code}] {message}"

    lines = [head]
    if fix:
        lines.append(f"    fix: {fix}")
    return lines


def _render_rls_posture(posture: dict) -> list[str]:
    """RLS posture as text — re-renders the SAME fields the JSON carries (parity rule).

    Summary line carries every default count (incl. ``rls_enabled``); when ``--full`` populated
    the table-name lists + ``policy_count``, each is rendered too (never omit a field JSON has).
    """
    lines = ["", (
        f"RLS posture: {posture.get('protected', 0)} protected, "
        f"{posture.get('rls_without_policy', 0)} RLS-without-policy, "
        f"{posture.get('unprotected', 0)} unprotected "
        f"({posture.get('rls_enabled', 0)} RLS-enabled of {posture.get('tables', 0)} tables)"
    )]
    # --full restores the actual table-name lists (JSON carries the same lists then too).
    name_lists = [(label, posture.get(key)) for label, key in
                  (("unprotected (no RLS)", "unprotected_tables"),
                   ("RLS but no policy", "rls_without_policy_tables"),
                   ("protected", "protected_tables"))]
    shown = [(label, names) for label, names in name_lists if names]
    for label, names in shown:
        lines.append(f"  {label}: {', '.join(names)}")
    counts = posture.get("policy_count")
    if counts:
        lines.append("  policy_count: " + ", ".join(f"{t}={n}" for t, n in counts.items()))
    if not shown and not counts and (posture.get("unprotected") or posture.get("rls_without_policy")):
        lines.append("  (use --full to list at-risk tables)")
    return lines


def _render_schema_security(payload: dict) -> list[str]:
    """Human lines for RLS posture + schema coverage/diagnostics (re-renders the SAME JSON)."""
    lines: list[str] = []
    posture = payload.get("rls_posture")
    if posture:
        lines.extend(_render_rls_posture(posture))
    coverage = payload.get("schema_coverage")
    if coverage and not coverage.get("complete", True):
        lines.append("")
        n = coverage.get("unextracted_files", 0)
        lines.append(f"⚠ schema parse coverage: PARTIAL — {n} file(s) had DDL Bounds could not fully parse.")
        note = coverage.get("note")
        if note:  # surface the JSON note verbatim (was a hardcoded substitute — a parity drift)
            lines.append(f"  {note}")
    elif coverage:
        lines.append("")
        lines.append("schema parse coverage: complete")
    diagnostics = payload.get("schema_diagnostics")
    if diagnostics:
        lines.append(f"schema diagnostics ({len(diagnostics)}):")
        for diag in diagnostics:
            where = f" [{diag['file']}]" if diag.get("file") else ""
            lines.append(f"  - {diag.get('code', '?')}{where}: {diag.get('message', '')}")
    return lines


def _render_subsystem_human(payload: dict) -> str:
    """Render a subsystem compact (shape of ``SubsystemCompact.to_dict()``) as readable text."""
    lines: list[str] = []
    lines.append(f"subsystem:  {payload.get('name', '?')}")
    lines.append(f"role:       {payload.get('role', '?')}")
    lines.append(f"criticality: {payload.get('criticality', '?')}")
    ns = payload.get("namespace")
    if ns:
        lines.append(f"namespace:  {ns}")
    desc = payload.get("description", "")
    if desc:
        lines.append(f"description: {desc}")
    paths = payload.get("paths", [])
    if paths:
        lines.append(f"paths:      {', '.join(paths)}")
    files = payload.get("files")
    if files:
        lines.append(f"files:      {', '.join(files)}")
    elif payload.get("file_count"):
        lines.append(f"files:      {payload['file_count']} file(s)  (use --full to list)")
    # Linked docs/tests (explicit + convention) — present only under --full, same gate as the roster.
    docs = payload.get("docs")
    if docs:
        lines.append(f"docs:       {', '.join(docs)}")
    tests = payload.get("tests")
    if tests:
        lines.append(f"tests:      {', '.join(tests)}")
    entry_points = payload.get("entry_points", [])
    if entry_points:
        lines.append(f"entry_points: {', '.join(entry_points)}")
    unparsed = payload.get("unparsed_files", [])
    if unparsed:
        lines.append(f"unparsed_files: {', '.join(unparsed)}")
    overlaps = payload.get("overlaps", [])
    if overlaps:
        lines.append(f"⚠ overlaps ({len(overlaps)}): same path/file claimed by another subsystem")
        for o in overlaps:
            lines.append(f"  - {o.get('file', '?')}: {o.get('message', '')}")

    exposes = payload.get("exposes", [])
    if exposes:
        lines.append("")
        lines.append(f"exposes ({len(exposes)}):")
        for e in exposes:
            name = e.get("name", "?")
            kind = e.get("kind", "?")
            file = e.get("file", "")
            verified = e.get("verified")
            tag = ""
            if verified is True:
                tag = " [verified]"
            elif verified is False:
                tag = " [unverified]"
            if e.get("entry_point"):
                tag += " [entry-point]"
            loc = f"  {file}" if file else ""
            lines.append(f"  - {name} ({kind}){loc}{tag}")
            columns = e.get("columns", [])
            if columns:
                lines.append(f"    columns: {', '.join(columns)}")
    else:
        lines.append("")
        lines.append("exposes:    (none)")

    tables = payload.get("tables", [])
    if tables:
        lines.append("")
        schema_hash = payload.get("schema_hash", "")
        suffix = f"  (schema_hash {schema_hash[:12]})" if schema_hash else ""
        lines.append(f"tables ({len(tables)}):{suffix}")
        for table in tables:
            columns = table.get("columns", [])
            detail = f" [{', '.join(columns)}]" if columns else ""
            lines.append(f"  - {table.get('name', '?')}{detail}")

    objects = payload.get("schema_objects")
    counts = payload.get("schema_object_counts")
    if objects:
        lines.append("")
        lines.append(f"schema objects ({len(objects)}):")
        for obj in objects:
            on = f" on {obj['table']}" if obj.get("table") else ""
            lines.append(f"  - {obj.get('kind', '?')} {obj.get('name', '?')}{on}")
    elif counts:
        lines.append("")
        summary = ", ".join(f"{n} {kind}" for kind, n in counts.items())
        lines.append(f"schema objects: {summary}  (use --full to list)")

    lines.extend(_render_schema_security(payload))

    consumes = payload.get("consumes", [])
    if consumes:
        lines.append("")
        lines.append(f"consumes ({len(consumes)}):")
        for c in consumes:
            sub = c.get("subsystem", "?")
            iface = c.get("interfaces", [])
            via = c.get("via")
            detail = f" via {via}" if via else ""
            iface_str = f" [{', '.join(iface)}]" if iface else ""
            lines.append(f"  - {sub}{iface_str}{detail}")
    else:
        lines.append("")
        lines.append("consumes:   (none)")

    consumed_by = payload.get("consumed_by", [])
    if consumed_by:
        lines.append(f"consumed_by: {', '.join(sorted(consumed_by))}")
    else:
        lines.append("consumed_by: (none)")

    val_status = payload.get("validation_status", "")
    if val_status:
        lines.append(f"validation_status: {val_status}")
    proj_status = payload.get("project_status", "")
    if proj_status:
        lines.append(f"project_status: {proj_status}")
    semantic = payload.get("semantic")
    if semantic is not None:
        lines.append(f"semantic: {semantic.get('note', '')}")

    return "\n".join(lines)


def _render_generic_human(payload) -> str:
    """Pretty-print an arbitrary dict (or value) as readable key/value text."""
    if not isinstance(payload, dict):
        return _scalar(payload)
    if not payload:
        return "(empty)"

    lines: list[str] = []
    for key in payload:
        lines.extend(_format_kv(str(key), payload[key], indent=0))
    return "\n".join(lines)


def _format_kv(key: str, value, indent: int) -> list[str]:
    """Render one ``key: value`` pair, expanding nested dicts and lists by indentation."""
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return [f"{pad}{key}: {{}}"]
        lines = [f"{pad}{key}:"]
        for sub_key in value:
            lines.extend(_format_kv(str(sub_key), value[sub_key], indent + 1))
        return lines
    if isinstance(value, list):
        if not value:
            return [f"{pad}{key}: []"]
        if all(not isinstance(item, (dict, list)) for item in value):
            return [f"{pad}{key}: " + ", ".join(_scalar(item) for item in value)]
        lines = [f"{pad}{key}:"]
        for item in value:
            if isinstance(item, dict):
                lines.append(f"{pad}  -")
                for sub_key in item:
                    lines.extend(_format_kv(str(sub_key), item[sub_key], indent + 2))
            else:
                lines.append(f"{pad}  - {_scalar(item)}")
        return lines
    return [f"{pad}{key}: {_scalar(value)}"]


def _scalar(value) -> str:
    """Render a scalar value as a stable string (``null`` for None)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
