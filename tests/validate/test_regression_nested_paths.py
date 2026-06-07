"""Regression guard for BOUNDS-001 — the nested-path discover↔validate inconsistency (now fixed).

Surfaced by the 16-repo cross-language benchmark (benchmarks/results/oss-cross-language.md): when one
subsystem's path was an ancestor of another's, `validate` assigned the shared files to the
sort-earlier (parent) subsystem ("first declared owner wins"), starving the nested child of its own
files. The child then reported E_STRUCTURAL_DRIFT for every symbol it declared — symbols `describe`
simultaneously marked `[verified]` — i.e. the tool contradicted itself on unchanged source.

Fixed by making ownership *most-specific-path-wins* (`scan.resolve_owners`): the deepest declared
path owns a file, so a nested sub-package keeps its own code. These tests lock that in. See
[BOUNDS-001] in docs/known-issues.md.
"""
from __future__ import annotations

import subprocess

from bounds.shared import config, errors
from bounds.core.extract import scan
from bounds.core.validate import engine


def _git_init(path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


def _nested_project(tmp_path):
    """Two subsystems where the parent's name sorts BEFORE the child's and its path is an ancestor.

    `alpha` (path src/alpha) is an ancestor of `beta` (path src/alpha/beta), and 'alpha' < 'beta',
    so the engine's sorted first-owner-wins pass lets alpha swallow beta's file — the exact shape of
    click's `tests`/`tests/typing` and flask's `flask`/`flask/json` overlaps.
    """
    cfg = tmp_path / config.BOUNDS_DIR
    (cfg / config.MANIFESTS_DIR).mkdir(parents=True)
    (cfg / config.ROOT_FILE).write_text(
        'version: "1"\nproject: nested\nlanguages: [python]\n'
        "subsystems: [alpha, beta]\n",
        encoding="utf-8",
    )
    (cfg / config.MANIFESTS_DIR / "alpha.yaml").write_text(
        "name: alpha\nrole: library\ncriticality: leaf\npaths:\n  - src/alpha\n"
        "exposes:\n  - { name: parent_fn, kind: function }\n",
        encoding="utf-8",
    )
    (cfg / config.MANIFESTS_DIR / "beta.yaml").write_text(
        "name: beta\nrole: library\ncriticality: leaf\npaths:\n  - src/alpha/beta\n"
        "exposes:\n  - { name: child_fn, kind: function }\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "alpha" / "beta").mkdir(parents=True)
    (tmp_path / "src" / "alpha" / "core.py").write_text("def parent_fn():\n    pass\n", encoding="utf-8")
    (tmp_path / "src" / "alpha" / "beta" / "mod.py").write_text("def child_fn():\n    pass\n", encoding="utf-8")
    _git_init(tmp_path)
    return tmp_path


def test_nested_child_keeps_its_own_exports(tmp_path):
    """A nested child subsystem owns the files under its own path — no false structural drift."""
    _nested_project(tmp_path)
    report = engine.run(tmp_path, mode="full", include_gitignored=True)
    drift = [i for i in report.issues
             if i.code == errors.E_STRUCTURAL_DRIFT and i.subsystem == "beta"]
    # `child_fn` is genuinely defined in src/alpha/beta/mod.py and declared in beta.exposes, so the
    # most-specific-path-wins owner (beta) keeps it and reports zero drift.
    assert drift == [], f"beta drift on unchanged source: {[i.message for i in drift]}"


def test_resolve_owners_most_specific_path_wins(tmp_path):
    """The deepest declared path owns a file even when its subsystem sorts after the parent's."""
    _nested_project(tmp_path)
    from bounds.core.manifest import loader
    _root, subs, _issues = loader.load_all(tmp_path)
    owners = scan.resolve_owners(tmp_path, subs, {".py"})
    assert owners["src/alpha/beta/mod.py"][0] == "beta"   # child, not the alphabetically-first alpha
    assert owners["src/alpha/core.py"][0] == "alpha"


def test_path_specificity_ranks_deeper_and_exact_higher():
    """Deeper directory prefixes and exact-file paths outrank shallow ones (root = least specific)."""
    assert scan.path_specificity("src/a/b/m.py", ["src/a/b"]) > scan.path_specificity("src/a/b/m.py", ["src/a"])
    assert scan.path_specificity("src/a/m.py", ["src/a/m.py"]) > scan.path_specificity("src/a/m.py", ["src/a"])
    assert scan.path_specificity("src/a/m.py", ["."]) == 0
    assert scan.path_specificity("src/a/m.py", ["other"]) == 0


def test_explicit_files_declaration_outranks_sibling_directory_path(tmp_path):
    """An explicit `files: [x]` declaration owns x even when a sibling lists the parent dir in
    `paths:` — an explicit single-file intent is the most specific a manifest can express. (Without
    this, `files:`-declared ownership silently lost to a sibling's directory prefix.)"""
    cfg = tmp_path / config.BOUNDS_DIR
    (cfg / config.MANIFESTS_DIR).mkdir(parents=True)
    (cfg / config.ROOT_FILE).write_text(
        'version: "1"\nproject: x\nlanguages: [python]\nsubsystems: [special, zdir]\n'
    )
    (cfg / config.MANIFESTS_DIR / "special.yaml").write_text(
        "name: special\nrole: library\ncriticality: leaf\nfiles:\n  - pkg/special.py\nexposes: []\n"
    )
    (cfg / config.MANIFESTS_DIR / "zdir.yaml").write_text(  # sorts after 'special'; claims the dir
        "name: zdir\nrole: library\ncriticality: leaf\npaths:\n  - pkg\nexposes: []\n"
    )
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "special.py").write_text("def s():\n    pass\n")
    (pkg / "other.py").write_text("def o():\n    pass\n")
    _git_init(tmp_path)
    from bounds.core.manifest import loader
    _root, subs, _issues = loader.load_all(tmp_path)
    owners = scan.resolve_owners(tmp_path, subs, {".py"})
    assert owners["pkg/special.py"][0] == "special"   # explicit files: beats the dir paths:
    assert owners["pkg/other.py"][0] == "zdir"
