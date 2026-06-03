"""Command-line interface for Bounds.

Every command prints a JSON object to stdout by default and accepts ``-H/--human`` for a readable
rendering of the same data. Fatal conditions raise :class:`~bounds.errors.BoundsError`, which is
caught here, emitted as ``{"error": {...}}``, and exits with code 2. Blocking validation failures
exit 1; everything else exits 0.
"""

from __future__ import annotations

import itertools
import sys
import threading
from collections import Counter
from contextlib import nullcontext
from pathlib import Path

import click

from . import (
    __version__,
    agenthook,
    agentsync,
    calibrate as calibrate_mod,
    ciconfig,
    config,
    describe as describe_mod,
    discover as discover_mod,
    errors,
    guide as guide_mod,
    locate,
    output,
    upgrade as upgrade_mod,
    update_check,
)
from .cache import store as cache_store
from .extract.scan import coverage_has_gap
from .ignore import IgnoreMatcher
from .manifest import loader as manifest_loader
from .validate import engine as validate_engine
from .validate.checks import CheckContext, check_cycles

__all__ = ["main"]

# ---- shared option ----
_human = click.option("--human", "-H", "human", is_flag=True, default=False,
                      help="Human-readable output instead of JSON.")


def _require_root() -> Path:
    root = manifest_loader.find_root(Path.cwd())
    if root is None:
        raise errors.BoundsError(
            errors.E_MANIFEST_NOT_FOUND,
            "no .bounds/ directory found in this or any parent directory",
            fix="run 'bounds init --root' to initialize Bounds in this project",
        )
    return root


class _Spinner:
    """Minimal stderr spinner for long-running commands; no-op when stderr is not a TTY.

    The first frame is deferred by ``_DELAY_SECONDS`` so a command that finishes inside
    the grace period draws *nothing* — that lets the spinner be applied to every command
    uniformly without fast paths (cache hits, ``list``-class reads) flashing a spinner for
    a single frame. Waits go through the stop ``Event`` (not ``time.sleep``) so the thread
    also tears down instantly instead of lagging up to a frame on exit.
    """

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    _DELAY_SECONDS = 0.12  # grace period before the first frame — fast commands never flash
    _INTERVAL_SECONDS = 0.08

    def __init__(self, message: str) -> None:
        self._msg = message
        self._stop = threading.Event()
        self._drew = False
        self._thread = threading.Thread(target=self._spin, daemon=True)

    def _spin(self) -> None:
        # Defer the first frame: if the work completes within the grace period, return
        # without ever writing, so a fast command leaves stderr untouched.
        if self._stop.wait(self._DELAY_SECONDS):
            return
        for frame in itertools.cycle(self._FRAMES):
            sys.stderr.write(f"\r{frame} {self._msg}")
            sys.stderr.flush()
            self._drew = True
            if self._stop.wait(self._INTERVAL_SECONDS):
                break

    def __enter__(self):
        if sys.stderr.isatty():
            self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()
        # Only clear if we actually drew — a sub-grace-period run wrote nothing to clear.
        if self._drew:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()


def _progress(message: str):
    """Reusable loading indicator for any long-running command body.

    Returns a context manager that shows a stderr spinner while the wrapped block runs.
    Wrap ONLY the heavy compute — never ``output.emit`` — so the spinner line is cleared
    before any stdout is written (otherwise the clear sequence would land after the
    rendered result on a TTY). It is a no-op when stderr is not a TTY, so JSON/piped
    output stays byte-clean and agents never see spinner frames. This is the single seam
    every command reuses; the message is the only thing that varies.
    """
    return _Spinner(message)


def _run(human: bool, fn, ci: bool = False):
    """Execute a command body, turning BoundsError into a structured error + exit 2.

    ``ci`` is threaded from commands that expose ``--ci`` so a fatal error stays in the
    tab-delimited CI contract (one ``fatal`` line) instead of falling back to JSON.
    """
    try:
        fn()
    except errors.BoundsError as err:
        output.emit(err.to_dict(), human, ci=ci)
        sys.exit(config.EXIT_FATAL)
    except SystemExit:
        # A command body may legitimately call sys.exit (e.g. non-zero from validate);
        # let it propagate untouched rather than masking it as an internal error.
        raise
    except Exception:  # noqa: BLE001 - top-level guard: never leak a raw traceback to an agent
        # Any unexpected, non-BoundsError failure becomes a generic fatal error object so the
        # JSON-first contract holds (one {"error":{...}} object, exit 2) instead of a Python
        # traceback. The message is intentionally generic — no stack trace in the payload.
        err = errors.BoundsError(
            errors.E_INTERNAL,
            "an unexpected internal error occurred",
            fix="re-run with -H/--human for more context, or file an issue at "
            "https://github.com/Farzin312/bounds/issues",
        )
        output.emit(err.to_dict(), human, ci=ci)
        sys.exit(config.EXIT_FATAL)


def _interactive_human(explicit_human: bool) -> bool:
    """Whether an interactive maintenance command should render the human announcement.

    `upgrade`/`upgrade-check` are run by a person, not consumed by an agent, so they default
    to the human view in a terminal while still honoring the JSON-first contract when piped
    or redirected (an agent/script captures non-TTY output). `--human` always forces human.
    """
    return explicit_human or sys.stdout.isatty()


def _version_display(raw: str) -> str:
    """The ``--version`` string. Versions are dynamic CalVer (``YYYY.M.<build>``, e.g.
    ``2026.6.24``) — already clean numbers — so this only annotates the not-installed case."""
    if raw == "unknown":
        return "unknown (not installed as a package)"
    return raw


# Commands rendered in purpose-ordered sections in ``bounds --help`` (instead of click's flat
# alphabetical list) so a human scanning the help reads them by *when they'd reach for them*, not
# by spelling. Every registered command must appear in exactly one group; a stray command not
# listed here still shows up under "Other" (a loud signal to add it), so nothing is ever hidden.
_COMMAND_GROUPS = (
    ("Set up", ("guide", "init", "discover", "agent", "ci")),
    ("Read the map (do this before grepping source)", ("list", "describe", "overview", "where", "impact")),
    ("Catch drift", ("validate", "preflight", "calibrate")),
    ("Maintain", ("cache", "upgrade", "upgrade-check")),
)

