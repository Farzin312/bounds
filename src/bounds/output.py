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

# Severity groups, in the order they render and rank.
_SEVERITY_ORDER = ("error", "warning", "info")
_BULLETS = {"error": "✗", "warning": "⚠", "info": "ℹ"}


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

    if isinstance(payload, dict) and "validation_status" in payload and "mode" in payload:
        stream.write(_render_report_dict_human(payload))
    elif isinstance(payload, dict) and {"symbol", "match", "results"} <= payload.keys():
        stream.write(_render_where_human(payload))
    elif isinstance(payload, dict) and {"current", "checked", "outdated"} <= payload.keys():
        stream.write(_render_upgrade_check_human(payload))
    elif isinstance(payload, dict) and {"command", "dry_run", "source"} <= payload.keys():
        stream.write(_render_upgrade_human(payload))
    elif isinstance(payload, dict) and "namespace" in payload and "subsystems" in payload:
        stream.write(_render_namespace_human(payload))
    elif isinstance(payload, dict) and "name" in payload and "role" in payload:
        stream.write(_render_subsystem_human(payload))
    else:
        stream.write(_render_generic_human(payload))
    stream.write("\n")


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
        return head + "\n\n(no matches)"
    lines = [head, ""]
    for r in results:
        tag = " [exposed]" if r.get("exposed") else ""
        lines.append(
            f"  {r.get('symbol')} ({r.get('kind')})  {r.get('file')}:{r.get('line')}"
            f"  → {r.get('owning_subsystem')}{tag}"
        )
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
    command = " ".join(str(p) for p in payload.get("command", []))
    lines = [payload.get("note") or "upgrade complete", f"command: {command}"]
    if not payload.get("ok", True):
        stderr = payload.get("stderr", "")
        if stderr:
            lines.append(f"stderr: {stderr}")
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

    error_count = len(by_severity["error"])
    lines.append("")
    if ok:
        lines.append("OK")
    else:
        lines.append(f"FAILED ({error_count} error{'s' if error_count != 1 else ''})")

    return "\n".join(lines)


def _format_stats_line(stats: dict) -> str:
    """Render the stats mapping as a compact, deterministic ``key=value`` line."""
    if not stats:
        return "stats:  (none)"
    parts = [f"{key}={stats[key]}" for key in sorted(stats)]
    return "stats:  " + " ".join(parts)


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
    files = payload.get("files", [])
    if files:
        lines.append(f"files:      {', '.join(files)}")
    entry_points = payload.get("entry_points", [])
    if entry_points:
        lines.append(f"entry_points: {', '.join(entry_points)}")
    unparsed = payload.get("unparsed_files", [])
    if unparsed:
        lines.append(f"unparsed_files: {', '.join(unparsed)}")

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
        lines.append(f"tables ({len(tables)}):")
        for table in tables:
            columns = table.get("columns", [])
            detail = f" [{', '.join(columns)}]" if columns else ""
            lines.append(f"  - {table.get('name', '?')}{detail}")

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
