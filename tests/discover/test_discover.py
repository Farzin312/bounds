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
    """Dry-run discover proposes both source dirs as kept candidates with confidence scored by file count (5 files -> high)."""
    _project(tmp_path)
    result = run_discover(tmp_path)
    assert result["mode"] == "discover"
    assert result["applied"] is False
    kept = {c["name"]: c for c in result["candidates"] if not c["dropped"]}
    assert {"db", "auth"} <= set(kept)
    # 5 files each -> high confidence.
    assert kept["db"]["score"] == "high"


def test_discover_exposes_are_verified_and_skip_private(tmp_path):
    """Proposed exposes are public-only (no `_private`) and every one is tree-sitter-verified — discover never guesses a surface."""
    _project(tmp_path)
    result = run_discover(tmp_path)
    auth = next(c for c in result["candidates"] if c["name"] == "auth")
    names = {e["name"] for e in auth["exposes"]}
    assert "login" in names and "verify" in names
    assert "_private" not in names  # private symbols are not proposed
    assert all(e["verified"] is True for e in auth["exposes"])  # tree-sitter confirmed


def test_discover_infers_consumes_edge(tmp_path):
    """An import from auth->db is inferred as a consumes edge, and being consumed bumps db's criticality above leaf."""
    _project(tmp_path)
    result = run_discover(tmp_path)
    auth = next(c for c in result["candidates"] if c["name"] == "auth")
    assert "db" in auth["consumes"]
    # db is consumed -> criticality bumped above leaf.
    db = next(c for c in result["candidates"] if c["name"] == "db")
    assert db["criticality"] in {"connector", "core"}


def test_discover_apply_writes_manifests(tmp_path):
    """apply=True writes root.yaml + per-subsystem manifests, and a re-run skips existing files rather than clobbering edits."""
    _project(tmp_path)
    result = run_discover(tmp_path, apply=True)
    assert result["applied"] is True
    root_file = tmp_path / config.BOUNDS_DIR / config.ROOT_FILE
    assert root_file.is_file()
    root_doc = yaml.safe_load(root_file.read_text())
    assert "auth" in root_doc["subsystems"] and "db" in root_doc["subsystems"]
    auth_manifest = tmp_path / config.BOUNDS_DIR / config.MANIFESTS_DIR / "auth.yaml"
    assert auth_manifest.is_file()

    # Re-running --apply on a fully bounded repo does not re-propose duplicate manifests.
    again = run_discover(tmp_path, apply=True)
    assert again["candidates"] == []
    assert again["written"] == []
    assert again["skipped"] == []
    assert "no unmapped supported source" in again["notice"]


def test_discover_apply_ensures_gitignore(tmp_path):
    """apply=True scaffolds .bounds/.gitignore (regenerable cache armor) with the three entries."""
    _project(tmp_path)
    run_discover(tmp_path, apply=True)
    gi = tmp_path / config.BOUNDS_DIR / config.GITIGNORE_FILE
    assert gi.is_file()
    body = gi.read_text(encoding="utf-8")
    for entry in ("cache.db", "cache.db-journal", "state.json"):
        assert entry in body


def test_discover_apply_preserves_existing_gitignore(tmp_path):
    """apply=True onto a pre-existing user-authored .bounds/.gitignore preserves it and appends only the missing entries (no rewrite, no dupes)."""
    _project(tmp_path)
    bounds_dir = tmp_path / config.BOUNDS_DIR
    bounds_dir.mkdir(parents=True, exist_ok=True)
    gi = bounds_dir / config.GITIGNORE_FILE
    user_body = "# hand-written\nsecrets.env\n"
    gi.write_text(user_body, encoding="utf-8")

    run_discover(tmp_path, apply=True)

    after = gi.read_text(encoding="utf-8")
    assert after.startswith(user_body)  # user content survives byte-for-byte at the head
    lines = [line.strip() for line in after.splitlines()]
    assert "secrets.env" in lines
    for entry in config.GITIGNORE_ENTRIES:
        assert lines.count(entry) == 1  # each required entry present exactly once


def test_discover_namespace_tag(tmp_path):
    """A namespace= arg tags every kept candidate with that namespace, so a monorepo slice can be grouped on discovery."""
    _project(tmp_path)
    result = run_discover(tmp_path, namespace="backend")
    kept = [c for c in result["candidates"] if not c["dropped"]]
    assert all(c["namespace"] == "backend" for c in kept)


