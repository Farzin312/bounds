"""Maintenance-related CLI command implementations.

Commands: edit, cache, upgrade, upgrade-check.
"""

from __future__ import annotations

from pathlib import Path

from . import util
from ..shared import config, errors, output
from ..maintenance import update as update_mod, upgrade as upgrade_mod
from ..shared.cache import store as cache_store

def edit_cmd(human: bool) -> None:
    """Open the local Bounds configuration in the system editor."""
    human = util.interactive_human(human)

    def go() -> None:
        root = util.require_root()
        root_yaml = root / config.BOUNDS_DIR / config.ROOT_FILE
        if not root_yaml.is_file():
            raise errors.BoundsError(
                errors.E_MANIFEST_NOT_FOUND,
                f"root manifest not found at {root_yaml.relative_to(root)}",
                fix="run 'bounds init --root' to initialize",
            )
        import os
        import subprocess
        editor = os.environ.get("EDITOR", "vi")
        subprocess.call([editor, str(root_yaml)])
        output.emit({"mode": "edit", "path": root_yaml.relative_to(root).as_posix()}, human)

    util.run_wrapped(human, go)

def cache_cmd(do_clear: bool, do_inspect: bool, human: bool) -> None:
    """Inspect or clear the local boundary cache."""
    human = util.interactive_human(human)

    def go() -> None:
        if do_clear and do_inspect:
            raise errors.BoundsError(errors.E_USAGE, "pass at most one of --clear, --inspect")

        root = util.require_root()
        if do_clear:
            cache_store.clear_cache(root)
            payload = {"mode": "cache-clear", "cleared": True}
        else:
            payload = cache_store.cache_status(root)
        output.emit(payload, human)

    util.run_wrapped(human, go)

def upgrade_cmd(do_check: bool, force: bool, dry_run: bool, human: bool) -> None:
    """Check for or install a newer version of the Bounds CLI."""
    human = util.interactive_human(human)

    def go() -> None:
        if do_check:
            payload = update_mod.check(force=force)
        else:
            payload = upgrade_mod.run_upgrade(force=force, dry_run=dry_run)
        output.emit(payload, human)

    util.run_wrapped(human, go)