# Leading "\b" marks the quick-start block as pre-formatted so click does not reflow the
# command line mid-word; the prose paragraph after the blank line is allowed to wrap normally.
_HELP_EPILOG = (
    "\b\n"
    "New here? Run 'bounds guide' for a state-aware setup checklist.\n"
    "Quick start:\n"
    "  bounds init --root  ·  bounds discover --apply  ·  bounds agent --sync\n"
    "\n"
    "Output is JSON by default; add -H/--human for a readable view of the same data.\n"
    "AI agents: read AGENTS.md, then use 'bounds list' and 'bounds describe <name>'."
)


class _BoundsGroup(click.Group):
    """``click.Group`` that lists commands in :data:`_COMMAND_GROUPS` sections.

    Only the top-level ``bounds --help`` listing changes; per-command help, parsing, and the
    JSON contract are untouched. Each section's short help comes from the command's own
    ``short_help`` (kept concise so click never truncates it with an ellipsis).
    """

    def format_commands(self, ctx, formatter):  # noqa: D102 - click hook
        listed: set[str] = set()
        sections: list[tuple[str, list[tuple[str, str]]]] = []
        for title, names in _COMMAND_GROUPS:
            rows = []
            for name in names:
                cmd = self.get_command(ctx, name)
                if cmd is None or getattr(cmd, "hidden", False):
                    continue
                listed.add(name)
                rows.append((name, cmd.get_short_help_str(limit=formatter.width)))
            if rows:
                sections.append((title, rows))
        # Safety net: a registered command we forgot to group still shows, never silently dropped.
        rest = [
            (name, self.get_command(ctx, name).get_short_help_str(limit=formatter.width))
            for name in self.list_commands(ctx)
            if name not in listed and self.get_command(ctx, name) is not None
        ]
        if rest:
            sections.append(("Other", rest))
        for title, rows in sections:
            with formatter.section(title):
                formatter.write_dl(rows)


@click.group(cls=_BoundsGroup, context_settings={"help_option_names": ["-h", "--help"]},
             epilog=_HELP_EPILOG)
@click.version_option(_version_display(__version__), prog_name="bounds",
                      message="%(prog)s %(version)s")
def main() -> None:
    """Bounds — AI-first architecture context for coding agents.

    Give agents a verified, token-lean map before they search source; then catch
    architecture drift in CI. Structural extraction and validation are deterministic
    and zero-LLM.
    """


# ===========================================================================
# guide
# ===========================================================================
@main.command("guide", short_help="Setup checklist: get Bounds working in this project")
@click.option("--sdd", "sdd", is_flag=True, default=False,
              help="Include the optional Spec-Driven Development track even if root.yaml has not enabled it.")
@_human
def guide_cmd(sdd: bool, human: bool) -> None:
    """Show the setup steps (init → discover → agent --sync → ci) with what's already done, plus
    the daily commands and the single next action. Read-only; safe to run anywhere."""
    human = _interactive_human(human)

    def go() -> None:
        output.emit(guide_mod.run_guide(Path.cwd(), sdd=sdd), human)

    _run(human, go)


# ===========================================================================
# list
# ===========================================================================
@main.command("list", short_help="Show the subsystem map (read before grepping source)")
@click.option("--namespace", default=None, help="Only list subsystems in this namespace.")
@_human
def list_cmd(namespace: str | None, human: bool) -> None:
    """Show the subsystem map agents should read before broad source search."""

    def go() -> None:
        root = _require_root()
        rootm, subs, _ = manifest_loader.load_all(root)
        entries: list[dict] = []
        for n in sorted(subs):
            sub = subs[n]
            if namespace is not None and sub.namespace != namespace:
                continue
            entry: dict = {
                "name": sub.name,
                "role": sub.role,
                "criticality": sub.criticality,
            }
            if sub.namespace:
                entry["namespace"] = sub.namespace
            entry.update({
                "description": sub.description,
                "exposes": len(sub.exposes),
                "consumes": len(sub.consumes),
                "consumed_by": sorted(sub.consumed_by),
            })
            entries.append(entry)
        payload = {"project": rootm.project, "subsystems": entries}
        output.emit(payload, human)

    _run(human, go)


# ===========================================================================
# describe
# ===========================================================================
@main.command("describe", short_help="Show one subsystem's verified surface/table contract")
@click.argument("name", required=False)
@click.option("--namespace", default=None,
              help="Describe every subsystem in this namespace instead of one by name.")
@click.option("--deep", is_flag=True, default=False, help="Include Tier-3 LLM enrichment (roadmap).")
@click.option("--full", "full", is_flag=True, default=False,
              help="Include the full file roster and schema-object list (default shows counts).")
@_human
def describe_cmd(name: str | None, namespace: str | None, deep: bool, full: bool, human: bool) -> None:
    """Return one verified subsystem surface/table contract as JSON."""

    def go() -> None:
        if name is None and namespace is None:
            raise errors.BoundsError(
                errors.E_USAGE,
                "nothing to describe",
                fix="pass a subsystem name, or --namespace <ns> to describe a group",
            )
        if name is not None and namespace is not None:
            raise errors.BoundsError(
                errors.E_USAGE,
                "pass either a subsystem name or --namespace, not both",
                fix="run 'bounds describe <name>' or 'bounds describe --namespace <ns>'",
            )
        root = _require_root()
        rootm, subs, _ = manifest_loader.load_all(root)
        entry_matcher = IgnoreMatcher(rootm.entry_points)

        if namespace is not None:
            matched = [subs[n] for n in sorted(subs) if subs[n].namespace == namespace]
            with _progress("reading subsystems..."):
                # One shared read-only quick run; each describe_one scopes status to its subsystem.
                report = describe_mod.status_report(root) if matched else None
                payload = {
                    "namespace": namespace,
                    "subsystems": [describe_mod.describe_one(root, s, deep, report, entry_matcher, full)
                                   for s in matched],
                }
            output.emit(payload, human)
            return

        if name not in subs:
            raise errors.BoundsError(
                errors.E_SUBSYSTEM_NOT_FOUND,
                f"subsystem '{name}' not found",
                fix=f"known subsystems: {sorted(subs)}; or run 'bounds init --subsystem {name}'",
            )
        with _progress("reading subsystem..."):
            described = describe_mod.describe_one(
                root, subs[name], deep, describe_mod.status_report(root), entry_matcher, full,
            )
        output.emit(described, human)

    _run(human, go)


