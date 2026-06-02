"""End-to-end: discover then validate a TS package leaves ZERO structural drift (BOUNDS-008).

`bounds discover` writes each exported symbol into a subsystem's `exposes`; `bounds validate`
must re-detect every one of those names as exported by the *same* extractor. When the extractor
under-detected TS type/re-export forms (or mislabeled enum/namespace kinds), discover-declared
names vanished at validate time and produced false `E_STRUCTURAL_DRIFT`. This drives the real
commands over a type-heavy package to prove discover and validate stay symmetric.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from bounds import errors
from bounds.discover import run_discover
from bounds.validate import engine

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git_init(path) -> None:
    """Initialize a git repo so validate's git-aware file selection has a repo to diff."""
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


def _write_ts_package(tmp_path) -> None:
    """A type-heavy TS subsystem exercising every BOUNDS-008 export form (5 files = kept candidate)."""
    pkg = tmp_path / "src" / "core"
    pkg.mkdir(parents=True)

    # Every locally-resolvable export form lives here.
    (pkg / "model.ts").write_text(
        "export type Id = string;\n"
        "export interface User { id: Id; }\n"
        "export enum Role { Admin, Member }\n"
        "export const enum Flag { On, Off }\n"
        "export class Repo {}\n"
        "export abstract class Base {}\n"
        "export function build(a: number): number;\n"
        "export function build(a: string): string;\n"
        "export function build(a: unknown): unknown { return a; }\n"
        "export const LIMIT = 10, OFFSET = 0;\n"
        "export let counter = 0;\n"
        "export default function main() {}\n"
    )

    # Local re-export (no `from`): kinds resolve from in-file declarations.
    (pkg / "facade.ts").write_text(
        "class Service {}\n"
        "const helper = 1;\n"
        "export { Service, helper };\n"
        "export { Service as PublicService };\n"
    )

    # Cross-module + type-only + namespace + star re-exports through a barrel.
    (pkg / "index.ts").write_text(
        'export { Repo } from "./model";\n'
        'export { Role as RoleEnum } from "./model";\n'
        'export type { User } from "./model";\n'
        'export * as models from "./model";\n'
        'export * from "./facade";\n'
    )

    # Pad files so the dir scores as a kept candidate (not count-dropped).
    for i in range(3):
        (pkg / f"util{i}.ts").write_text(f"export function util{i}() {{ return {i}; }}\n")


@requires_git
def test_discover_then_validate_has_zero_structural_drift(tmp_path):
    """A fresh discover --apply followed by validate yields no E_STRUCTURAL_DRIFT for any TS form."""
    _git_init(tmp_path)
    _write_ts_package(tmp_path)

    discovered = run_discover(tmp_path, apply=True)
    assert discovered["applied"] is True

    report = engine.run(tmp_path, mode="full", persist=False)
    drift = [i for i in report.issues if i.code == errors.E_STRUCTURAL_DRIFT]
    assert drift == [], f"unexpected structural drift: {[i.message for i in drift]}"


@requires_git
def test_every_discovered_name_is_re_detected_by_validate(tmp_path):
    """Symmetry, name-for-name: every name discover wrote to `exposes` is in validate's actual surface."""
    _git_init(tmp_path)
    _write_ts_package(tmp_path)

    discovered = run_discover(tmp_path, apply=True)
    core = next(c for c in discovered["candidates"] if c["name"] == "core")
    declared = {e["name"] for e in core["exposes"]}
    # Sanity: the type-heavy forms actually made it into exposes (not silently dropped).
    assert {"Id", "User", "Role", "Flag", "Repo", "build", "models", "RoleEnum"} <= declared

    # Rebuild validate's view and confirm each declared name is an actual export.
    report = engine.run(tmp_path, mode="full", persist=False)
    drift_names = {
        i.message.split("'")[1]
        for i in report.issues
        if i.code == errors.E_STRUCTURAL_DRIFT and "declares" in i.message
    }
    assert declared.isdisjoint(drift_names), f"declared-but-missing at validate: {declared & drift_names}"


@requires_git
def test_star_reexport_name_not_declared(tmp_path):
    """`export * from "./m"` has no enumerable names, so discover must not invent an unresolvable one."""
    _git_init(tmp_path)
    _write_ts_package(tmp_path)
    discovered = run_discover(tmp_path, apply=True)
    core = next(c for c in discovered["candidates"] if c["name"] == "core")
    names = {e["name"] for e in core["exposes"]}
    # No symbol named after the star target ("facade") leaked into exposes.
    assert "facade" not in names
