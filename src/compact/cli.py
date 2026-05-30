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


def _run(human: bool, fn):
    """Execute a command body, turning CompactError into a structured error + exit 2."""
    try:
        fn()
    except errors.CompactError as err:
        output.emit(err.to_dict(), human)
        sys.exit(config.EXIT_FATAL)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="compact")
def main() -> None:
    """Compact — AI-native codebase understanding via subsystem boundary manifests."""


# ===========================================================================
# list
# ===========================================================================
@main.command("list")
@_human
def list_cmd(human: bool) -> None:
    """Discover subsystems."""

    def go() -> None:
        root = _require_root()
        rootm, subs, _ = manifest_loader.load_all(root)
        payload = {
            "project": rootm.project,
            "subsystems": [
                {
                    "name": subs[n].name,
                    "role": subs[n].role,
                    "criticality": subs[n].criticality,
                    "description": subs[n].description,
                    "exposes": len(subs[n].exposes),
                    "consumes": len(subs[n].consumes),
                    "consumed_by": sorted(subs[n].consumed_by),
                }
                for n in sorted(subs)
            ],
        }
        output.emit(payload, human)

    _run(human, go)


# ===========================================================================
# describe
# ===========================================================================
@main.command("describe")
@click.argument("name")
@click.option("--deep", is_flag=True, default=False, help="Include Tier-3 LLM enrichment (roadmap).")
@_human
def describe_cmd(name: str, deep: bool, human: bool) -> None:
    """Return one subsystem compact as JSON."""

    def go() -> None:
        root = _require_root()
        _, subs, _ = manifest_loader.load_all(root)
        if name not in subs:
            raise errors.CompactError(
                errors.E_SUBSYSTEM_NOT_FOUND,
                f"subsystem '{name}' not found",
                fix=f"known subsystems: {sorted(subs)}; or run 'compact init --subsystem {name}'",
            )
        sub = subs[name]
        payload = sub.to_dict()

        # ---- Merge Tier-1 extraction into Tier-2 declared data ----
        exts = supported_extensions()
        extracted_symbols: dict[str, str] = {}  # symbol_name -> file_path
        owned_files: list[str] = []

        for raw_path in sub.paths or []:
            base = root / raw_path
            if base.is_dir():
                for f in sorted(base.rglob("*")):
                    if f.is_file() and f.suffix in exts:
                        rel = f.relative_to(root).as_posix()
                        owned_files.append(rel)
                        adapter = get_adapter(rel)
                        if adapter is None:
                            continue
                        try:
                            source = f.read_bytes()
                        except OSError:
                            continue
                        result = adapter.extract(rel, source)
                        if result and result.error is None:
                            for sym in result.symbols:
                                if sym.exported:
                                    extracted_symbols[sym.name] = rel
            elif base.is_file() and base.suffix in exts:
                rel = base.relative_to(root).as_posix()
                owned_files.append(rel)
                adapter = get_adapter(rel)
                if adapter is not None:
                    try:
                        source = base.read_bytes()
                    except OSError:
                        continue
                    result = adapter.extract(rel, source)
                    if result and result.error is None:
                        for sym in result.symbols:
                            if sym.exported:
                                extracted_symbols[sym.name] = rel
            else:
                # Treat as glob pattern relative to project root
                for f in sorted(root.glob(raw_path)):
                    if f.is_file() and f.suffix in exts:
                        rel = f.relative_to(root).as_posix()
                        if rel not in owned_files:
                            owned_files.append(rel)
                        adapter = get_adapter(rel)
                        if adapter is None:
                            continue
                        try:
                            source = f.read_bytes()
                        except OSError:
                            continue
                        result = adapter.extract(rel, source)
                        if result and result.error is None:
                            for sym in result.symbols:
                                if sym.exported:
                                    extracted_symbols[sym.name] = rel

        # Also check sub.files (the explicit files list)
        for raw in sub.files or []:
            f = root / raw
            if f.is_file() and f.suffix in exts:
                rel = f.relative_to(root).as_posix()
                if rel not in owned_files:
                    owned_files.append(rel)

        for expose in payload.get("exposes", []):
            ename = expose.get("name", "")
            if ename in extracted_symbols:
                expose["file"] = extracted_symbols[ename]
                expose["verified"] = True
            else:
                expose["verified"] = False

        payload["files"] = sorted(owned_files)

        try:
            report = validate_engine.run(root, mode="quick", persist=False)
            payload["validation_status"] = report.status
        except errors.CompactError:
            payload["validation_status"] = "unresolved"
        if deep:
            payload["semantic"] = {"note": "LLM enrichment (Tier 3) not enabled in this build"}
        output.emit(payload, human)

    _run(human, go)


# ===========================================================================
# validate
# ===========================================================================
@main.command("validate")
@click.option("--quick", is_flag=True, default=False, help="Git-diff incremental validation.")
@click.option("--mode", type=click.Choice(sorted(config.VALID_MODES)), default=None,
              help="Explicit validation mode (default: full).")
@click.option("--enforce", type=click.Choice(sorted(config.VALID_ENFORCE)), default=None,
              help="Override root.yaml enforce setting.")
@click.option("--base", default="HEAD", show_default=True, help="Git ref to diff against in quick mode.")
@_human
def validate_cmd(quick: bool, mode: str | None, enforce: str | None, base: str, human: bool) -> None:
    """Validate manifests against source. Defaults to full mode."""

    def go() -> None:
        root = _require_root()
        selected = "quick" if quick else (mode or "full")
        report = validate_engine.run(root, mode=selected, base=base, enforce=enforce)
        output.emit(report.to_dict(), human)
        sys.exit(config.EXIT_OK if report.ok else config.EXIT_BLOCKED)

    _run(human, go)


# ===========================================================================
# preflight
# ===========================================================================
@main.command("preflight")
@_human
def preflight_cmd(human: bool) -> None:
    """Run the 6 pre-PR structural checks (blocking)."""

    def go() -> None:
        root = _require_root()
        report = validate_engine.run(root, mode="preflight")
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
        output.emit(payload, human)
        sys.exit(config.EXIT_OK if report.ok else config.EXIT_BLOCKED)

    _run(human, go)


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
subsystems: []
'''

_SUBSYS_TEMPLATE = '''name: {name}
role: library
criticality: leaf
description: TODO describe this subsystem.
paths:
  - src/{name}
exposes: []
consumes: []
'''


@main.command("init")
@click.option("--root", "root_flag", is_flag=True, default=False, help="Initialize .compact/root.yaml.")
@click.option("--subsystem", default=None, help="Scaffold a new subsystem manifest.")
@_human
def init_cmd(root_flag: bool, subsystem: str | None, human: bool) -> None:
    """Initialize .compact/ structure, or add a subsystem."""

    def go() -> None:
        if not root_flag and not subsystem:
            raise errors.CompactError(
                errors.E_USAGE,
                "nothing to initialize",
                fix="pass --root to scaffold .compact/, or --subsystem <name> to add one",
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
                sub_file.write_text(_SUBSYS_TEMPLATE.format(name=subsystem), encoding="utf-8")
                created.append(rel)
            result["hint"] = f"add '{subsystem}' to the 'subsystems' list in {config.COMPACT_DIR}/{config.ROOT_FILE}"

        result["compact_dir"] = compact_dir.relative_to(project).as_posix()
        output.emit(result, human)

    _run(human, go)


if __name__ == "__main__":  # pragma: no cover
    main()