# ===========================================================================
# validate
# ===========================================================================
# File-selection + output toggles shared by validate and preflight.
_scan_options = [
    click.option("--include-ignored", is_flag=True, default=False,
                 help="Scan files normally excluded by .boundsignore."),
    click.option("--include-gitignored", is_flag=True, default=False,
                 help="Scan files excluded by .gitignore."),
    click.option("--follow-symlinks", is_flag=True, default=False,
                 help="Follow external symlinks instead of skipping them with a warning."),
    click.option("--fail-on-unowned", is_flag=True, default=False,
                 help="Treat tracked source files outside every subsystem as a blocking error."),
    click.option("--ci", is_flag=True, default=False,
                 help="CI plaintext output: one issue per line (tab-delimited)."),
]


def _scan_flags(fn):
    """Apply the shared scan/output options to a command (innermost first)."""
    for option in reversed(_scan_options):
        fn = option(fn)
    return fn


@main.command("validate", short_help="Catch source-vs-contract drift after edits")
@click.option("--quick", is_flag=True, default=False, help="Git-diff incremental validation.")
@click.option("--mode", type=click.Choice(sorted(config.VALID_MODES)), default=None,
              help="Explicit validation mode (default: full).")
@click.option("--enforce", type=click.Choice(sorted(config.VALID_ENFORCE)), default=None,
              help="Override root.yaml enforce setting.")
@click.option("--base", default="HEAD", show_default=True, help="Git ref to diff against in quick mode.")
@_scan_flags
@_human
def validate_cmd(quick: bool, mode: str | None, enforce: str | None, base: str,
                 include_ignored: bool, include_gitignored: bool, follow_symlinks: bool,
                 fail_on_unowned: bool, ci: bool, human: bool) -> None:
    """Catch source-vs-contract drift after agent or human edits."""

    def go() -> None:
        root = _require_root()
        selected = "quick" if quick else (mode or "full")
        with _progress("validating..."):
            report = validate_engine.run(
                root, mode=selected, base=base, enforce=enforce,
                include_ignored=include_ignored, include_gitignored=include_gitignored,
                follow_symlinks=follow_symlinks, fail_on_unowned=fail_on_unowned,
            )
        output.emit(report.to_dict(), human, ci=ci)
        sys.exit(config.EXIT_OK if report.ok else config.EXIT_BLOCKED)

    _run(human, go, ci=ci)


# ===========================================================================
# preflight
# ===========================================================================
@main.command("preflight", short_help="Blocking CI gate: drift, boundaries, contracts, cycles")
@_scan_flags
@_human
def preflight_cmd(include_ignored: bool, include_gitignored: bool, follow_symlinks: bool,
                  fail_on_unowned: bool, ci: bool, human: bool) -> None:
    """Run the blocking CI gate for drift, boundaries, contracts, and cycles."""

    def go() -> None:
        root = _require_root()
        with _progress("running checks..."):
            report = validate_engine.run(
                root, mode="preflight",
                include_ignored=include_ignored, include_gitignored=include_gitignored,
                follow_symlinks=follow_symlinks, fail_on_unowned=fail_on_unowned,
            )
        counts: Counter = Counter()
        for i in report.issues:
            counts[i.code] += i.count  # sum magnitude (rolled-up issues carry count>1), not issue rows
        payload = report.to_dict()
        payload["checks"] = {
            "structural_drift": counts.get(errors.E_STRUCTURAL_DRIFT, 0),
            "boundary_compliance": counts.get(errors.E_BOUNDARY_VIOLATION, 0),
            "contract_compliance": counts.get(errors.E_CONTRACT_MISSING_EXPORT, 0),
            "cross_subsystem_impact": counts.get(errors.E_STALE_INTERFACE, 0),
            "cycle_detection": counts.get(errors.E_CYCLE_DETECTED, 0),
            "orphan_detection": counts.get(errors.E_ORPHAN_EXPORT, 0),
        }
        output.emit(payload, human, ci=ci)
        sys.exit(config.EXIT_OK if report.ok else config.EXIT_BLOCKED)

    _run(human, go, ci=ci)