# --- languages detection (regression for the hardcoded `languages: [python]` bug) ---
def test_discover_writes_detected_languages(tmp_path):
    """root.yaml's `languages` reflects the extracted source, not a hardcoded default."""
    _project(tmp_path)  # pure-Python fixture
    run_discover(tmp_path, apply=True)
    root_doc = yaml.safe_load((tmp_path / config.BOUNDS_DIR / config.ROOT_FILE).read_text())
    assert root_doc["languages"] == ["python"]


def test_discover_never_promotes_test_dirs_to_subsystems(tmp_path):
    """BOUNDS-020: tests are linked evidence, not architecture subsystems created by discover."""
    auth = tmp_path / "auth"
    auth.mkdir()
    for i in range(5):
        (auth / f"m{i}.py").write_text(f"def f{i}():\n    pass\n")
    tests_auth = tmp_path / "tests" / "auth"
    tests_auth.mkdir(parents=True)
    for i in range(8):
        (tests_auth / f"test_case_{i}.py").write_text(
            "def test_case():\n    assert True\n"
            "def make_fixture():\n    return 1\n"
        )

    result = run_discover(tmp_path)
    names = {c["name"] for c in result["candidates"]}
    assert names == {"auth"}
    auth_cand = next(c for c in result["candidates"] if c["name"] == "auth" and not c["dropped"])
    assert auth_cand["tests"] == ["tests/auth"]
    assert {e["name"] for e in auth_cand["exposes"]} == {f"f{i}" for i in range(5)}


def test_discover_top_level_tests_dir_is_not_a_candidate(tmp_path):
    """A top-level ``tests/`` directory produces NO discover candidate — neither kept nor dropped.

    Codifies the is_test_file pre-filter: test files are removed from ``sources`` before candidate
    grouping, so a tests/ dir never even appears as a (dropped) candidate. It is linked evidence on
    a real subsystem, never architecture discover would propose as its own subsystem.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    for i in range(5):
        (pkg / f"m{i}.py").write_text(f"def f{i}():\n    pass\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    for i in range(8):
        (tests / f"test_thing_{i}.py").write_text("def test_thing():\n    assert True\n")

    result = run_discover(tmp_path)
    # No candidate at all references the tests dir — not kept, not dropped.
    assert all("tests" not in c["name"] for c in result["candidates"])
    assert {c["name"] for c in result["candidates"]} == {"pkg"}


def test_discover_exposes_honour_dunder_all_matching_extraction(tmp_path):
    """Discover's ``exposes`` honour a module's literal __all__ exactly as the adapter does, so discover and validate agree on the public surface: a non-listed public name is dropped, and an __all__-ed leading-underscore name is kept."""
    pkg = tmp_path / "api"
    pkg.mkdir()
    (pkg / "core.py").write_text(
        '__all__ = ["public_a", "_explicit"]\n\n'
        "def public_a():\n    pass\n\n"
        "def public_b():\n    pass\n\n"
        "def _explicit():\n    pass\n\n"
        "def _hidden():\n    pass\n"
    )
    for i in range(4):
        (pkg / f"more{i}.py").write_text(f"def g{i}():\n    pass\n")

    result = run_discover(tmp_path)
    api = next(c for c in result["candidates"] if c["name"] == "api" and not c["dropped"])
    names = {e["name"] for e in api["exposes"]}
    assert "public_a" in names          # listed in __all__
    assert "_explicit" in names         # __all__ overrides the underscore rule
    assert "public_b" not in names      # public-cased but omitted from __all__
    assert "_hidden" not in names       # neither listed nor public-cased


def test_discover_overwrites_hardcoded_python_default_for_ts(tmp_path):
    """REGRESSION (BUG-4): a pure-TS repo must NOT keep init's `languages: [python]` placeholder.

    `bounds init` seeds root.yaml with `languages: [python]`; discovering a TS-only repo must
    overwrite that with the detected language, or a non-Python project lies about itself.
    """
    cfg = tmp_path / config.BOUNDS_DIR
    cfg.mkdir()
    # Simulate what `init` wrote: the hardcoded python placeholder.
    (cfg / config.ROOT_FILE).write_text(
        'version: "1"\nproject: app\nlanguages: [python]\nsubsystems: []\n'
    )
    svc = tmp_path / "src" / "svc"
    svc.mkdir(parents=True)
    (svc / "a.ts").write_text("export function alpha() {}\nexport class Beta {}\n")
    for i in range(4):
        (svc / f"m{i}.ts").write_text(f"export const k{i} = {i};\n")

    run_discover(tmp_path, apply=True)
    root_doc = yaml.safe_load((cfg / config.ROOT_FILE).read_text())
    assert root_doc["languages"] == ["typescript"]  # overwritten, not the stale [python]
    assert "python" not in root_doc["languages"]


def test_discover_folds_module_subparts_into_parent(tmp_path):
    """A NestJS-shaped module folds dto/ and services/ subdirs into one `auth` subsystem (path src/auth), not three fragments."""
    # A NestJS-shaped module (auth.module.ts directly + dto/ and services/ subdirs) becomes ONE
    # `auth` subsystem, not auth + auth-dto + auth-services (over-fragmentation).
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
    """A structural-named dir (types/) with no candidate parent stays its own subsystem — folding never invents a parent."""
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
    """Two dirs sharing a basename (a/utils, b/utils) get distinct path-derived names (a-utils/b-utils), never fused into one 'utils'."""
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
    """discover --apply merges new finds without clobbering an existing root.yaml's custom roles/criticality (extensible-schema)."""
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


