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


def test_discover_folds_module_subparts_into_parent(tmp_path):
    # A NestJS-shaped module (auth.module.ts directly + dto/ and services/ subdirs) becomes ONE
    # `auth` subsystem, not auth + auth-dto + auth-services (the spex_backend over-fragmentation).
    (tmp_path / ".bounds").mkdir()
    auth = tmp_path / "src" / "auth"
    (auth / "dto").mkdir(parents=True)
    (auth / "services").mkdir()
    (auth / "auth.module.ts").write_text("export class AuthModule {}\n")
    (auth / "auth.controller.ts").write_text("export class AuthController {}\n")
    (auth / "dto" / "login.dto.ts").write_text("export class LoginDto {}\n")
    (auth / "dto" / "register.dto.ts").write_text("export class RegisterDto {}\n")
    (auth / "services" / "auth.service.ts").write_text("export class AuthService {}\n")
    (auth / "services" / "token.service.ts").write_text("export class TokenService {}\n")
    result = run_discover(tmp_path)
    names = {c["name"] for c in result["candidates"] if not c["dropped"]}
    assert names == {"auth"}
    auth_cand = next(c for c in result["candidates"] if c["name"] == "auth")
    assert auth_cand["paths"] == ["src/auth"]  # collapsed to the covering root, not three paths


def test_discover_keeps_standalone_structural_dir(tmp_path):
    # A structural-named dir whose parent is NOT a candidate (no sibling module files) is preserved
    # — folding never invents a parent or fuses unrelated trees.
    (tmp_path / ".bounds").mkdir()
    types = tmp_path / "src" / "types"
    types.mkdir(parents=True)
    for i in range(4):
        (types / f"t{i}.ts").write_text(f"export type T{i} = string;\n")
    result = run_discover(tmp_path)
    names = {c["name"] for c in result["candidates"] if not c["dropped"]}
    assert "types" in names


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


# ---- SQL/schema directory mapping (FIX) ----
def test_discover_maps_sql_schema_dir_regardless_of_count(tmp_path):
    # A migration set folds to one table surface, so it must be kept even when its file
    # count would otherwise score 'low' (here a single migration) AND when it is large.
    mig = tmp_path / "supabase" / "migrations"
    mig.mkdir(parents=True)
    (mig / "001_init.sql").write_text(
        "CREATE TABLE profiles (id uuid PRIMARY KEY, email text);\n"
    )
    result = run_discover(tmp_path)
    # Basename is unique here, so the candidate is named for the dir ('migrations').
    schema = next((c for c in result["candidates"] if c["name"] == "migrations"), None)
    assert schema is not None, "supabase/migrations must become a candidate"
    assert schema["dropped"] is False  # never count-dropped despite a single file
    assert schema["score"] == "schema"
    assert schema.get("schema") is True
    assert schema["tables"] == 1  # the fold materialized the table surface


def test_discover_large_schema_dir_not_dropped(tmp_path):
    mig = tmp_path / "db" / "migrations"
    mig.mkdir(parents=True)
    for i in range(60):  # well over the old >50 'low' cap
        (mig / f"{i:03d}_add.sql").write_text(f"CREATE TABLE t{i} (id int);\n")
    result = run_discover(tmp_path)
    schema = next((c for c in result["candidates"] if c["name"] == "migrations"), None)
    assert schema is not None and schema["dropped"] is False
    assert schema["score"] == "schema"
    assert schema["tables"] == 60


def test_schema_classification_falls_back_to_extension_when_extraction_fails(tmp_path):
    # A .sql/.prisma file missing from `extracts` (e.g. oversized/unreadable/unparsable, which
    # yields no extract) must still count as schema via its extension — otherwise a real
    # migration dir could be misclassified and dropped, breaking the always-keep guarantee.
    from bounds.discover import _is_schema_candidate

    # No extracts at all: extension fallback recognizes the migration set as schema.
    assert _is_schema_candidate(["db/migrations/001.sql", "db/migrations/002.sql"], {}) is True
    # Non-schema files with no extracts are NOT misread as schema.
    assert _is_schema_candidate(["src/a.py", "src/b.py"], {}) is False
    # Mixed: schema-dominant (2 of 3) still counts as schema even with empty extracts.
    assert _is_schema_candidate(["m/1.sql", "m/2.sql", "m/util.py"], {}) is True


def test_discover_maps_migration_dir_with_oversized_unextracted_file(tmp_path):
    # End-to-end: a migrations dir whose files don't all extract is still kept as schema.
    mig = tmp_path / "supa" / "migrations"
    mig.mkdir(parents=True)
    (mig / "001_init.sql").write_text("CREATE TABLE t (id int);\n")
    # An oversized .sql is skipped by extraction (returns no extract) — extension keeps it schema.
    (mig / "002_huge.sql").write_text("-- big\n" + ("SELECT 1;\n" * 200_000))
    result = run_discover(tmp_path)
    schema = next((c for c in result["candidates"] if c["name"] == "migrations"), None)
    assert schema is not None and schema["dropped"] is False
    assert schema["score"] == "schema"


def test_discover_large_code_dir_kept_not_silently_dropped(tmp_path):
    # A legitimately large code directory (>50 files) is mapped, never silently dropped —
    # hiding a repo's biggest surfaces is the opposite of discovery's job.
    big = tmp_path / "src" / "components"
    big.mkdir(parents=True)
    for i in range(60):
        (big / f"c{i}.py").write_text(f"def comp{i}():\n    pass\n")
    result = run_discover(tmp_path)
    comp = next((c for c in result["candidates"] if c["name"] == "components"), None)
    assert comp is not None and comp["dropped"] is False
    assert comp["score"] == "high"
