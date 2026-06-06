"""Maintenance-related CLI command implementations.

Commands: edit, cache, upgrade, upgrade-check.
"""

from __future__ import annotations

from pathlib import Path

from . import util
from ..shared import config, errors, output
from ..maintenance import update as update_mod, upgrade as upgrade_mod
from ..shared.cache import store as cache_store
from ..core.manifest import loader as manifest_loader

def edit_cmd(subsystem: str, description: str | None, human: bool) -> None:
    """Update subsystem metadata through the CLI."""
    human = util.interactive_human(human)

    def go() -> None:
        chosen = [name for name, val in (("--description", description),) if val is not None]
        if len(chosen) != 1:
            raise errors.BoundsError(
                errors.E_USAGE,
                "pass exactly one metadata field to update",
                fix="example: bounds edit logging --description 'Structured operational logging'",
            )
        root = util.require_root()
        _rootm, subs, _schema_issues = manifest_loader.load_all(root)
        sub = subs.get(subsystem)
        if sub is None:
            raise errors.BoundsError(
                errors.E_SUBSYSTEM_NOT_FOUND, f"unknown subsystem {subsystem!r}",
                fix="run 'bounds list' to see available subsystems"
            )

        import yaml
        manifest_path = root / sub.source_path
        text = manifest_path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
        
        if description is not None:
            data["description"] = description

        manifest_path.write_text(
            yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
            encoding="utf-8"
        )
        output.emit({
            "mode": "edit",
            "subsystem": subsystem,
            "updated": chosen,
            "description": data.get("description"),
        }, human)

    util.run_wrapped(human, go)

def cache_cmd(do_migrate: bool, do_prune: bool, do_inspect: bool, human: bool) -> None:
    """Manage the binary extraction cache (.bounds/cache.db)."""
    human = util.interactive_human(human)

    def go() -> None:
        selected = [f for f, on in
                    (("migrate", do_migrate), ("prune", do_prune), ("inspect", do_inspect)) if on]
        if len(selected) != 1:
            raise errors.BoundsError(
                errors.E_USAGE,
                "pass exactly one of --migrate, --prune, --inspect",
                fix="e.g. 'bounds cache --inspect' to summarize, '--migrate' to convert state.json",
            )
        root = util.require_root()
        if do_migrate:
            payload = cache_store.migrate_json_to_sqlite(root)
        elif do_prune:
            payload = cache_store.prune_missing(root)
        else:
            payload = cache_store.inspect(root)
        output.emit(payload, human)

    util.run_wrapped(human, go)

def upgrade_cmd(do_check: bool, force: bool, dry_run: bool, human: bool) -> None:
    """Check for or install a newer version of the Bounds CLI."""
    human = util.interactive_human(human)

    def go() -> None:
        if do_check:
            payload = update_mod.check()
        else:
            with util.progress("upgrading bounds...") if not dry_run else util.nullcontext():
                payload = upgrade_mod.run_upgrade(dry_run=dry_run)
        output.emit(payload, human)
        if not do_check:
            import sys
            sys.exit(config.EXIT_OK if payload.get("ok") else config.EXIT_BLOCKED)

    util.run_wrapped(human, go)