def test_discover_existing_model_does_not_create_duplicate_subsystems(tmp_path):
    """On an already-bounded repo, discover targets unmapped source only; it must not create alternate manifests over owned files."""
    cfg = tmp_path / config.BOUNDS_DIR
    (cfg / config.MANIFESTS_DIR).mkdir(parents=True)
    (cfg / config.ROOT_FILE).write_text(
        yaml.safe_dump({"version": "1", "project": "proj", "languages": ["python"], "subsystems": ["app"]}),
        encoding="utf-8",
    )
    (cfg / config.MANIFESTS_DIR / "app.yaml").write_text(
        "name: app\nrole: library\ncriticality: core\npaths: [src/app]\n"
        "exposes:\n  - { name: run, kind: function }\n"
        "tests:\n  - tests/app\n",
        encoding="utf-8",
    )
    app = tmp_path / "src" / "app"
    app.mkdir(parents=True)
    (app / "main.py").write_text("def run():\n    return True\n", encoding="utf-8")
    tests = tmp_path / "tests" / "app"
    tests.mkdir(parents=True)
    for i in range(3):
        (tests / f"test_app_{i}.py").write_text("def test_run():\n    assert True\n", encoding="utf-8")

    dry = run_discover(tmp_path)
    assert [c for c in dry["candidates"] if not c["dropped"]] == []
    assert "no unmapped supported source" in dry["notice"]

    applied = run_discover(tmp_path, apply=True)
    assert applied["written"] == []
    assert applied["skipped"] == []
    assert not (cfg / config.MANIFESTS_DIR / "tests-app.yaml").exists()
    root_doc = yaml.safe_load((cfg / config.ROOT_FILE).read_text())
    assert root_doc["subsystems"] == ["app"]


def test_discover_existing_model_adds_only_unmapped_source(tmp_path):
    """Partial discovery may add a new subsystem, but imports to existing subsystems stay wired."""
    cfg = tmp_path / config.BOUNDS_DIR
    (cfg / config.MANIFESTS_DIR).mkdir(parents=True)
    (cfg / config.ROOT_FILE).write_text(
        yaml.safe_dump({"version": "1", "project": "proj", "languages": ["python"], "subsystems": ["app"]}),
        encoding="utf-8",
    )
    (cfg / config.MANIFESTS_DIR / "app.yaml").write_text(
        "name: app\nrole: library\ncriticality: core\npaths: [src/app]\n"
        "exposes:\n  - { name: run, kind: function }\n",
        encoding="utf-8",
    )
    app = tmp_path / "src" / "app"
    extra = tmp_path / "src" / "extra"
    app.mkdir(parents=True)
    extra.mkdir(parents=True)
    (app / "main.py").write_text("def run():\n    return True\n", encoding="utf-8")
    (extra / "feature.py").write_text(
        "from ..app.main import run\n\n"
        "def feature():\n    return run()\n",
        encoding="utf-8",
    )
    for i in range(4):
        (extra / f"mod{i}.py").write_text(f"def helper{i}():\n    return True\n", encoding="utf-8")

    dry = run_discover(tmp_path)
    kept = [c for c in dry["candidates"] if not c["dropped"]]
    assert [c["name"] for c in kept] == ["extra"]
    assert kept[0]["consumes"] == ["app"]

    applied = run_discover(tmp_path, apply=True)
    assert f"{config.BOUNDS_DIR}/{config.MANIFESTS_DIR}/extra.yaml" in applied["written"]
    assert not (cfg / config.MANIFESTS_DIR / "app.yaml.yaml").exists()
    root_doc = yaml.safe_load((cfg / config.ROOT_FILE).read_text())
    assert root_doc["subsystems"] == ["app", "extra"]


