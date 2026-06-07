"""Setup-related CLI command implementations.

Commands: guide, init, discover, agent, sdd, ci.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ..agents import guide as guide_mod
from ..agents import sync as agentsync
from ..core import (
    ciconfig,
    discover as discover_mod,
    sdd as sdd_mod,
)
from ..shared import config, errors, output
from . import util
from ..core.manifest import loader as manifest_loader

def guide_cmd(sdd: bool, human: bool) -> None:
    """Show the setup steps (init → discover → agent --sync → ci) with what's already done."""
    # guide is a walkthrough → default to human view in a terminal.
    human = human or sys.stdout.isatty()
    root = Path.cwd()
    payload = guide_mod.run_guide(root, sdd=sdd)
    output.emit(payload, human)

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
{paths_block}
exposes: []
consumes: []
'''

def init_cmd(root_flag: bool, subsystem: str | None, namespace: str | None,
             paths: tuple[str, ...], human: bool) -> None:
    """Initialize .bounds/ structure, or add a subsystem."""
    human = util.interactive_human(human)

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
            
            gitignore_rel = (bounds_dir / config.GITIGNORE_FILE).relative_to(project).as_posix()
            if config.ensure_bounds_gitignore(bounds_dir):
                created.append(gitignore_rel)
            else:
                skipped.append(gitignore_rel)

        result: dict = {"created": created, "skipped": skipped}
        updated: list[str] = []

        if subsystem:
            manifest_paths = [util.clean_manifest_path(p) for p in paths] or [f"src/{subsystem}"]
            sub_file = bounds_dir / config.MANIFESTS_DIR / f"{subsystem}.yaml"
            sub_file.parent.mkdir(parents=True, exist_ok=True)
            rel = sub_file.relative_to(project).as_posix()
            if sub_file.exists():
                skipped.append(rel)
                if paths and _merge_subsystem_paths(sub_file, manifest_paths, subsystem, project):
                    updated.append(rel)
            else:
                namespace_line = f"namespace: {namespace}\n" if namespace else ""
                paths_block = "".join(f"  - {p}\n" for p in manifest_paths)
                sub_file.write_text(
                    _SUBSYS_TEMPLATE.format(
                        name=subsystem, namespace_line=namespace_line, paths_block=paths_block,
                    ),
                    encoding="utf-8",
                )
                created.append(rel)
            root_file = bounds_dir / config.ROOT_FILE
            if root_file.is_file():
                if _register_subsystem(root_file, subsystem):
                    updated.append(root_file.relative_to(project).as_posix())
                result["registered"] = subsystem
            else:
                result["hint"] = (
                    f"add '{subsystem}' to the 'subsystems' list in "
                    f"{config.BOUNDS_DIR}/{config.ROOT_FILE}"
                )

        if updated:
            result["updated"] = sorted(set(updated))
        result["bounds_dir"] = bounds_dir.relative_to(project).as_posix()
        output.emit(result, human)

    util.run_wrapped(human, go)

def _merge_subsystem_paths(
    manifest_file: Path, paths: list[str], subsystem: str, project_root: Path,
) -> bool:
    import yaml
    raw = yaml.safe_load(manifest_file.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raw = {}
    original = [str(p) for p in (raw.get("paths") or [])]
    current = list(original)
    scaffold_default = f"src/{subsystem}"
    if scaffold_default in current and not (project_root / scaffold_default).exists():
        current = [p for p in current if p != scaffold_default]
    merged = list(dict.fromkeys(current + paths))
    if merged == original:
        return False
    raw["paths"] = merged
    manifest_file.write_text(
        yaml.safe_dump(raw, sort_keys=False, default_flow_style=False), encoding="utf-8"
    )
    return True

def _register_subsystem(root_file: Path, subsystem: str) -> bool:
    import yaml
    text = root_file.read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise errors.BoundsError(
            errors.E_MANIFEST_PARSE_ERROR,
            f"could not parse .bounds/root.yaml: {exc}",
            fix="fix the YAML syntax in .bounds/root.yaml, then retry",
        ) from exc
    if not isinstance(raw, dict):
        raw = {}
    current = [str(s) for s in (raw.get("subsystems") or [])]
    if subsystem in current:
        return False
    updated = sorted(current + [subsystem])
    if _rewrite_root_subsystems_block(root_file, text, updated):
        return True
    raw["subsystems"] = updated
    root_file.write_text(yaml.safe_dump(raw, sort_keys=False, default_flow_style=False), encoding="utf-8")
    return True

def _rewrite_root_subsystems_block(root_file: Path, text: str, subsystems: list[str]) -> bool:
    lines = text.splitlines(keepends=True)
    for idx, line in enumerate(lines):
        if line.strip().startswith("subsystems:") and not line.startswith((" ", "\t")):
            start = idx + 1
            end = start
            while end < len(lines):
                stripped = lines[end].strip()
                if not stripped:
                    end += 1
                    continue
                if lines[end].startswith((" ", "\t")) or stripped.startswith("- "):
                    end += 1
                    continue
                break
            replacement = ["subsystems:\n"] + [f"  - {name}\n" for name in subsystems]
            root_file.write_text("".join(lines[:idx] + replacement + lines[end:]), encoding="utf-8")
            return True
    return False

def discover_cmd(do_apply: bool, dry_run: bool, namespace: str | None,
                 merge_into: tuple[str, ...], human: bool) -> None:
    """Create initial Bounds contracts so agents have a map to query."""
    human = util.interactive_human(human)

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
        with util.progress("scanning repo..."):
            payload = discover_mod.run_discover(
                root, apply=do_apply, namespace=namespace, merges=merges,
            )
        output.emit(payload, human)

    util.run_wrapped(human, go)

def agent_cmd(do_sync: bool, do_detect: bool, do_check: bool, invocation: str | None,
              want_all: bool, human: bool, prompt_fn, **selectors: bool) -> None:
    """Teach coding agents (Claude, Codex, Gemini, Cursor, …) to query Bounds first."""
    human = human if do_check else util.interactive_human(human)

    def go() -> None:
        nonlocal do_sync
        if invocation is not None:
            _apply_invocation_level(invocation, do_detect, do_check)
            do_sync = True

        modes = [m for m, on in (("sync", do_sync), ("detect", do_detect), ("check", do_check)) if on]
        if len(modes) > 1:
            raise errors.BoundsError(
                errors.E_USAGE, "pass at most one of --sync, --detect, --check",
                fix="run 'bounds agent --sync' to wire agents, or bare 'bounds agent' to list them",
            )
        mode = modes[0] if modes else "detect"
        only = None if want_all else ({k for k in agentsync.AGENT_KEYS if selectors.get(k)} or None)
        root = manifest_loader.find_root(Path.cwd()) or Path.cwd()
        
        if mode == "sync" and only is None and not want_all and sys.stdout.isatty():
            detected = set(agentsync.run_agent(root, mode="detect").get("detected", []))
            only = prompt_fn(list(agentsync.AGENT_KEYS), detected)
            
        payload = agentsync.run_agent(root, mode=mode, only=only)
        output.emit(payload, human)

    util.run_wrapped(human, go)

def prompt_agent_selection(available: list[str], detected: set[str]) -> set[str] | None:
    """Interactive prompt for tool selection (moved from cli.py)."""
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
    chosen = set()
    for tok in raw.replace(",", " ").split():
        if tok.isdigit() and 1 <= int(tok) <= len(available):
            chosen.add(available[int(tok) - 1])
        elif tok in available:
            chosen.add(tok)
    return chosen or None

def _apply_invocation_level(level: str, do_detect: bool, do_check: bool) -> None:
    """Validate and persist an invocation level before the implied agent sync."""
    if do_detect or do_check:
        raise errors.BoundsError(
            errors.E_USAGE,
            "--invocation sets the level and re-syncs; it can't combine with --detect/--check",
            fix="run 'bounds agent --invocation <level>' on its own",
        )
    root = util.require_root()
    import yaml
    root_path = root / config.BOUNDS_DIR / config.ROOT_FILE
    try:
        raw = yaml.safe_load(root_path.read_text(encoding="utf-8")) or {}
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
    root_path.write_text(
        yaml.safe_dump(raw, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )

def ci_cmd(do_install: bool, want_github: bool, want_action_alias: bool, want_gitlab: bool,
           want_precommit: bool, want_all: bool, human: bool) -> None:
    human = util.interactive_human(human)

    def go() -> None:
        if not do_install:
            raise errors.BoundsError(
                errors.E_USAGE,
                "nothing to install",
                fix="run 'bounds ci --install' to generate CI config",
            )
        
        want_github_eff = want_github or want_action_alias
        root = manifest_loader.find_root(Path.cwd()) or Path.cwd()
        
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
            
            no_provider_chosen = not (want_github_eff or want_gitlab)
            if no_provider_chosen and not want_precommit:
                found = ciconfig.detect_ci_provider(root)
                if len(found) == 1:
                    targets |= found
                    detected = sorted(found)
                else:
                    # Ambiguous or no provider detected.
                    raise errors.BoundsError(
                        errors.E_USAGE,
                        "ambiguous or no CI host detected; please specify a provider",
                        fix="pass one or more: --github, --gitlab (add --precommit or --all)",
                    )
        
        payload = ciconfig.run_ci_install(root, targets=targets)
        if detected:
            payload["detected"] = detected
        output.emit(payload, human)

    util.run_wrapped(human, go)

def sdd_cmd(
    do_status: bool, phase_name: str | None, do_doctor: bool,
    do_enable: bool, do_disable: bool, phases_str: str | None,
    add_phase: str | None, remove_phase: str | None, human: bool,
) -> None:
    """Map SDD phases to deterministic Bounds commands; configure optional SDD tracking."""
    _is_write = do_enable or do_disable or bool(add_phase) or bool(remove_phase)
    if _is_write:
        human = util.interactive_human(human)

    def go() -> None:
        read_chosen = [n for n, on in (
            ("--status", do_status), ("--phase", bool(phase_name)), ("--doctor", do_doctor),
        ) if on]
        write_chosen = [n for n, on in (
            ("--enable", do_enable), ("--disable", do_disable),
            ("--add-phase", bool(add_phase)), ("--remove-phase", bool(remove_phase)),
        ) if on]

        if len(read_chosen) > 1:
            raise errors.BoundsError(
                errors.E_USAGE, "pass at most one of --status, --phase, --doctor",
                fix="run 'bounds sdd' for status, 'bounds sdd --phase implement' for one phase, "
                    "or 'bounds sdd --doctor' to self-check",
            )
        if len(write_chosen) > 1:
            raise errors.BoundsError(
                errors.E_USAGE,
                f"pass at most one of --enable, --disable, --add-phase, --remove-phase; "
                f"got {' and '.join(write_chosen)}",
                fix="use --add-phase / --remove-phase for targeted updates, "
                    "or --enable --phases x,y to replace the full list",
            )
        if read_chosen and write_chosen:
            raise errors.BoundsError(
                errors.E_USAGE,
                f"cannot combine {read_chosen[0]} (read) with {write_chosen[0]} (write)",
                fix="omit the read flag to configure, or omit the write flag to inspect",
            )
        if phases_str and not do_enable:
            raise errors.BoundsError(
                errors.E_USAGE, "--phases is only valid with --enable",
                fix="use --enable --phases x,y,z, or --add-phase / --remove-phase for targeted edits",
            )

        root = util.require_root()

        if write_chosen:
            parsed_phases: list[str] | None = None
            if phases_str:
                parsed_phases = [p.strip() for p in phases_str.split(",") if p.strip()]
                invalid = [p for p in parsed_phases if p not in config.SDD_PHASES]
                if invalid:
                    raise errors.BoundsError(
                        errors.E_USAGE, f"unknown phase(s): {', '.join(invalid)}",
                        fix=f"valid phases: {', '.join(config.SDD_PHASES)}",
                    )
            for flag, val in (("--add-phase", add_phase), ("--remove-phase", remove_phase)):
                if val and val not in config.SDD_PHASES:
                    raise errors.BoundsError(
                        errors.E_USAGE, f"unknown SDD phase '{val}' for {flag}",
                        fix=f"valid phases: {', '.join(config.SDD_PHASES)}",
                    )
            payload = sdd_mod.write_sdd_config(
                root,
                enable=True if do_enable else (False if do_disable else None),
                phases=parsed_phases,
                add_phase=add_phase,
                remove_phase=remove_phase,
            )
        else:
            rootm, subs, schema_issues = manifest_loader.load_all(root)
            if do_doctor:
                with util.progress("checking SDD readiness..."):
                    payload = sdd_mod.run_sdd(root, rootm, subs, doctor=True)
            else:
                payload = sdd_mod.run_sdd(root, rootm, subs, phase_name=phase_name)
            if payload is None:
                raise errors.BoundsError(
                    errors.E_USAGE, f"unknown SDD phase '{phase_name}'",
                    fix=f"valid phases: {', '.join(config.SDD_PHASES)}",
                )
        output.emit(payload, human)

    util.run_wrapped(human, go)
