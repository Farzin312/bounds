"""Command-line interface for Compact.

Every command prints a JSON object to stdout by default and accepts ``-H/--human`` for a readable
rendering of the same data. Fatal conditions raise :class:`~compact.errors.CompactError`, which is
caught here, emitted as ``{"error": {...}}``, and exits with code 2. Blocking validation failures
exit 1; everything else exits 0.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import click

from . import __version__, config, errors, output
from .extract import get_adapter, supported_extensions
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
        raise errors.CompactError(
            errors.E_MANIFEST_NOT_FOUND,
            "no .compact/ directory found in this or any parent directory",
            fix="run 'compact init --root' to initialize Compact in this project",
        )
    return root


def _run(human: bool, fn, ci: bool = False):
    """Execute a command body, turning CompactError into a structured error + exit 2.

    ``ci`` is threaded from commands that expose ``--ci`` so a fatal error stays in the
    tab-delimited CI contract (one ``fatal`` line) instead of falling back to JSON.
    """
    try:
        fn()
    except errors.CompactError as err:
        output.emit(err.to_dict(), human, ci=ci)
        sys.exit(config.EXIT_FATAL)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="compact")
def main() -> None:
    """Compact — AI-native codebase understanding via subsystem boundary manifests."""


# ===========================================================================
# list
# ===========================================================================
@main.command("list")
@click.option("--namespace", default=None, help="Only list subsystems in this namespace.")
@_human
def list_cmd(namespace: str | None, human: bool) -> None:
    """Discover subsystems."""

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
def _extract_owned(root: Path, sub) -> tuple[dict[str, str], list[str]]:
    """Tier-1 extraction for one subsystem.

    Returns ``(exported_symbol_name -> owning_file, owned_files)``. A file is recorded as
    owned regardless of whether it parses (so ``files`` reflects the declared surface), but
    only successfully-extracted exported symbols populate the symbol map used to mark
    ``exposes`` entries ``verified``.
    """
    exts = supported_extensions()
    extracted_symbols: dict[str, str] = {}  # symbol_name -> file_path
    owned_files: list[str] = []

    def scan(rel: str, abs_path: Path) -> None:
        if rel not in owned_files:
            owned_files.append(rel)
        adapter = get_adapter(rel)
        if adapter is None:
            return
        try:
            source = abs_path.read_bytes()
        except OSError:
            return
        result = adapter.extract(rel, source)
        if result and result.error is None:
            for sym in result.symbols:
                if sym.exported:
                    extracted_symbols[sym.name] = rel

    for raw_path in sub.paths or []:
        base = root / raw_path
        if base.is_dir():
            for f in sorted(base.rglob("*")):
                if f.is_file() and f.suffix in exts:
                    scan(f.relative_to(root).as_posix(), f)
        elif base.is_file() and base.suffix in exts:
            scan(base.relative_to(root).as_posix(), base)
        else:  # treat as a glob relative to the project root
            for f in sorted(root.glob(raw_path)):
                if f.is_file() and f.suffix in exts:
                    scan(f.relative_to(root).as_posix(), f)

    # Also record sub.files (explicit files list) as owned, even if non-source.
    for raw in sub.files or []:
        f = root / raw
        if f.is_file() and f.suffix in exts:
            rel = f.relative_to(root).as_posix()
            if rel not in owned_files:
                owned_files.append(rel)

    return extracted_symbols, owned_files


def _describe_one(
    root: Path, sub, deep: bool, validation_status: str, entry_matcher: IgnoreMatcher
) -> dict:
    """Build the merged Tier-1 + Tier-2 describe payload for a single subsystem.

    Owned files matching a ``root.entry_points`` glob are surfaced: each is listed under
    ``entry_points`` and any ``exposes`` entry backed by one is flagged ``entry_point: true``
    (GAP #6, hybrid B+C), so an agent sees a symbol lives in a bootstrap file.
    """
    payload = sub.to_dict()
    extracted_symbols, owned_files = _extract_owned(root, sub)
    for expose in payload.get("exposes", []):
        ename = expose.get("name", "")
        if ename in extracted_symbols:
            expose["file"] = extracted_symbols[ename]
            expose["verified"] = True
            if entry_matcher and entry_matcher.matches(expose["file"]):
                expose["entry_point"] = True
        else:
            expose["verified"] = False
    payload["files"] = sorted(owned_files)
    # Always present (like ``files``) for a stable shape; the human renderer hides it when empty.
    payload["entry_points"] = sorted(
        f for f in owned_files if entry_matcher and entry_matcher.matches(f)
    )
    payload["validation_status"] = validation_status
    if deep:
        payload["semantic"] = {"note": "LLM enrichment (Tier 3) not enabled in this build"}
    return payload


def _quick_status(root: Path) -> str:
    """Compute the project's validation status via a read-only quick run (or 'unresolved')."""
    try:
        return validate_engine.run(root, mode="quick", persist=False).status
    except errors.CompactError:
        return "unresolved"


@main.command("describe")
@click.argument("name", required=False)
@click.option("--namespace", default=None,
              help="Describe every subsystem in this namespace instead of one by name.")
@click.option("--deep", is_flag=True, default=False, help="Include Tier-3 LLM enrichment (roadmap).")
@_human
def describe_cmd(name: str | None, namespace: str | None, deep: bool, human: bool) -> None:
    """Return a subsystem compact as JSON, or every compact in a namespace."""

    def go() -> None:
        if name is None and namespace is None:
            raise errors.CompactError(
                errors.E_USAGE,
                "nothing to describe",
                fix="pass a subsystem name, or --namespace <ns> to describe a group",
            )
        if name is not None and namespace is not None:
            raise errors.CompactError(
                errors.E_USAGE,
                "pass either a subsystem name or --namespace, not both",
                fix="run 'compact describe <name>' or 'compact describe --namespace <ns>'",
            )
        root = _require_root()
        rootm, subs, _ = manifest_loader.load_all(root)
        entry_matcher = IgnoreMatcher(rootm.entry_points)

        if namespace is not None:
            matched = [subs[n] for n in sorted(subs) if subs[n].namespace == namespace]
            # Validation status is project-wide; compute it once and reuse across the group.
            vstatus = _quick_status(root) if matched else ""
            payload = {
                "namespace": namespace,
                "subsystems": [_describe_one(root, s, deep, vstatus, entry_matcher) for s in matched],
            }
            output.emit(payload, human)
            return

        if name not in subs:
            raise errors.CompactError(
                errors.E_SUBSYSTEM_NOT_FOUND,
                f"subsystem '{name}' not found",
                fix=f"known subsystems: {sorted(subs)}; or run 'compact init --subsystem {name}'",
            )
        output.emit(_describe_one(root, subs[name], deep, _quick_status(root), entry_matcher), human)

    _run(human, go)


# ===========================================================================
# validate
# ===========================================================================
# File-selection + output toggles shared by validate and preflight.
_scan_options = [
    click.option("--include-ignored", is_flag=True, default=False,
                 help="Scan files normally excluded by .compactignore."),
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
    """Validate manifests against source. Defaults to full mode."""

    def go() -> None:
        root = _require_root()
        selected = "quick" if quick else (mode or "full")
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
    """Run the 6 pre-PR structural checks (blocking)."""

    def go() -> None:
        root = _require_root()
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
@click.option("--root", "root_flag", is_flag=True, default=False, help="Initialize .compact/root.yaml.")
@click.option("--subsystem", default=None, help="Scaffold a new subsystem manifest.")
@click.option("--namespace", default=None, help="Namespace for the scaffolded subsystem (requires --subsystem).")
@_human
def init_cmd(root_flag: bool, subsystem: str | None, namespace: str | None, human: bool) -> None:
    """Initialize .compact/ structure, or add a subsystem."""

    def go() -> None:
        if not root_flag and not subsystem:
            raise errors.CompactError(
                errors.E_USAGE,
                "nothing to initialize",
                fix="pass --root to scaffold .compact/, or --subsystem <name> to add one",
            )
        if namespace and not subsystem:
            raise errors.CompactError(
                errors.E_USAGE,
                "--namespace requires --subsystem",
                fix="pass --subsystem <name> together with --namespace <ns>",
            )
        existing = manifest_loader.find_root(Path.cwd())
        project = existing or Path.cwd()
        compact_dir = project / config.COMPACT_DIR
        created: list[str] = []
        skipped: list[str] = []

        if root_flag:
            compact_dir.mkdir(parents=True, exist_ok=True)
            root_file = compact_dir / config.ROOT_FILE
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
            sub_file = compact_dir / config.MANIFESTS_DIR / f"{subsystem}.yaml"
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
            result["hint"] = f"add '{subsystem}' to the 'subsystems' list in {config.COMPACT_DIR}/{config.ROOT_FILE}"

        result["compact_dir"] = compact_dir.relative_to(project).as_posix()
        output.emit(result, human)

    _run(human, go)


if __name__ == "__main__":  # pragma: no cover
    main()