def test_discover_merge_into(tmp_path):
    """An explicit merges= directive folds multiple paths into one named subsystem ('core'), suppressing the per-dir candidates."""
    _project(tmp_path)
    # Fold both dirs into one subsystem named 'core'.
    result = run_discover(tmp_path, merges=[("core", ["src/db", "src/auth"])])
    names = {c["name"] for c in result["candidates"] if not c["dropped"]}
    assert "core" in names
    assert "db" not in names and "auth" not in names


# ---- .gitignore awareness (FIX) ----
@requires_git
def test_discover_skips_gitignored_paths(tmp_path):
    """A gitignored dir (dist/) is excluded even from dropped candidates — discover honors .gitignore, never maps build artifacts."""
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
    """Sanity counterpart to gitignore filtering: with no .gitignore, every real source dir is still discovered."""
    # Sanity: with no .gitignore, every real source dir is still discovered.
    _git_init(tmp_path)
    _project(tmp_path)
    result = run_discover(tmp_path)
    names = {c["name"] for c in result["candidates"] if not c["dropped"]}
    assert {"db", "auth"} <= names


def test_discover_non_git_repo_still_works(tmp_path):
    """Fail-soft: with no .git, gitignore filtering is skipped (not an error) and DEFAULT_IGNORES-based discovery still works."""
    # No .git here: gitignore filtering fails soft, DEFAULT_IGNORES behavior is unchanged.
    assert not (tmp_path / ".git").exists()
    _project(tmp_path)
    result = run_discover(tmp_path)
    names = {c["name"] for c in result["candidates"] if not c["dropped"]}
    assert {"db", "auth"} <= names


# ---- explicit "0 written / N skipped" signal (FIX) ----
def test_discover_apply_zero_written_emits_notice(tmp_path):
    """A re-apply that writes 0 new manifests emits a notice pointing at `calibrate`, so the user isn't silently confused by a no-op."""
    _project(tmp_path)
    first = run_discover(tmp_path, apply=True)
    assert "notice" not in first  # real manifests were written -> no confusing notice

    again = run_discover(tmp_path, apply=True)  # all source is already owned
    assert again["written"] == []
    assert again["skipped"] == []
    assert "notice" in again
    assert "no unmapped supported source" in again["notice"]
    assert "calibrate" in again["notice"]  # points the user at the reconcile path


def test_discover_dry_run_has_no_notice(tmp_path):
    """A dry-run (no apply) emits no 'wrote/skipped' notice — the notice only describes actual writes, never a preview."""
    _project(tmp_path)
    result = run_discover(tmp_path)  # dry-run never claims to have written anything
    assert "notice" not in result


# ---- SQL/schema directory mapping (FIX) ----
def test_discover_maps_sql_schema_dir_regardless_of_count(tmp_path):
    """A migration dir is always kept and scored 'schema' (never count-dropped), with its folded table surface materialized."""
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
    """A large migration dir (60 files, past the old >50 'low' cap) is still kept as schema with all 60 tables folded in."""
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
    """Fail-soft: when files yield no extract, schema is classified by extension (.sql majority), so a real migration dir isn't dropped."""
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
    """End-to-end fail-soft: a migrations dir with an oversized (unextracted) .sql is still kept and scored schema."""
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
    """A large code dir (60 files) is kept and scored 'high', never silently dropped — discovery must not hide a repo's biggest surfaces."""
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


def test_discover_auto_populates_tests_by_convention(tmp_path):
    """A fresh discover already links a subsystem's tests by convention (a mirrored tests/<name>/ dir
    → subsystem <name>), so the user does very little — a directory glob is preferred over files."""
    auth = tmp_path / "auth"
    auth.mkdir()
    for i in range(5):  # 5 files → `auth` is a kept subsystem named exactly `auth`
        (auth / f"m{i}.py").write_text(f"def f{i}():\n    pass\n")
    tests_auth = tmp_path / "tests" / "auth"
    tests_auth.mkdir(parents=True)
    (tests_auth / "test_login.py").write_text("def test_login():\n    pass\n")  # 1 file → not its own subsystem
    _git_init(tmp_path)
    result = run_discover(tmp_path)
    auth_cand = next(c for c in result["candidates"] if c["name"] == "auth" and not c["dropped"])
    # tests/auth/ maps to `auth` by name-segment convention; the whole dir maps cleanly so it
    # collapses to the dir glob (token-lean), not the individual file.
    assert auth_cand["tests"] == ["tests/auth"]