# ===========================================================================
# overview
# ===========================================================================
@main.command("overview", short_help="Project health dashboard")
@_human
def overview_cmd(human: bool) -> None:
    """Project health dashboard."""

    def go() -> None:
        root = _require_root()
        rootm, subs, schema_issues = manifest_loader.load_all(root)
        roles = Counter(s.role for s in subs.values())
        criticality = Counter(s.criticality for s in subs.values())
        edges = [
            {"from": n, "to": c.subsystem, "interfaces": sorted(c.interfaces)}
            for n in sorted(subs)
            for c in subs[n].consumes
        ]
        # Deterministic edge order regardless of consumes-declaration order.
        edges.sort(key=lambda e: (e["from"], e["to"], e["interfaces"]))
        ctx = CheckContext(root, rootm, subs, {}, {}, set(), set())
        cycle_issues = check_cycles(ctx)
        schema_errors = sum(1 for i in schema_issues if i.severity == "error")
        # Fold a real validation pass into health so overview can never report ok=true while
        # validate would block (BOUNDS-009): a clean manifest graph says nothing about whether
        # the *source* still matches its contracts (drift) or respects boundaries. Run the
        # existing full engine read-only (persist=False — overview is a read, never mutates the
        # cache); reuse its error counts + mapping coverage rather than re-walking the tree.
        with _progress("checking health..."):
            report = validate_engine.run(root, mode="full", persist=False)
        counts: Counter = Counter()
        for i in report.issues:
            counts[i.code] += i.count  # sum magnitude (rolled-up issues carry count>1), not issue rows
        cov = report.stats.get("coverage", {})
        mapping = cov.get("mapping") or {}
        validation_errors = report.errors()
        validation = {
            "ok": not validation_errors,
            "errors": len(validation_errors),
            "warnings": len(report.warnings()),
            "structural_drift": counts.get(errors.E_STRUCTURAL_DRIFT, 0),
            "boundary_violations": counts.get(errors.E_BOUNDARY_VIOLATION, 0),
            "ownership_overlaps": counts.get(errors.E_SUBSYSTEM_OVERLAP, 0),
            "contract_gaps": counts.get(errors.E_CONTRACT_MISSING_EXPORT, 0),
            "stale_interfaces": counts.get(errors.E_STALE_INTERFACE, 0),
            "mapped_pct": mapping.get("mapped_pct", 0.0),
        }
        # Description coverage: how many subsystems carry prose. Empty descriptions silently break
        # *concept* discovery — an agent can map a concept ("auth", "billing", "profile") to a
        # subsystem only via `describe`/`where` description matches, which need prose to match. A repo
        # that ran `discover` and never filled descriptions looks healthy while concept lookups all
        # miss. Pure manifest data (no extraction); surfaced so the gap is measurable, not silent.
        described_n = sum(1 for s in subs.values() if (s.description or "").strip())
        total_subs = len(subs)
        validation["described"] = {
            "with_description": described_n,
            "total": total_subs,
            "pct": round(100.0 * described_n / total_subs, 1) if total_subs else 100.0,
        }
        next_steps: list[str] = []
        # Nudge only when the gap materially hurts discovery: nothing described, or under half.
        if total_subs and described_n * 2 < total_subs:
            missing = total_subs - described_n
            next_steps.append(
                f"Add subsystem descriptions ({missing} of {total_subs} are empty). Without prose, "
                "concept lookups (`bounds where <concept>`, `bounds describe`) can't map a concept to "
                "a subsystem — only exact symbol names match. Fill each manifest's `description:` so "
                "agents discover by concept instead of falling back to grep."
            )
        if coverage_has_gap(mapping):
            # Name WHAT is unmapped and the right move per gap kind — so an agent reading overview is
            # told loudly which files are dark and that unsupported-language manifests are durable
            # (hand-author once; calibrate/validate won't strip or flag them). JSON-first: the same
            # mapping fields the human view re-renders. A `dark`-only gap can still sit at 100% mapped
            # (all supported source owned), so gate on the gap predicate, not the % alone.
            sup, unsup = mapping.get("supported", {}), mapping.get("unsupported", {})
            bits: list[str] = []
            if sup.get("unowned"):
                bits.append(
                    f"{sup['unowned']} supported file(s) in no subsystem — add to a manifest's `paths:`"
                )
            if unsup.get("dark"):
                langs = ", ".join(sorted(unsup.get("by_language", {})))
                bits.append(
                    f"{unsup['dark']} unsupported-language file(s) no manifest claims"
                    + (f" ({langs})" if langs else "")
                    + " — hand-author a manifest's `exposes` (durable: calibrate/validate keep it)"
                )
            detail = ("; ".join(bits)) if bits else "missing library source"
            next_steps.append(
                f"Close the coverage gap ({detail}). Run `bounds validate -H` for the full "
                "`E_COVERAGE_GAP` fix (see docs/coverage.md), then rerun `bounds validate`."
            )
        if validation["ownership_overlaps"]:
            next_steps.append(
                "Resolve duplicate ownership: run `bounds validate -H` for the overlapping "
                "subsystems, then narrow one path or move shared files to `files:`."
            )
        if validation_errors:
            next_steps.append(
                "Refresh the generated model or fix the contract: run `bounds validate -H` "
                "and address error-severity drift, missing exports, or boundary violations."
            )
        if cycle_issues:
            next_steps.append("Break dependency cycles before treating impact results as complete.")
        if schema_errors:
            next_steps.append("Fix schema manifest errors before trusting schema/table answers.")
        if not next_steps:
            next_steps.append(
                "Use `bounds list` → `bounds describe <name>` → `bounds impact <name>` to scope "
                "changes, then `bounds validate --quick` after edits."
            )
        validation["trust_note"] = (
            "Bounds is authoritative for tree-sitter-verified symbols in mapped source. "
            "If mapped_pct is below 100, unmapped library source is outside the architecture map; "
            "use Bounds to scope first, then inspect source where the map is incomplete."
        )
        validation["next_steps"] = next_steps
        # Informational doc/test linkage (tracked, never a blocking gap) — carried so the human
        # overview can re-render the same data the validate JSON exposes (JSON-first parity).
        for label in ("tests", "docs"):
            bucket = mapping.get(label) or {}
            if bucket.get("total"):
                validation[label] = {"linked": bucket.get("linked", 0),
                                     "unlinked": bucket.get("unlinked", 0)}
        payload = {
            "project": rootm.project,
            "subsystems": len(subs),
            "roles": dict(sorted(roles.items())),
            "criticality": dict(sorted(criticality.items())),
            "edges": edges,
            "cycles": [i.message for i in cycle_issues],
            "schema_issues": [i.to_dict() for i in schema_issues],
            "health": {
                # ok is true only when the dashboard is actually clean: no error-severity
                # validation issues, no graph cycles, and no schema errors. This is stricter
                # than `report.ok`, which can stay true under enforce=off while still reporting
                # real drift.
                "ok": not validation_errors and not cycle_issues and schema_errors == 0,
                "schema_errors": schema_errors,
                "cycles": len(cycle_issues),
                "validation": validation,
            },
        }
        output.emit(payload, human)

    _run(human, go)


# ===========================================================================
# impact
# ===========================================================================
@main.command("impact", short_help="Blast radius before changing a subsystem or table")
@click.argument("name", required=True)
@click.option("--verify", is_flag=True, default=False,
              help="Cross-check the declared blast radius against the resolved import graph "
                   "(extracts source — off the fast path).")
@click.option("--include-raw-queries", "include_raw", is_flag=True, default=False,
              help="Add advisory raw-SQL-string consumers of a table (LOW-CONFIDENCE, never "
                   "counted in blast_radius, never blocking).")
@_human
def impact_cmd(name: str, verify: bool, include_raw: bool, human: bool) -> None:
    """Show blast radius before changing a subsystem interface or table."""

    def go() -> None:
        with _progress("computing impact..."):
            result = locate.run_impact(_require_root(), name, verify, include_raw)
        output.emit(result, human)

    _run(human, go)


# ===========================================================================
# where
# ===========================================================================
@main.command("where", short_help="Locate a symbol or table without grepping")
@click.argument("symbol", required=True)
@click.option("--prefix", is_flag=True, default=False,
              help="Match symbols whose name starts with SYMBOL, instead of an exact match.")
