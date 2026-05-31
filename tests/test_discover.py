"""Bootstrap discovery: the bounds discover command."""

from __future__ import annotations

import shutil
import subprocess

import pytest
import yaml

from bounds import config
from bounds.discover import run_discover


def _git_init(path) -> None:
    """Initialize a git repo at ``path`` (identity set so commands don't warn)."""
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _project(tmp_path):
    """A 2-subsystem project: db (5 files) consumed by auth (5 files)."""
    db = tmp_path / "src" / "db"
    auth = tmp_path / "src" / "auth"
    db.mkdir(parents=True)
    auth.mkdir(parents=True)
    (db / "store.py").write_text("def connect():\n    pass\ndef query(sql):\n    pass\n")
    for i in range(4):
        (db / f"util{i}.py").write_text(f"def helper{i}():\n    pass\n")
    (auth / "login.py").write_text(
        "from ..db.store import connect, query\n"
        "def login(u):\n    pass\ndef verify(t):\n    pass\n"
        "def _private():\n    pass\n"
    )
    for i in range(4):
        (auth / f"mod{i}.py").write_text(f"def feature{i}():\n    pass\n")
    return tmp_path


def test_discover_proposes_candidates(tmp_path):
    _project(tmp_path)
    result = run_discover(tmp_path)
    assert result["mode"] == "discover"
    assert result["applied"] is False
    kept = {c["name"]: c for c in result["candidates"] if not c["dropped"]}
    assert {"db", "auth"} <= set(kept)
    # 5 files each -> high confidence.
    assert kept["db"]["score"] == "high"


def test_discover_exposes_are_verified_and_skip_private(tmp_path):
    _project(tmp_path)
    result = run_discover(tmp_path)
    auth = next(c for c in result["candidates"] if c["name"] == "auth")
    names = {e["name"] for e in auth["exposes"]}
    assert "login" in names and "verify" in names
    assert "_private" not in names  # private symbols are not proposed
    assert all(e["verified"] is True for e in auth["exposes"])  # tree-sitter confirmed


def test_discover_infers_consumes_edge(tmp_path):
    _project(tmp_path)
    result = run_discover(tmp_path)
    auth = next(c for c in result["candidates"] if c["name"] == "auth")
    assert "db" in auth["consumes"]
    # db is consumed -> criticality bumped above leaf.
    db = next(c for c in result["candidates"] if c["name"] == "db")
    assert db["criticality"] in {"connector", "core"}


def test_discover_apply_writes_manifests(tmp_path):
    _project(tmp_path)
    result = run_discover(tmp_path, apply=True)
    assert result["applied"] is True
    root_file = tmp_path / config.BOUNDS_DIR / config.ROOT_FILE
    assert root_file.is_file()
    root_doc = yaml.safe_load(root_file.read_text())
    assert "auth" in root_doc["subsystems"] and "db" in root_doc["subsystems"]
    auth_manifest = tmp_path / config.BOUNDS_DIR / config.MANIFESTS_DIR / "auth.yaml"
    assert auth_manifest.is_file()

    # Re-running --apply skips existing manifests rather than clobbering.
    again = run_discover(tmp_path, apply=True)
    assert any("auth.yaml" in s for s in again["skipped"])


def test_discover_namespace_tag(tmp_path):
    _project(tmp_path)
    result = run_discover(tmp_path, namespace="backend")
    kept = [c for c in result["candidates"] if not c["dropped"]]
    assert all(c["namespace"] == "backend" for c in kept)


def test_discover_disambiguates_colliding_basenames(tmp_path):
    # a/utils and b/utils must NOT fuse into one 'utils' subsystem.
    for tree in ("a", "b"):
        d = tmp_path / "src" / tree / "utils"
        d.mkdir(parents=True)
        for i in range(5):
            (d / f"u{i}.py").write_text(f"def fn{i}():\n    pass\n")
    result = run_discover(tmp_path)
    names = {c["name"] for c in result["candidates"] if not c["dropped"]}
    # Two distinct utils dirs -> two distinct, path-derived candidate names.
    assert "a-utils" in names and "b-utils" in names
    assert "utils" not in names


def test_discover_apply_preserves_custom_root_keys(tmp_path):
    # An existing root.yaml with custom roles must survive `discover --apply`.
    cfg = tmp_path / config.BOUNDS_DIR
    cfg.mkdir()
    (cfg / config.ROOT_FILE).write_text(
        yaml.safe_dump(
            {
                "version": "1", "project": "proj", "subsystems": [],
                "roles": {"gateway": {"extends": "service"}},
                "criticality": {"critical": {"depth": -1}},
            },
            sort_keys=False,
        )
    )
    _project(tmp_path)
    run_discover(tmp_path, apply=True)
    root_doc = yaml.safe_load((cfg / config.ROOT_FILE).read_text())
    assert root_doc.get("roles") == {"gateway": {"extends": "service"}}
    assert root_doc.get("criticality") == {"critical": {"depth": -1}}
    assert "auth" in root_doc["subsystems"]  # discovery still merged its finds


def test_discover_merge_into(tmp_path):
    _project(tmp_path)
    # Fold both dirs into one subsystem named 'core'.
    result = run_discover(tmp_path, merges=[("core", ["src/db", "src/auth"])])
    names = {c["name"] for c in result["candidates"] if not c["dropped"]}
    assert "core" in names
    assert "db" not in names and "auth" not in names


# ---- .gitignore awareness (FIX) ----
@requires_git
def test_discover_skips_gitignored_paths(tmp_path):
    # A gitignored build dir must not become a candidate subsystem.
    _git_init(tmp_path)
    _project(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    for i in range(5):
        (dist / f"bundle{i}.py").write_text(f"def junk{i}():\n    pass\n")
    (tmp_path / ".gitignore").write_text("dist/\n")

    result = run_discover(tmp_path)
    names = {c["name"] for c in result["candidates"]}  # includes dropped, to be strict
    assert "dist" not in names
    assert "db" in names and "auth" in names


@requires_git
def test_discover_does_not_skip_tracked_paths(tmp_path):
    # Sanity: with no .gitignore, every real source dir is still discovered.
    _git_init(tmp_path)
    _project(tmp_path)
    result = run_discover(tmp_path)
    names = {c["name"] for c in result["candidates"] if not c["dropped"]}
    assert {"db", "auth"} <= names


def test_discover_non_git_repo_still_works(tmp_path):
    # No .git here: gitignore filtering fails soft, DEFAULT_IGNORES behavior is unchanged.
    assert not (tmp_path / ".git").exists()
    _project(tmp_path)
    result = run_discover(tmp_path)
    names = {c["name"] for c in result["candidates"] if not c["dropped"]}
    assert {"db", "auth"} <= names


# ---- explicit "0 written / N skipped" signal (FIX) ----
def test_discover_apply_zero_written_emits_notice(tmp_path):
    _project(tmp_path)
    first = run_discover(tmp_path, apply=True)
    assert "notice" not in first  # real manifests were written -> no confusing notice

    again = run_discover(tmp_path, apply=True)  # all candidates already exist
    assert again["written"] == [f"{config.BOUNDS_DIR}/{config.ROOT_FILE}"]  # only root re-merged
    assert again["skipped"]  # the per-candidate manifests were skipped
    assert "notice" in again
    assert "0 new manifests" in again["notice"]
    assert "calibrate" in again["notice"]  # points the user at the reconcile path


def test_discover_dry_run_has_no_notice(tmp_path):
    _project(tmp_path)
    result = run_discover(tmp_path)  # dry-run never claims to have written anything
    assert "notice" not in result
