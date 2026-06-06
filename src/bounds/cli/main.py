"""Command-line interface for Bounds.

Every command prints a JSON object to stdout by default and accepts ``-H/--human`` for a readable
rendering of the same data. Fatal conditions raise :class:`~bounds.errors.BoundsError`, which is
caught here, emitted as ``{"error": {...}}``, and exits with code 2. Blocking validation failures
exit 1; everything else exits 0.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from .. import __version__
from ..agents import hook as agenthook, sync as agentsync
from ..core import ciconfig
from ..shared import config, errors, output
from . import util
from ..core.manifest import loader as manifest_loader

# Import command implementations
from . import read as cli_read, drift as cli_drift, maintain as cli_maintain, setup as cli_setup

# Internal attributes mocked by tests — kept for backward compatibility.
_AGENT_FLAGS = agentsync.AGENT_KEYS
_prompt_agent_selection = cli_setup.prompt_agent_selection

__all__ = ["main"]

# ---- shared option ----
_human = click.option("--human", "-H", "human", is_flag=True, default=False,
                      help="Human-readable output instead of JSON.")

# ===========================================================================
# main entry point
# ===========================================================================

_COMMAND_GROUPS = (
    ("Set up", ("guide", "init", "discover", "agent", "sdd", "ci")),
    ("Read the map (do this before grepping source)", ("list", "describe", "overview", "where", "impact")),
    ("Catch drift", ("validate", "preflight", "calibrate", "coverage", "fix-coverage")),
    ("Maintain", ("edit", "cache", "upgrade", "upgrade-check")),
)

# Leading "\b" marks the quick-start block as pre-formatted so click does not reflow the
# command line mid-word; the prose paragraph after the blank line is allowed to wrap normally.
_HELP_EPILOG = (
    "\b\n"
    "New here? Run 'bounds guide' for a state-aware setup checklist.\n"
    "Quick start:\n"
    "  bounds init --root  ·  bounds discover --apply  ·  bounds agent --sync\n"
    "\n"
    "Already set up? 'bounds guide' also shows optional features (SDD, agent invocation)\n"
    "with their current state and the exact command to change each one.\n"
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

def _version_display(v: str) -> str:
    """Installed version with (dirty) suffix when running from source."""
    return v

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
    pass

# ---- Setup Commands ----
@main.command("guide", short_help="Setup checklist: get Bounds working in this project")
@click.option("--sdd", is_flag=True, help="Include SDD preview even if disabled.")
@_human
def guide(sdd: bool, human: bool) -> None:
    cli_setup.guide_cmd(sdd, human)

@main.command("init", short_help="Initialize .bounds/, or add a subsystem")
@click.option("--root", "root_flag", is_flag=True, default=False, help="Initialize .bounds/root.yaml.")
@click.option("--subsystem", default=None, help="Scaffold a new subsystem manifest.")
@click.option("--namespace", default=None, help="Namespace for the scaffolded subsystem (requires --subsystem).")
@click.option("--path", "paths", multiple=True,
              help="Repo-relative file/dir/glob owned by the subsystem. Repeat for multiple paths.")
@_human
def init(root_flag: bool, subsystem: str | None, namespace: str | None,
         paths: tuple[str, ...], human: bool) -> None:
    cli_setup.init_cmd(root_flag, subsystem, namespace, paths, human)

@main.command("discover", short_help="Auto-map subsystems from source")
@click.option("--apply", "do_apply", is_flag=True, default=False, help="Write the discovered manifests to disk.")
@click.option("--dry-run", is_flag=True, default=False, help="Preview changes without writing.")
@click.option("--namespace", "-n", help="Namespace for discovered subsystems.")
@click.option("--merge-into", multiple=True, help="Merge into existing: 'name=path1,path2'.")
@_human
def discover(do_apply, dry_run, namespace, merge_into, human):
    cli_setup.discover_cmd(do_apply, dry_run, namespace, merge_into, human)


@main.command("agent", short_help="Wire AI coding agents to Bounds")
@click.option("--sync", "do_sync", is_flag=True, default=False, help="Write AGENTS.md + pointer files.")
@click.option("--detect", "do_detect", is_flag=True, default=False, help="List detected agents.")
@click.option("--check", "do_check", is_flag=True, default=False, help="Verify wiring is current.")
@click.option("--invocation", type=click.Choice(["off", "nudge", "strict"]),
              help="Set how hard agents push toward Bounds first.")
@click.option("--all", "want_all", is_flag=True, default=False, help="Sync all tools, skip prompt.")
@_human
# Per-agent selectors for selective sync.
@click.option("--claude", is_flag=True, help="Sync Claude Code config.")
@click.option("--codex", is_flag=True, help="Sync OpenAI Codex (AGENTS.md).")
@click.option("--opencode", is_flag=True, help="Sync OpenCode config.")
@click.option("--gemini", is_flag=True, help="Sync Google Gemini (GEMINI.md).")
@click.option("--copilot", is_flag=True, help="Sync GitHub Copilot config.")
@click.option("--cursor", is_flag=True, help="Sync Cursor config.")
@click.option("--aider", is_flag=True, help="Sync Aider config.")
@click.option("--windsurf", is_flag=True, help="Sync Windsurf config.")
def agent(do_sync, do_detect, do_check, invocation, want_all, human, **selectors):
    """Teach coding agents (Claude, Codex, Gemini, Cursor, …) to query Bounds first."""
    cli_setup.agent_cmd(
        do_sync, do_detect, do_check, invocation, want_all, human,
        cli_setup.prompt_agent_selection, **selectors
    )

@main.command("agent-hook", hidden=True)
def agent_hook():
    """Harness-run hook for Claude Code (UserPromptSubmit/PreToolUse)."""
    agenthook.agent_hook_cmd()

@main.command("ci", short_help="Install drift/boundary gates in CI")
@click.option("--install", "do_install", is_flag=True, default=False)
@click.option("--github", is_flag=True)
@click.option("--action", "want_action_alias", is_flag=True, default=False, hidden=True)
@click.option("--gitlab", is_flag=True)
@click.option("--precommit", is_flag=True)
@click.option("--all", "want_all", is_flag=True)
@_human
def ci(do_install, github, want_action_alias, gitlab, precommit, want_all, human):
    cli_setup.ci_cmd(do_install, github, want_action_alias, gitlab, precommit, want_all, human)

@main.command("sdd", short_help="Optional SDD phase map and configuration")
@click.option("--status", "do_status", is_flag=True, default=False)
@click.option("--phase", "phase_name")
@click.option("--doctor", "do_doctor", is_flag=True, default=False)
@click.option("--enable", "do_enable", is_flag=True, default=False)
@click.option("--disable", "do_disable", is_flag=True, default=False)
@click.option("--phases", "phases_str")
@click.option("--add-phase", "add_phase")
@click.option("--remove-phase", "remove_phase")
@_human
def sdd(do_status, phase_name, do_doctor, do_enable, do_disable, phases_str, add_phase, remove_phase, human):
    cli_setup.sdd_cmd(do_status, phase_name, do_doctor, do_enable, do_disable, phases_str, add_phase, remove_phase, human)

# ---- Read Commands ----
@main.command("list", short_help="Show the subsystem map (read before grepping source)")
@click.option("--namespace", "-n", help="Only list subsystems in this namespace.")
@_human
def list_subsystems(namespace: str | None, human: bool) -> None:
    cli_read.list_cmd(namespace, human)

@main.command("describe", short_help="Show a subsystem's verified surface and dependencies")
@click.argument("name", required=False)
@click.option("--namespace", "-n", help="Describe all subsystems in a namespace.")
@click.option("--full", is_flag=True, default=False, help="Include implementation files.")
@click.option("--deep", is_flag=True, default=False, help="Include semantic LLM-enriched facts.")
@_human
def describe(name: str | None, namespace: str | None, full: bool, deep: bool, human: bool) -> None:
    cli_read.describe_cmd(name, namespace, full, deep, human)

@main.command("overview", short_help="Project health dashboard")
@_human
def overview(human: bool) -> None:
    cli_read.overview_cmd(human)

@main.command("where", short_help="Locate a symbol or table without asking an agent to grep blindly")
@click.argument("symbol")
@click.option("--prefix", is_flag=True, default=False, help="Match symbols by leading substring.")
@_human
def where(symbol: str, prefix: bool, human: bool) -> None:
    cli_read.where_cmd(symbol, prefix, human)

@main.command("impact", short_help="Blast radius of a subsystem/interface change")
@click.argument("name")
@click.option("--verify", "verify", is_flag=True, default=False, help="Cross-check against actual import graph.")
@click.option("--include-raw-queries", "include_raw", is_flag=True, default=False, help="Include heuristic raw-SQL consumers.")
@_human
def impact(name, verify, include_raw, human):
    cli_read.impact_cmd(name, verify, include_raw, human)

# ---- Drift Commands ----
@main.command("validate", short_help="Run architecture checks (drift, boundary, cycles)")
@click.option("--quick", "quick", is_flag=True, default=False, help="Skip schema extraction and coverage.")
@click.option("--include-ignored", "include_ignored", is_flag=True, default=False, help="Include .boundsignore in denominator.")
@click.option("--include-gitignored", "include_gitignored", is_flag=True, default=False, help="Include .gitignore in denominator.")
@click.option("--follow-symlinks", "follow_symlinks", is_flag=True, default=False, help="Traverse directory symlinks.")
@click.option("--fail-on-unowned", "fail_on_unowned", is_flag=True, default=False, help="Block on unmapped supported source.")
@click.option("--ci", "is_ci", is_flag=True, default=False, help="Output tab-delimited CI status.")
@_human
def validate(quick, include_ignored, include_gitignored, follow_symlinks, fail_on_unowned, is_ci, human):
    cli_drift.validate_cmd(quick, include_ignored, include_gitignored,
                           follow_symlinks, fail_on_unowned, is_ci, human)

@main.command("preflight", short_help="Architecture gate (validate + coverage)")
@click.option("--ci", "is_ci", is_flag=True, help="Output tab-delimited CI status.")
@click.option("--include-ignored", "include_ignored", is_flag=True, default=False)
@click.option("--include-gitignored", "include_gitignored", is_flag=True, default=False)
@click.option("--follow-symlinks", "follow_symlinks", is_flag=True, default=False)
@click.option("--fail-on-unowned", "fail_on_unowned", is_flag=True, default=False)
@_human
def preflight(is_ci, include_ignored, include_gitignored, follow_symlinks, fail_on_unowned, human):
    cli_drift.preflight_cmd(is_ci, include_ignored, include_gitignored,
                            follow_symlinks, fail_on_unowned, human)

@main.command("calibrate", short_help="Realign contracts with source after code changes")
@click.option("--subsystem", default=None)
@click.option("--apply", "do_apply", is_flag=True, default=False)
@click.option("--dry-run", "dry_run", is_flag=True, default=False)
@click.option("--check", "do_check", is_flag=True, default=False)
@click.option("--dump-baseline", "do_dump", is_flag=True, default=False)
@click.option("--prune-unknown", "do_prune", is_flag=True, default=False)
@click.option("--prune-missing-exports", "prune_missing_exports", is_flag=True, default=False)
@click.option("--track-interfaces", "track_interfaces", is_flag=True, default=False)
@click.option("--coarsen-interfaces", "coarsen_interfaces", is_flag=True, default=False)
@_human
def calibrate(subsystem, do_apply, dry_run, do_check, do_dump, do_prune,
              prune_missing_exports, track_interfaces, coarsen_interfaces, human):
    cli_drift.calibrate_cmd(subsystem, do_apply, dry_run, do_check, do_dump, do_prune,
                            prune_missing_exports, track_interfaces, coarsen_interfaces, human)

@main.command("coverage", short_help="Mapping coverage report (no full walk on --quick)")
@click.option("--why", "why_path", default=None, metavar="PATH")
@click.option("--summary", "summary", is_flag=True, default=False)
@_human
def coverage(why_path: str | None, summary: bool, human: bool) -> None:
    cli_drift.coverage_cmd(why_path, summary, human)

@main.command("fix-coverage", short_help="Diagnose and resolve the mapping-coverage gap")
@click.option("--explain", "explain_path", default=None, metavar="PATH")
@click.option("--auto", "do_auto", is_flag=True, default=False)
@click.option("--apply", "apply", is_flag=True, default=False)
@_human
def fix_coverage(explain_path, do_auto, apply, human):
    cli_drift.fix_coverage_cmd(explain_path, do_auto, apply, human)

# ---- Maintain Commands ----
@main.command("edit", short_help="Safely update subsystem metadata")
@click.argument("subsystem")
@click.option("--description", default=None)
@_human
def edit(subsystem, description, human):
    cli_maintain.edit_cmd(subsystem, description, human)

@main.command("cache", short_help="Manage the binary extraction cache (.bounds/cache.db)")
@click.option("--migrate", "do_migrate", is_flag=True, default=False)
@click.option("--prune", "do_prune", is_flag=True, default=False)
@click.option("--inspect", "do_inspect", is_flag=True, default=False)
@_human
def cache(do_migrate, do_prune, do_inspect, human):
    cli_maintain.cache_cmd(do_migrate, do_prune, do_inspect, human)

@main.command("upgrade", short_help="Update or check for Bounds CLI releases")
@click.option("--check", "do_check", is_flag=True, default=False)
@click.option("--force", "force", is_flag=True, default=False)
@click.option("--dry-run", "dry_run", is_flag=True, default=False)
@_human
def upgrade(do_check, force, dry_run, human):
    cli_maintain.upgrade_cmd(do_check, force, dry_run, human)

@main.command("upgrade-check", hidden=True)
@click.option("--force", is_flag=True)
@_human
def upgrade_check(force: bool, human: bool) -> None:
    cli_maintain.upgrade_cmd(True, force, False, human)

if __name__ == "__main__":
    main()