@_human
def where_cmd(symbol: str, prefix: bool, human: bool) -> None:
    """Locate a symbol/table without asking an agent to grep blindly."""

    def go() -> None:
        with _progress("searching..."):
            result = locate.run_where(_require_root(), symbol, prefix)
        output.emit(result, human)

    _run(human, go)


# ===========================================================================
# init
# ===========================================================================
_ROOT_TEMPLATE = '''version: "{version}"
project: {project}
languages: [python]
enforce: "off"
# Root-level bootstrap files (globs) — main.py, app.py, setup.py, etc.
# Listed here, they stay exempt from `validate --fail-on-unowned`.
entry_points: []
subsystems: []
'''

_SUBSYS_TEMPLATE = '''name: {name}
role: library
criticality: leaf
description: TODO describe this subsystem.
{namespace_line}paths:
  - src/{name}
exposes: []
consumes: []
'''


@main.command("init", short_help="Initialize .bounds/, or add a subsystem")
@click.option("--root", "root_flag", is_flag=True, default=False, help="Initialize .bounds/root.yaml.")
@click.option("--subsystem", default=None, help="Scaffold a new subsystem manifest.")
@click.option("--namespace", default=None, help="Namespace for the scaffolded subsystem (requires --subsystem).")
@_human
def init_cmd(root_flag: bool, subsystem: str | None, namespace: str | None, human: bool) -> None:
    """Initialize .bounds/ structure, or add a subsystem."""
    human = _interactive_human(human)  # interactive setup action: announce in a terminal

    def go() -> None:
        if not root_flag and not subsystem:
            raise errors.BoundsError(
                errors.E_USAGE,
                "nothing to initialize",
                fix="pass --root to scaffold .bounds/, or --subsystem <name> to add one",
            )
        if namespace and not subsystem:
            raise errors.BoundsError(
                errors.E_USAGE,
                "--namespace requires --subsystem",
                fix="pass --subsystem <name> together with --namespace <ns>",
            )
        # Guard before any path is built: a subsystem name is interpolated into
        # `<MANIFESTS_DIR>/<name>.yaml`, so an unvalidated name like `../../tmp/x` would
        # write OUTSIDE .bounds/. relative_to() is lexical and would NOT catch it.
        if subsystem and not config.is_valid_subsystem_name(subsystem):
            raise errors.BoundsError(
                errors.E_USAGE,
                f"invalid subsystem name {subsystem!r}",
                fix="use only letters, digits, '-' and '_'",
            )
        existing = manifest_loader.find_root(Path.cwd())
        project = existing or Path.cwd()
        bounds_dir = project / config.BOUNDS_DIR
        created: list[str] = []
        skipped: list[str] = []

        if root_flag:
            bounds_dir.mkdir(parents=True, exist_ok=True)
            root_file = bounds_dir / config.ROOT_FILE
            rel = root_file.relative_to(project).as_posix()
            if root_file.exists():
                skipped.append(rel)
            else:
                root_file.write_text(
                    _ROOT_TEMPLATE.format(version=config.SCHEMA_VERSION, project=project.name),
                    encoding="utf-8",
                )
                created.append(rel)
            # Gitignore the regenerable, binary cache (.bounds/README claims it's gitignored).
            # Idempotent: report created vs already-present like root.yaml does.
            gitignore_rel = (bounds_dir / config.GITIGNORE_FILE).relative_to(project).as_posix()
            if config.ensure_bounds_gitignore(bounds_dir):
                created.append(gitignore_rel)
            else:
                skipped.append(gitignore_rel)

        result: dict = {"created": created, "skipped": skipped}

        if subsystem:
            sub_file = bounds_dir / config.MANIFESTS_DIR / f"{subsystem}.yaml"
            sub_file.parent.mkdir(parents=True, exist_ok=True)
            rel = sub_file.relative_to(project).as_posix()
            if sub_file.exists():
                skipped.append(rel)
            else:
                namespace_line = f"namespace: {namespace}\n" if namespace else ""
                sub_file.write_text(
                    _SUBSYS_TEMPLATE.format(name=subsystem, namespace_line=namespace_line),
                    encoding="utf-8",
                )
                created.append(rel)
            result["hint"] = f"add '{subsystem}' to the 'subsystems' list in {config.BOUNDS_DIR}/{config.ROOT_FILE}"

        result["bounds_dir"] = bounds_dir.relative_to(project).as_posix()
        output.emit(result, human)

    _run(human, go)


# ===========================================================================
# discover
# ===========================================================================
@main.command("discover", short_help="Auto-generate initial contracts from source")
@click.option("--apply", "do_apply", is_flag=True, default=False,
              help="Write proposed manifests to .bounds/ (default: dry-run preview).")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="Explicitly preview without writing (the default).")
@click.option("--namespace", default=None, help="Tag every discovered subsystem with this namespace.")
@click.option("--merge-into", "merge_into", multiple=True,
              help="Fold paths into one subsystem: --merge-into 'name=path1,path2' (repeatable).")
@_human
def discover_cmd(do_apply: bool, dry_run: bool, namespace: str | None,
                 merge_into: tuple[str, ...], human: bool) -> None:
    """Create initial Bounds contracts so agents have a map to query."""
    human = _interactive_human(human)  # interactive onboarding action: announce in a terminal

    def go() -> None:
        if do_apply and dry_run:
            raise errors.BoundsError(
                errors.E_USAGE, "pass either --apply or --dry-run, not both",
                fix="omit both for a preview, or pass --apply to write",
            )
        merges: list[tuple[str, list[str]]] = []
        for spec in merge_into:
            if "=" not in spec:
                raise errors.BoundsError(
                    errors.E_USAGE, f"bad --merge-into value {spec!r}",
                    fix="use --merge-into 'name=path1,path2'",
                )
            name, _, paths = spec.partition("=")
            merges.append((name.strip(), [p.strip() for p in paths.split(",") if p.strip()]))
        root = manifest_loader.find_root(Path.cwd()) or Path.cwd()
        with _progress("scanning repo..."):
            payload = discover_mod.run_discover(
                root, apply=do_apply, namespace=namespace, merges=merges,
            )
        output.emit(payload, human)

    _run(human, go)


