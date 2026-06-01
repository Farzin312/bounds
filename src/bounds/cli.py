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
    agentsync,
    calibrate as calibrate_mod,
    ciconfig,
    config,
    describe as describe_mod,
    discover as discover_mod,
    errors,
    locate,
    output,
    upgrade as upgrade_mod,
    update_check,
)
from .cache import store as cache_store
from .ignore import IgnoreMatcher
from .manifest import loader as manifest_loader
from .validate import engine as validate_engine
from .validate.checks import CheckContext, check_cycles

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


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(_version_display(__version__), prog_name="bounds",
                      message="%(prog)s %(version)s")
def main() -> None:
    """Bounds — AI-first architecture context for coding agents.

    Give agents a verified, token-lean map before they search source; then catch
    architecture drift in CI. Structural extraction and validation are deterministic
    and zero-LLM.
    """


# ===========================================================================
# list
# ===========================================================================
@main.command("list")
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
@main.command("describe")
@click.argument("name", required=False)
@click.option("--namespace", default=None,
              help="Describe every subsystem in this namespace instead of one by name.")
@click.option("--deep", is_flag=True, default=False, help="Include Tier-3 LLM enrichment (roadmap).")
@click.option("--full", "full", is_flag=True, default=False,
              help="Include the full file roster and schema-object list (default shows counts).")
@_human
def describe_cmd(name: str | None, namespace: str | None, deep: bool, full: bool, human: bool) -> None:
    """Return one verified subsystem API/table contract as JSON."""

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


@main.command("validate")
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
@main.command("preflight")
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
        counts = Counter(i.code for i in report.issues)
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
@main.command("overview")
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
        payload = {
            "project": rootm.project,
            "subsystems": len(subs),
            "roles": dict(sorted(roles.items())),
            "criticality": dict(sorted(criticality.items())),
            "edges": edges,
            "cycles": [i.message for i in cycle_issues],
            "schema_issues": [i.to_dict() for i in schema_issues],
            "health": {
                "ok": not cycle_issues and schema_errors == 0,
                "schema_errors": schema_errors,
                "cycles": len(cycle_issues),
            },
        }
        output.emit(payload, human)

    _run(human, go)


# ===========================================================================
# impact
# ===========================================================================
@main.command("impact")
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
@main.command("where")
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


@main.command("init")
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
@main.command("discover")
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
@main.command("calibrate")
@click.option("--subsystem", default=None, help="Calibrate only this subsystem.")
@click.option("--apply", "do_apply", is_flag=True, default=False,
              help="Write the proposed reconciliation to the manifests (default: diff only).")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="Explicitly show the diff without writing (the default).")
@click.option("--check", "do_check", is_flag=True, default=False,
              help="Exit non-zero on NEW drift above the committed baseline (CI gate; never writes).")
@click.option("--dump-baseline", "do_dump", is_flag=True, default=False,
              help="Record current drift as the accepted baseline in .bounds/drift-baseline.json.")
@_human
def calibrate_cmd(subsystem: str | None, do_apply: bool, dry_run: bool,
                  do_check: bool, do_dump: bool, human: bool) -> None:
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
            payload = calibrate_mod.run_calibrate(root, subsystem=subsystem, apply=do_apply)
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


@main.command("agent")
@click.option("--sync", "do_sync", is_flag=True, default=False,
              help="Generate the canonical AGENTS.md contract + per-agent config files.")
@click.option("--detect", "do_detect", is_flag=True, default=False,
              help="List which coding agents are present in this project.")
@click.option("--check", "do_check", is_flag=True, default=False,
              help="Verify detected agents have a Bounds config.")
@click.option("--all", "want_all", is_flag=True, default=False,
              help="Wire every supported agent without prompting (skips the interactive picker).")
@_agent_selectors
@_human
def agent_cmd(do_sync: bool, do_detect: bool, do_check: bool, want_all: bool,
              human: bool, **selectors: bool) -> None:
    """Teach Claude, Codex, Gemini, Cursor, and other agents to query Bounds first."""
    # --sync/--detect are interactive actions → announce in a terminal; --check is a CI gate →
    # keep it JSON-default (still honors explicit --human).
    human = human if do_check else _interactive_human(human)

    def go() -> None:
        modes = [m for m, on in (("sync", do_sync), ("detect", do_detect), ("check", do_check)) if on]
        if len(modes) != 1:
            raise errors.BoundsError(
                errors.E_USAGE, "pass exactly one of --sync, --detect, --check",
                fix="e.g. 'bounds agent --sync' to generate configs, '--detect' to list agents",
            )
        only = None if want_all else ({k for k in _AGENT_FLAGS if selectors.get(k)} or None)
        root = manifest_loader.find_root(Path.cwd()) or Path.cwd()
        # Interactive --sync with no tools chosen up front: ask which to wire (pre-checked =
        # detected) instead of writing the whole kitchen sink. Piped/CI runs (non-TTY), an
        # explicit selector, or --all skip the prompt — the canonical AGENTS.md is always written.
        if modes[0] == "sync" and only is None and not want_all and sys.stdout.isatty():
            detected = set(agentsync.run_agent(root, mode="detect").get("detected", []))
            only = _prompt_agent_selection(_AGENT_FLAGS, detected)
        payload = agentsync.run_agent(root, mode=modes[0], only=only)
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


# ===========================================================================
# ci
# ===========================================================================
@main.command("ci")
@click.option("--install", "do_install", is_flag=True, default=False,
              help="Generate CI config for the detected systems.")
@click.option("--action", "want_action", is_flag=True, default=False, help="GitHub Action only.")
@click.option("--precommit", "want_precommit", is_flag=True, default=False, help="pre-commit hook only.")
@click.option("--gitlab", "want_gitlab", is_flag=True, default=False, help="GitLab CI only.")
@click.option("--all", "want_all", is_flag=True, default=False, help="All CI targets.")
@_human
def ci_cmd(do_install: bool, want_action: bool, want_precommit: bool, want_gitlab: bool,
           want_all: bool, human: bool) -> None:
    """Install drift/boundary gates so the agent workflow is enforced in CI."""
    human = _interactive_human(human)  # interactive setup action: announce in a terminal

    def go() -> None:
        if not do_install:
            raise errors.BoundsError(
                errors.E_USAGE, "ci needs --install",
                fix="run 'bounds ci --install' (optionally --action/--precommit/--gitlab/--all)",
            )
        targets: set[str] = set()
        if want_action:
            targets.add("action")
        if want_precommit:
            targets.add("precommit")
        if want_gitlab:
            targets.add("gitlab")
        if want_all:
            targets = set()  # empty => all
        root = manifest_loader.find_root(Path.cwd()) or Path.cwd()
        payload = ciconfig.run_ci_install(root, targets=targets)
        output.emit(payload, human)

    _run(human, go)


# ===========================================================================
# cache
# ===========================================================================
@main.command("cache")
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
@main.command("upgrade")
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
@main.command("upgrade-check")
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