def test_discover_top_level_test_file_links_without_overclaiming_tests_dir(tmp_path):
    """A single `tests/test_<name>.py` link stays file-scoped; discover must not claim all of `tests/`."""
    auth = tmp_path / "auth"
    auth.mkdir()
    for i in range(5):
        (auth / f"m{i}.py").write_text(f"def f{i}():\n    pass\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_auth.py").write_text("def test_auth():\n    assert True\n")

    result = run_discover(tmp_path)
    auth_cand = next(c for c in result["candidates"] if c["name"] == "auth" and not c["dropped"])
    assert auth_cand["tests"] == ["tests/test_auth.py"]


def test_discover_docs_convention_links_file_without_overclaiming_docs_dir(tmp_path):
    """A top-level docs/<name>.md link stays file-scoped; discover must not claim all of `docs/`."""
    auth = tmp_path / "auth"
    auth.mkdir()
    for i in range(5):
        (auth / f"m{i}.py").write_text(f"def f{i}():\n    pass\n")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "auth.md").write_text("# Auth\n")

    result = run_discover(tmp_path)
    auth_cand = next(c for c in result["candidates"] if c["name"] == "auth" and not c["dropped"])
    assert auth_cand["docs"] == ["docs/auth.md"]


def test_discover_docs_named_dir_can_collapse_to_dir_link(tmp_path):
    """docs/<name>/ can collapse when the directory basename is the owner and all docs inside map to it."""
    auth = tmp_path / "auth"
    auth.mkdir()
    for i in range(5):
        (auth / f"m{i}.py").write_text(f"def f{i}():\n    pass\n")
    docs = tmp_path / "docs" / "auth"
    docs.mkdir(parents=True)
    (docs / "overview.md").write_text("# Auth overview\n")
    (docs / "api.md").write_text("# Auth API\n")

    result = run_discover(tmp_path)
    auth_cand = next(c for c in result["candidates"] if c["name"] == "auth" and not c["dropped"])
    assert auth_cand["docs"] == ["docs/auth"]


def test_discover_applied_manifest_carries_tests(tmp_path):
    """--apply writes the convention-linked `tests:` into the manifest, so a fresh discover produces
    a manifest that already maps source↔tests with no hand-editing."""
    billing = tmp_path / "billing"
    billing.mkdir()
    for i in range(5):
        (billing / f"m{i}.py").write_text(f"def f{i}():\n    pass\n")
    tests_billing = tmp_path / "tests" / "billing"
    tests_billing.mkdir(parents=True)
    (tests_billing / "test_charge.py").write_text("def test_charge():\n    pass\n")
    _git_init(tmp_path)
    run_discover(tmp_path, apply=True)
    man = tmp_path / config.BOUNDS_DIR / config.MANIFESTS_DIR / "billing.yaml"
    doc = yaml.safe_load(man.read_text())
    assert doc["tests"] == ["tests/billing"]


def test_discover_existing_model_reports_unlinked_tests_without_candidates(tmp_path):
    """Unlinked tests in an existing Bounds repo stay in coverage diagnostics, not generated manifests."""
    cfg = tmp_path / config.BOUNDS_DIR
    (cfg / config.MANIFESTS_DIR).mkdir(parents=True)
    (cfg / config.ROOT_FILE).write_text(
        yaml.safe_dump({"version": "1", "project": "proj", "languages": ["python"], "subsystems": ["app"]}),
        encoding="utf-8",
    )
    (cfg / config.MANIFESTS_DIR / "app.yaml").write_text(
        "name: app\nrole: library\ncriticality: core\npaths: [src/app]\n"
        "exposes:\n  - { name: run, kind: function }\n",
        encoding="utf-8",
    )
    app = tmp_path / "src" / "app"
    app.mkdir(parents=True)
    (app / "main.py").write_text("def run():\n    return True\n", encoding="utf-8")
    misc = tmp_path / "tests" / "misc"
    misc.mkdir(parents=True)
    for i in range(6):
        (misc / f"test_misc_{i}.py").write_text("def test_misc():\n    assert True\n", encoding="utf-8")

    result = run_discover(tmp_path, apply=True)
    assert result["candidates"] == []
    assert result["written"] == []
    assert result["coverage"]["tests"]["unlinked"] == 6
    assert not (cfg / config.MANIFESTS_DIR / "misc.yaml").exists()