# ===========================================================================
# calibrate
# ===========================================================================
@main.command("calibrate", short_help="Realign contracts with source after code changes")
@click.option("--subsystem", default=None, help="Calibrate only this subsystem.")
@click.option("--apply", "do_apply", is_flag=True, default=False,
              help="Write the proposed reconciliation to the manifests (default: diff only).")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="Explicitly show the diff without writing (the default).")
@click.option("--check", "do_check", is_flag=True, default=False,
              help="Exit non-zero on NEW drift above the committed baseline (CI gate; never writes).")
@click.option("--dump-baseline", "do_dump", is_flag=True, default=False,
              help="Record current drift as the accepted baseline in .bounds/drift-baseline.json.")
@click.option("--prune-unknown", "do_prune", is_flag=True, default=False,
              help="With --apply, also remove consumes edges that name a non-existent subsystem "
                   "(stale/typo'd refs that keep validate reporting 'unresolved'). Off by default "
                   "so a genuine forward reference survives.")
@_human
def calibrate_cmd(subsystem: str | None, do_apply: bool, dry_run: bool,
                  do_check: bool, do_dump: bool, do_prune: bool, human: bool) -> None:
    """Keep contracts aligned with extracted source after code changes."""
    # Diff/apply/dump-baseline are interactive actions → announce in a terminal; --check is a
    # CI gate consumed by automation → keep it JSON-default (it still honors explicit --human).
    human = human if do_check else _interactive_human(human)

    def go() -> None:
        # The four modes are mutually exclusive: diff (default) / apply / check / dump-baseline.
        chosen = [n for n, on in
                  (("--apply", do_apply), ("--check", do_check), ("--dump-baseline", do_dump)) if on]
        if len(chosen) > 1 or (do_apply and dry_run):
            raise errors.BoundsError(
                errors.E_USAGE, "pass at most one of --apply / --check / --dump-baseline",
                fix="omit all for a diff, --apply to write, --check to gate, --dump-baseline to record",
            )
        root = _require_root()
        if do_dump:
            with _progress("calibrating..."):
                baseline = calibrate_mod.dump_baseline(root, subsystem=subsystem)
            output.emit(baseline, human)
            return
        if do_check:
            with _progress("calibrating..."):
                payload = calibrate_mod.check_drift(root, subsystem=subsystem)
            output.emit(payload, human)
            sys.exit(config.EXIT_OK if payload["ok"] else config.EXIT_BLOCKED)
        with _progress("calibrating..."):
            payload = calibrate_mod.run_calibrate(
                root, subsystem=subsystem, apply=do_apply, prune_unknown=do_prune
            )
        output.emit(payload, human)

    _run(human, go)


# ===========================================================================
# agent
# ===========================================================================
_AGENT_FLAGS = ["claude", "codex", "opencode", "gemini", "copilot", "cursor", "aider", "windsurf"]


def _agent_selectors(fn):
    for key in reversed(_AGENT_FLAGS):
        fn = click.option(f"--{key}", key, is_flag=True, default=False,
                          help=f"Limit --sync/--check to {key}.")(fn)
    return fn


