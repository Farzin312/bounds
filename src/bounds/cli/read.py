"""Read-related CLI command implementations.

Commands: list, describe, overview, where, impact.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import util
from ..core import describe as describe_mod, locate
from ..shared import errors, output
from ..core.manifest import loader as manifest_loader

def list_cmd(namespace: str | None, human: bool) -> None:
    """List all subsystems in the project map. Read-only."""
    human = util.interactive_human(human)

    def go() -> None:
        root = util.require_root()
        rootm, subs, _schema_issues = manifest_loader.load_all(root)
        payload = describe_mod.run_list(subs, rootm, namespace=namespace)
        output.emit(payload, human)

    util.run_wrapped(human, go)

def describe_cmd(name: str | None, namespace: str | None, full: bool, deep: bool, human: bool) -> None:
    """Show one subsystem's verified public surface and tables. Read-only."""
    human = util.interactive_human(human)

    def go() -> None:
        if name and namespace:
            raise errors.BoundsError(
                errors.E_USAGE, "pass either NAME or --namespace, not both",
                fix="run 'bounds describe NAME' for one, or 'bounds describe --namespace NS' for all in a namespace"
            )
        if not name and not namespace:
            raise errors.BoundsError(
                errors.E_USAGE, "NAME or --namespace is required",
                fix="run 'bounds list' to see available subsystems, then describe one"
            )

        root = util.require_root()
        rootm, subs, _schema_issues = manifest_loader.load_all(root)
        
        if namespace:
            payload = describe_mod.run_describe_namespace(subs, namespace, root, rootm, full=full, deep=deep)
        else:
            payload = describe_mod.run_describe(subs, name, root, rootm, full=full, deep=deep)
            
        if payload is None:
            if namespace:
                raise errors.BoundsError(errors.E_USAGE, f"no subsystems found in namespace {namespace!r}")
            raise errors.BoundsError(
                errors.E_SUBSYSTEM_NOT_FOUND, f"unknown subsystem {name!r}",
                fix="run 'bounds list' to see available subsystems"
            )
        
        if deep:
            if namespace:
                for sub_payload in payload["subsystems"]:
                    sub_payload["semantic"] = {"note": "LLM enrichment (Tier 3) not enabled in this build"}
            else:
                payload["semantic"] = {"note": "LLM enrichment (Tier 3) not enabled in this build"}

        output.emit(payload, human)

    util.run_wrapped(human, go)

def overview_cmd(human: bool) -> None:
    """Show project health, coverage, and trust guidance at a glance. Read-only."""
    human = util.interactive_human(human)

    def go() -> None:
        root = util.require_root()
        rootm, subs, schema_issues = manifest_loader.load_all(root)
        with util.progress("analyzing project..."):
            payload = describe_mod.run_overview(root, rootm, subs, schema_issues)
        output.emit(payload, human)

    util.run_wrapped(human, go)

def where_cmd(symbol: str, prefix: bool, human: bool) -> None:
    """Locate the subsystem and file owning a symbol or table. Read-only."""
    human = util.interactive_human(human)

    def go() -> None:
        root = util.require_root()
        with util.progress("searching..."):
            payload = locate.run_where(root, symbol, prefix=prefix)
        output.emit(payload, human)

    util.run_wrapped(human, go)

def impact_cmd(name: str, verify: bool, include_raw: bool, human: bool) -> None:
    """transitive blast radius of changing a subsystem or table. Read-only."""
    human = util.interactive_human(human)

    def go() -> None:
        root = util.require_root()
        with util.progress("analyzing impact..."):
            payload = locate.run_impact(root, name, verify=verify, include_raw=include_raw)
        output.emit(payload, human)

    util.run_wrapped(human, go)