def _set_invocation_level(project_root: Path, level: str) -> None:
    """Persist ``agentsync.invocation`` in root.yaml (preserving other keys) for `agent --invocation`.

    A targeted config write so a user can change how hard agents are pushed toward Bounds without
    hand-editing YAML. Round-trips through PyYAML like ``discover --apply`` does (comments are not
    preserved — consistent with that existing path). Requires an initialized project.
    """
    import yaml

    root_file = config.config_dir(project_root) / config.ROOT_FILE
    # Defense-in-depth: the `agent --invocation` caller already verified `find_root` is non-None, so
    # this guard is effectively unreachable from there — but it's kept for any standalone/direct
    # caller (and guards the file, not just the dir). Don't delete it as dead code.
    if not root_file.is_file():
        raise errors.BoundsError(
            errors.E_MANIFEST_NOT_FOUND,
            "no .bounds/root.yaml to configure",
            fix="run 'bounds init --root' first, then set the invocation level",
        )
    try:
        raw = yaml.safe_load(root_file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise errors.BoundsError(
            errors.E_MANIFEST_PARSE_ERROR,
            f"could not parse .bounds/root.yaml: {exc}",
            fix="fix the YAML syntax in .bounds/root.yaml, then retry",
        ) from exc
    if not isinstance(raw, dict):
        raw = {}
    agentsync_cfg = dict(raw.get("agentsync") or {})
    agentsync_cfg["invocation"] = level
    raw["agentsync"] = agentsync_cfg
    root_file.write_text(
        yaml.safe_dump(raw, sort_keys=False, default_flow_style=False), encoding="utf-8"
    )


def _apply_invocation_level(invocation: str, do_detect: bool, do_check: bool) -> None:
    """Validate and persist `agent --invocation`: a write action that implies (and forces) `--sync`.

    Raises ``E_USAGE`` if combined with a read-only mode, or ``E_MANIFEST_NOT_FOUND`` when the
    project isn't initialized. On success it writes the level to root.yaml; the caller then syncs.
    """
    if do_detect or do_check:
        raise errors.BoundsError(
            errors.E_USAGE,
            "--invocation sets the level and re-syncs; it can't combine with --detect/--check",
            fix="run 'bounds agent --invocation <level>' on its own",
        )
    inv_root = manifest_loader.find_root(Path.cwd())
    if inv_root is None:
        raise errors.BoundsError(
            errors.E_MANIFEST_NOT_FOUND,
            "no .bounds/ directory found in this or any parent directory",
            fix="run 'bounds init --root' first, then set the invocation level",
        )
    _set_invocation_level(inv_root, invocation)


@main.command("agent", short_help="Wire coding agents to query Bounds first")
@click.option("--sync", "do_sync", is_flag=True, default=False,
              help="Write the AGENTS.md contract + per-agent config files.")
@click.option("--detect", "do_detect", is_flag=True, default=False,
              help="List which coding agents are present in this project (the default).")
@click.option("--check", "do_check", is_flag=True, default=False,
              help="Verify detected agents have an up-to-date Bounds config.")
@click.option("--invocation", "invocation", type=click.Choice(["off", "nudge", "strict"]),
              default=None,
              help="Set how hard agents are pushed to query Bounds first, then sync: off (advisory "
                   "files only), nudge (gentle reminder hook), strict (pause before a broad search). "
                   "Writes root.yaml + re-syncs.")
@click.option("--all", "want_all", is_flag=True, default=False,
              help="Wire every supported agent without prompting (skips the interactive picker).")
@_agent_selectors
@_human
def agent_cmd(do_sync: bool, do_detect: bool, do_check: bool, invocation: str | None,
              want_all: bool, human: bool, **selectors: bool) -> None:
    """Teach coding agents (Claude, Codex, Gemini, Cursor, …) to query Bounds first.

    Pick at most one mode. Bare 'bounds agent' runs the read-only --detect, so it is always
    safe to type to see what's present:

    \b
      bounds agent                 list which agents this repo has (read-only; the default)
      bounds agent --sync          write AGENTS.md + each selected agent's config
      bounds agent --check         verify wiring is current (CI-friendly; JSON by default)
      bounds agent --invocation X  set off|nudge|strict (how hard to push agents to Bounds), re-sync

    '--sync' in a terminal asks which tools to wire (pre-checked = detected); '--all' or an
    explicit '--claude'/'--codex'/… selector skips the prompt. AGENTS.md is always written.
    """
    # --sync/--detect are interactive actions → announce in a terminal; --check is a CI gate →
    # keep it JSON-default (still honors explicit --human).
    human = human if do_check else _interactive_human(human)

    def go() -> None:
        nonlocal do_sync
        # --invocation sets the level in root.yaml, then re-syncs to apply it (write/refresh/remove
        # the harness hook). It is a write action that implies --sync.
        if invocation is not None:
            _apply_invocation_level(invocation, do_detect, do_check)
            do_sync = True

        modes = [m for m, on in (("sync", do_sync), ("detect", do_detect), ("check", do_check)) if on]
        if len(modes) > 1:
            raise errors.BoundsError(
                errors.E_USAGE, "pass at most one of --sync, --detect, --check",
                fix="run 'bounds agent --sync' to wire agents, or bare 'bounds agent' to list them",
            )
        # Bare `bounds agent` (no mode flag) defaults to the read-only detect — like every other
        # top-level command, it does something useful with no arguments instead of erroring.
        mode = modes[0] if modes else "detect"
        only = None if want_all else ({k for k in _AGENT_FLAGS if selectors.get(k)} or None)
        root = manifest_loader.find_root(Path.cwd()) or Path.cwd()
        # Interactive --sync with no tools chosen up front: ask which to wire (pre-checked =
        # detected) instead of writing the whole kitchen sink. Piped/CI runs (non-TTY), an
        # explicit selector, or --all skip the prompt — the canonical AGENTS.md is always written.
        if mode == "sync" and only is None and not want_all and sys.stdout.isatty():
            detected = set(agentsync.run_agent(root, mode="detect").get("detected", []))
            only = _prompt_agent_selection(_AGENT_FLAGS, detected)
        payload = agentsync.run_agent(root, mode=mode, only=only)
        output.emit(payload, human)

    _run(human, go)


def _prompt_agent_selection(available: list[str], detected: set[str]) -> set[str] | None:
    """Interactive checklist for `agent --sync`: which tools to wire.

    Returns the chosen agent set, or ``None`` to mean "all" (run_agent's no-filter sentinel).
    Pressing Enter accepts the detected tools (or all, when none were detected) so the common
    case is one keystroke; a typo'd/empty selection falls back to "all" rather than writing
    nothing. The canonical ``AGENTS.md`` is written regardless of the choice.
    """
    click.echo("Which AI tools should Bounds wire? (AGENTS.md is always written.)")
    for i, key in enumerate(available, 1):
        mark = "  [detected]" if key in detected else ""
        click.echo(f"  {i}. {key}{mark}")
    default_hint = ", ".join(sorted(detected)) if detected else "all"
    raw = click.prompt(
        f"Enter names/numbers (comma-separated), 'all', or Enter for [{default_hint}]",
        default="", show_default=False,
    ).strip()
    if not raw:
        return set(detected) if detected else None
    if raw.lower() == "all":
        return None
    chosen: set[str] = set()
    for tok in raw.replace(",", " ").split():
        if tok.isdigit() and 1 <= int(tok) <= len(available):
            chosen.add(available[int(tok) - 1])
        elif tok in available:
            chosen.add(tok)
    return chosen or None  # bad input → fall back to all rather than writing nothing


@main.command("agent-hook", hidden=True, short_help="(internal) harness hook entry point")
def agent_hook_cmd() -> None:
    """Internal: the entry point wired into a harness hook by ``bounds agent --invocation``.

    Reads one harness hook event (JSON on stdin) and writes the hook-protocol response (JSON on
    stdout, or nothing for a no-op). It deliberately does NOT use the normal ``_run`` wrapper or the
    JSON-first error contract: a hook must NEVER break the agent's turn, so this always exits 0 and
    emits hook-protocol JSON only — any error degrades to an empty (allow / no-op) response.
    """
    import json

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        payload = {}
    try:
        result = agenthook.run_hook(payload if isinstance(payload, dict) else {})
    except Exception:  # noqa: BLE001 - defense in depth; run_hook already fails open.
        result = {}
    if result:
        sys.stdout.write(json.dumps(result))
    sys.exit(config.EXIT_OK)


# ===========================================================================
# ci
# ===========================================================================
@main.command("ci", short_help="Install drift/boundary gates in CI")
@click.option("--install", "do_install", is_flag=True, default=False,
              help="Generate CI config for the selected (or auto-detected) provider.")
@click.option("--github", "want_github", is_flag=True, default=False,
              help="Install the GitHub Actions workflow (.github/workflows/bounds.yml).")
# --action is the deprecated former name for --github; kept as a hidden alias for
# back-compat so existing scripts/docs don't break. Prefer --github.
@click.option("--action", "want_action_alias", is_flag=True, default=False, hidden=True)
@click.option("--gitlab", "want_gitlab", is_flag=True, default=False,
              help="Install the GitLab CI job (.gitlab-ci.yml).")
@click.option("--precommit", "want_precommit", is_flag=True, default=False,
              help="Install the local pre-commit hook (.pre-commit-config.yaml). Opt-in; orthogonal to provider.")
@click.option("--all", "want_all", is_flag=True, default=False,
              help="Install all three targets (GitHub + GitLab + pre-commit).")
@_human
def ci_cmd(do_install: bool, want_github: bool, want_action_alias: bool, want_gitlab: bool,
           want_precommit: bool, want_all: bool, human: bool) -> None:
    """Install drift/boundary gates so the agent workflow is enforced in CI.

    Pick a provider: ``--github`` or ``--gitlab`` (add ``--precommit`` for a local hook,
    or ``--all`` for everything). With no provider flag, Bounds auto-detects the one CI
    host this repo already uses; if it can't tell (no or both markers), it asks you to
    pick instead of installing all three.
    """
    human = _interactive_human(human)  # interactive setup action: announce in a terminal

    def go() -> None:
        if not do_install:
            raise errors.BoundsError(
                errors.E_USAGE, "ci needs --install",
                fix="run 'bounds ci --install' (pick --github/--gitlab, add --precommit, or --all)",
            )

        want_github_eff = want_github or want_action_alias  # --action is the legacy alias
        root = manifest_loader.find_root(Path.cwd()) or Path.cwd()

        # Resolve the FINAL explicit target set here; run_ci_install never expands an
        # empty/missing selection to "all" — so a stray host config is never dumped.
        targets: set[str] = set()
        detected: list[str] = []
        if want_all:
            targets = set(ciconfig.ALL_TARGETS)
        else:
            if want_github_eff:
                targets.add("action")
            if want_gitlab:
                targets.add("gitlab")
            if want_precommit:
                targets.add("precommit")
            # No explicit provider chosen (precommit-only is fine and stays as-is) → try
            # to auto-detect the single provider this repo uses.
            no_provider_chosen = not (want_github_eff or want_gitlab)
            if no_provider_chosen and not want_precommit:
                found = ciconfig.detect_ci_provider(root)
                detected = sorted(found)
                if len(found) == 1:
                    targets |= found
                else:
                    # Zero or both markers: ambiguous. Guide the user to choose rather
                    # than silently installing all three (the old footgun).
                    raise errors.BoundsError(
                        errors.E_USAGE,
                        "Couldn't determine your CI provider"
                        + (f" (detected markers for: {', '.join(detected)})" if detected else " (no CI markers found)")
                        + ".",
                        fix="Pass --github or --gitlab (add --precommit for a local hook, "
                            "or --all for everything).",
                    )

        payload = ciconfig.run_ci_install(root, targets=targets)
        if detected:
            payload["detected"] = detected  # which provider auto-detect picked
        output.emit(payload, human)

    _run(human, go)


# ===========================================================================
# cache
# ===========================================================================
@main.command("cache", short_help="Manage the binary extraction cache")
@click.option("--migrate", "do_migrate", is_flag=True, default=False,
              help="Convert a legacy state.json cache to the binary cache.db.")
@click.option("--prune", "do_prune", is_flag=True, default=False,
              help="Drop cache rows whose source file no longer exists.")
@click.option("--inspect", "do_inspect", is_flag=True, default=False,
              help="Show a token-lean cache summary (counts only, never symbol dumps).")
@_human
def cache_cmd(do_migrate: bool, do_prune: bool, do_inspect: bool, human: bool) -> None:
    """Manage the binary extraction cache (.bounds/cache.db)."""
    human = _interactive_human(human)  # interactive maintenance action: announce in a terminal

    def go() -> None:
        selected = [f for f, on in
                    (("migrate", do_migrate), ("prune", do_prune), ("inspect", do_inspect)) if on]
        if len(selected) != 1:
            raise errors.BoundsError(
                errors.E_USAGE,
                "pass exactly one of --migrate, --prune, --inspect",
                fix="e.g. 'bounds cache --inspect' to summarize, '--migrate' to convert state.json",
            )
        root = _require_root()
        if do_migrate:
            payload = cache_store.migrate_json_to_sqlite(root)
        elif do_prune:
            payload = cache_store.prune_missing(root)
        else:
            payload = cache_store.inspect(root)
        output.emit(payload, human)

    _run(human, go)


# ===========================================================================
# upgrade
# ===========================================================================
@main.command("upgrade", short_help="Upgrade a stale Bounds CLI via pipx")
@click.option("--ref", "ref", default="main", show_default=True,
              help="Git ref to install from when upgrading from GitHub.")
@click.option("--local", "local", type=click.Path(path_type=Path, file_okay=False, dir_okay=True, exists=True),
              default=None, help="Install editable from a local Bounds clone instead of GitHub.")
@click.option("--dry-run", is_flag=True, default=False, help="Print the upgrade command without running it.")
@_human
def upgrade_cmd(ref: str, local: Path | None, dry_run: bool, human: bool) -> None:
    """Upgrade a stale Bounds CLI through pipx."""
    # `upgrade` is an interactive maintenance action a person runs, not data an agent
    # consumes — so default to the human announcement in a terminal, and only fall back to
    # the JSON contract when piped/redirected (an agent or script captures non-TTY output).
    show_human = _interactive_human(human)

    def go() -> None:
        # dry-run does no real work, so there is nothing to spin on; otherwise reuse the
        # shared progress seam (no-op when stderr isn't a TTY, like every other command).
        with _progress("upgrading bounds...") if not dry_run else nullcontext():
            payload = upgrade_mod.run_upgrade(ref=ref, local=local, dry_run=dry_run)
        output.emit(payload, show_human)
        sys.exit(config.EXIT_OK if payload.get("ok") else config.EXIT_BLOCKED)

    _run(show_human, go)


# ===========================================================================
# upgrade-check
# ===========================================================================
@main.command("upgrade-check", short_help="Check for a newer Bounds release")
@_human
def upgrade_check_cmd(human: bool) -> None:
    """Check whether a newer Bounds release is available (opt-in; makes a network call)."""

    # Interactive check: human announcement in a terminal, JSON when piped (see `upgrade`).
    show_human = _interactive_human(human)

    def go() -> None:
        # Informational only: being outdated is never an error, and the check fails
        # soft when offline, so this command always exits 0.
        output.emit(update_check.check(), show_human)

    _run(show_human, go)


if __name__ == "__main__":  # pragma: no cover
    main()
