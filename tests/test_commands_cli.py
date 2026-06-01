"""End-to-end CLI smoke tests for the discover/calibrate/impact/where/overview/agent/ci/cache commands (via Click's CliRunner)."""

from __future__ import annotations

import json

from click.testing import CliRunner

from bounds.cli import main


def _invoke(monkeypatch, cwd, args):
    monkeypatch.chdir(cwd)
    return CliRunner().invoke(main, args)


def _write_schema_project(root):
    (root / ".bounds" / "manifests").mkdir(parents=True)
    (root / ".bounds" / "root.yaml").write_text(
        'version: "1"\nproject: schema\nlanguages: [sql, python]\nenforce: "on"\nsubsystems: [db, app]\n',
        encoding="utf-8",
    )
    (root / ".bounds" / "manifests" / "db.yaml").write_text(
        "name: db\nrole: library\ncriticality: core\npaths: [migrations]\n"
        "exposes:\n  - { name: users, kind: table }\nconsumes: []\n",
        encoding="utf-8",
    )
    (root / ".bounds" / "manifests" / "app.yaml").write_text(
        "name: app\nrole: service\ncriticality: leaf\npaths: [app]\nexposes: []\n"
        "consumes:\n  - subsystem: db\n    via: reads\n    interfaces: [users, users.email]\n",
        encoding="utf-8",
    )
    (root / "migrations").mkdir()
    (root / "migrations" / "001_create.sql").write_text(
        "CREATE TABLE users (id integer primary key, email text);\n",
        encoding="utf-8",
    )
    (root / "app").mkdir()
    (root / "app" / "main.py").write_text("def run():\n    return True\n", encoding="utf-8")


def test_impact_cli(monkeypatch, py_project):
    res = _invoke(monkeypatch, py_project, ["impact", "models"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["subsystem"] == "models"
    assert "svc" in data["transitive_consumers"]
    assert data["blast_radius"] >= 1
    # honesty fields are always present.
    assert data["basis"] == "declared-consumes"
    assert data["blast_radius_is_lower_bound"] is True
    assert "lower bound" in data["note"]
    assert "undeclared_consumer_edges" not in data  # only with --verify


def test_impact_verify_flags_undeclared_consumer(monkeypatch, py_project):
    # Drop svc's declared consume of models, but svc/main.py still imports it: the declared
    # blast radius now under-reports (0), and --verify must surface the real edge .
    (py_project / ".bounds" / "manifests" / "svc.yaml").write_text(
        "name: svc\nrole: service\ncriticality: leaf\npaths: [src/svc]\nexposes: []\nconsumes: []\n",
        encoding="utf-8",
    )
    plain = json.loads(_invoke(monkeypatch, py_project, ["impact", "models"]).output)
    assert plain["blast_radius"] == 0                  # declared graph misses it
    assert plain["blast_radius_is_lower_bound"] is True

    verified = json.loads(_invoke(monkeypatch, py_project, ["impact", "models", "--verify"]).output)
    edges = verified["undeclared_consumer_edges"]
    assert any(e["consumer"] == "svc" and "src/svc/main.py" in e["files"] for e in edges)


def test_impact_unknown_subsystem_is_fatal(monkeypatch, py_project):
    res = _invoke(monkeypatch, py_project, ["impact", "nope"])
    assert res.exit_code == 2
    assert "E_SUBSYSTEM_NOT_FOUND" in res.output


def test_schema_describe_impact_and_column_contract(monkeypatch, tmp_path):
    _write_schema_project(tmp_path)

    describe = json.loads(_invoke(monkeypatch, tmp_path, ["describe", "db"]).output)
    assert describe["tables"] == [
        {"name": "users", "kind": "table", "columns": ["email", "id"], "files": ["migrations/001_create.sql"]}
    ]
    users = next(e for e in describe["exposes"] if e["name"] == "users")
    assert users["verified"] is True
    assert users["columns"] == ["email", "id"]

    impact = json.loads(_invoke(monkeypatch, tmp_path, ["impact", "users"]).output)
    assert impact["interface"] == "users"
    assert impact["direct_consumers"] == ["app"]

    (tmp_path / "migrations" / "002_drop.sql").write_text(
        "ALTER TABLE users DROP COLUMN email;\n", encoding="utf-8"
    )
    validate = json.loads(_invoke(monkeypatch, tmp_path, ["validate"]).output)
    assert any(
        issue["code"] == "E_CONTRACT_MISSING_EXPORT" and "users.email" in issue["message"]
        for issue in validate["issues"]
    )


def test_describe_schema_objects_are_counts_by_default_full_lists_them(monkeypatch, tmp_path):
    # A schema with non-table objects (index + function) must summarize them by kind by
    # default (token-lean) and only emit the full list under --full. Tables (the contract)
    # stay full in both.
    _write_schema_project(tmp_path)
    (tmp_path / "migrations" / "002_objects.sql").write_text(
        "CREATE INDEX idx_users_email ON users (email);\n"
        "CREATE FUNCTION bump() RETURNS integer AS $$ SELECT 1 $$ LANGUAGE sql;\n",
        encoding="utf-8",
    )
    lean = json.loads(_invoke(monkeypatch, tmp_path, ["describe", "db"]).output)
    assert lean["schema_object_counts"] == {"function": 1, "index": 1}
    assert "schema_objects" not in lean          # the full list is gated
    assert lean["tables"]                          # the table contract is still full
    assert "file_count" in lean and "files" not in lean

    full = json.loads(_invoke(monkeypatch, tmp_path, ["describe", "db", "--full"]).output)
    kinds = sorted({o["kind"] for o in full["schema_objects"]})
    assert kinds == ["function", "index"]
    assert "files" in full


def _write_exposes_column_project(root, drop_email=False):
    (root / ".bounds" / "manifests").mkdir(parents=True)
    (root / ".bounds" / "root.yaml").write_text(
        'version: "1"\nproject: s\nlanguages: [sql]\nenforce: "on"\nsubsystems: [db]\n', encoding="utf-8")
    # db exposes a COLUMN-granular surface: users.email
    (root / ".bounds" / "manifests" / "db.yaml").write_text(
        "name: db\nrole: library\ncriticality: core\npaths: [migrations]\n"
        "exposes:\n  - { name: users.email, kind: table }\nconsumes: []\n", encoding="utf-8")
    (root / "migrations").mkdir()
    (root / "migrations" / "001_create.sql").write_text(
        "CREATE TABLE users (id integer, email text);\n", encoding="utf-8")
    if drop_email:
        (root / "migrations" / "002_drop.sql").write_text(
            "ALTER TABLE users DROP COLUMN email;\n", encoding="utf-8")


def test_exposes_column_present_does_not_drift(monkeypatch, tmp_path):
    # FALSE-POSITIVE guard: a column-granular expose that still exists must NOT report drift.
    _write_exposes_column_project(tmp_path, drop_email=False)
    data = json.loads(_invoke(monkeypatch, tmp_path, ["validate"]).output)
    assert not any(
        i["code"] == "E_STRUCTURAL_DRIFT" and "users.email" in i["message"] for i in data["issues"]
    )


def test_exposes_column_dropped_drifts(monkeypatch, tmp_path):
    # FALSE-NEGATIVE guard: dropping the exposed column must surface as drift.
    _write_exposes_column_project(tmp_path, drop_email=True)
    data = json.loads(_invoke(monkeypatch, tmp_path, ["validate"]).output)
    assert any(
        i["code"] == "E_STRUCTURAL_DRIFT" and "users.email" in i["message"] for i in data["issues"]
    )


def _write_raw_query_project(root):
    (root / ".bounds" / "manifests").mkdir(parents=True)
    (root / ".bounds" / "root.yaml").write_text(
        'version: "1"\nproject: s\nlanguages: [sql, python]\nenforce: "on"\nsubsystems: [db, app]\n',
        encoding="utf-8")
    (root / ".bounds" / "manifests" / "db.yaml").write_text(
        "name: db\nrole: library\ncriticality: core\npaths: [migrations]\n"
        "exposes:\n  - { name: users, kind: table }\nconsumes: []\n", encoding="utf-8")
    # app declares NOTHING about db, yet runs a raw SQL query against users.
    (root / ".bounds" / "manifests" / "app.yaml").write_text(
        "name: app\nrole: service\ncriticality: leaf\npaths: [app]\nexposes: []\nconsumes: []\n",
        encoding="utf-8")
    (root / "migrations").mkdir()
    (root / "migrations" / "001.sql").write_text("CREATE TABLE users (id int);\n", encoding="utf-8")
    (root / "app").mkdir()
    (root / "app" / "main.py").write_text(
        'def run(db):\n    return db.execute("SELECT id FROM users")\n', encoding="utf-8")


def test_raw_query_ref_never_blocks_validation(monkeypatch, tmp_path):
    # MOAT GUARDRAIL: a raw-SQL string reference must NEVER produce a blocking boundary error.
    _write_raw_query_project(tmp_path)
    data = json.loads(_invoke(monkeypatch, tmp_path, ["validate"]).output)
    assert not any(i["code"] == "E_BOUNDARY_VIOLATION" for i in data["issues"])


def test_impact_include_raw_queries_is_advisory(monkeypatch, tmp_path):
    _write_raw_query_project(tmp_path)
    res = _invoke(monkeypatch, tmp_path, ["impact", "users", "--include-raw-queries"])
    data = json.loads(res.output)
    heur = data["heuristic_consumers"]
    assert any(h["subsystem"] == "app" and h["confidence"] == "heuristic" for h in heur)
    # Advisory only: the verified blast radius does not count it.
    assert data["blast_radius"] == 0


def test_prisma_schema_describe_and_impact(monkeypatch, tmp_path):
    (tmp_path / ".bounds" / "manifests").mkdir(parents=True)
    (tmp_path / ".bounds" / "root.yaml").write_text(
        'version: "1"\nproject: s\nlanguages: [prisma, python]\nenforce: "on"\nsubsystems: [db, app]\n',
        encoding="utf-8")
    (tmp_path / ".bounds" / "manifests" / "db.yaml").write_text(
        "name: db\nrole: library\ncriticality: core\npaths: [prisma]\n"
        "exposes:\n  - { name: users, kind: table }\nconsumes: []\n", encoding="utf-8")
    (tmp_path / ".bounds" / "manifests" / "app.yaml").write_text(
        "name: app\nrole: service\ncriticality: leaf\npaths: [app]\nexposes: []\n"
        "consumes:\n  - subsystem: db\n    via: reads\n    interfaces: [users]\n", encoding="utf-8")
    (tmp_path / "prisma").mkdir()
    (tmp_path / "prisma" / "schema.prisma").write_text(
        'model User {\n  id Int @id\n  email String\n  @@map("users")\n}\n', encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("def run():\n    return True\n", encoding="utf-8")

    describe = json.loads(_invoke(monkeypatch, tmp_path, ["describe", "db"]).output)
    assert describe["tables"] == [
        {"name": "users", "kind": "table", "columns": ["email", "id"], "files": ["prisma/schema.prisma"]}
    ]
    impact = json.loads(_invoke(monkeypatch, tmp_path, ["impact", "users"]).output)
    assert impact["direct_consumers"] == ["app"]


def test_cache_inspect_cli(monkeypatch, py_project):
    # Populate the cache first via a validate run, then inspect it.
    assert _invoke(monkeypatch, py_project, ["validate"]).exit_code in (0, 1)
    res = _invoke(monkeypatch, py_project, ["cache", "--inspect"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["backend"] == "sqlite"
    assert data["files"] >= 1


def test_cache_requires_one_flag(monkeypatch, py_project):
    res = _invoke(monkeypatch, py_project, ["cache"])
    assert res.exit_code == 2
    assert "E_USAGE" in res.output


def test_calibrate_cli(monkeypatch, py_project):
    res = _invoke(monkeypatch, py_project, ["calibrate"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["mode"] == "calibrate"
    # svc's main.py exports `run` but the manifest declares nothing -> proposed add.
    assert any(e["name"] == "run" for e in data["subsystems"].get("svc", {}).get("add_exposes", []))


def test_agent_sync_cli(monkeypatch, py_project):
    res = _invoke(monkeypatch, py_project, ["agent", "--sync", "--claude"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["canonical"] == "AGENTS.md"
    assert (py_project / "AGENTS.md").is_file()
    assert (py_project / ".claude" / "commands" / "bounds.md").is_file()


def test_agent_requires_one_mode(monkeypatch, py_project):
    res = _invoke(monkeypatch, py_project, ["agent"])
    assert res.exit_code == 2


def test_ci_install_cli(monkeypatch, py_project):
    res = _invoke(monkeypatch, py_project, ["ci", "--install", "--action"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert any("bounds.yml" in p for p in data["created"])
    assert (py_project / ".github" / "workflows" / "bounds.yml").is_file()


def test_ci_needs_install(monkeypatch, py_project):
    res = _invoke(monkeypatch, py_project, ["ci"])
    assert res.exit_code == 2


def test_discover_cli_runs(monkeypatch, py_project):
    res = _invoke(monkeypatch, py_project, ["discover"])
    assert res.exit_code == 0
    assert json.loads(res.output)["mode"] == "discover"


# ---------------------------------------------------------------------------
# architectural hardening (resource bounds, fail-loud, determinism)
# ---------------------------------------------------------------------------
def test_oversized_owned_file_is_skipped_loudly(monkeypatch, py_project):
    import bounds.config as cfg
    monkeypatch.setattr(cfg, "MAX_FILE_BYTES", 5)  # tiny: every real source file is "oversized"
    data = json.loads(_invoke(monkeypatch, py_project, ["validate"]).output)
    assert any(
        i["code"] == "E_EXTRACTION_FAILED" and "MAX_FILE_BYTES" in i["message"]
        for i in data["issues"]
    )
    # And coverage counts it as an extraction failure.
    assert data["stats"]["coverage"]["extraction_failures"] >= 1


def test_describe_reports_unparsed_owned_file(monkeypatch, py_project):
    import bounds.config as cfg
    monkeypatch.setattr(cfg, "MAX_FILE_BYTES", 5)
    data = json.loads(_invoke(monkeypatch, py_project, ["describe", "models"]).output)
    assert "src/models/thing.py" in data.get("unparsed_files", [])
    thing = next(e for e in data["exposes"] if e["name"] == "Thing")
    assert thing["verified"] is False  # couldn't read it — not silently "absent from source"


def test_symlink_cycle_does_not_hang(monkeypatch, py_project):
    import os
    # models/loop -> models is a directory-symlink cycle that would loop a naive rglob.
    os.symlink(py_project / "src" / "models", py_project / "src" / "models" / "loop")
    data = json.loads(_invoke(monkeypatch, py_project, ["where", "Thing"]).output)
    files = [r["file"] for r in data["results"]]
    assert files == ["src/models/thing.py"]  # found once (cycle visited at most once), no hang


def test_validate_stats_keys_are_sorted(monkeypatch, py_project):
    stats = json.loads(_invoke(monkeypatch, py_project, ["validate"]).output)["stats"]
    assert list(stats.keys()) == sorted(stats.keys())


def test_overview_edges_are_sorted(monkeypatch, py_project):
    edges = json.loads(_invoke(monkeypatch, py_project, ["overview"]).output)["edges"]
    keys = [(e["from"], e["to"]) for e in edges]
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# Edge cases / gaps
# ---------------------------------------------------------------------------
def test_where_finds_undeclared_public_symbol(monkeypatch, py_project):
    # A public symbol the manifest does not declare is still locatable, tagged exposed=False.
    (py_project / "src" / "models" / "extra.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8"
    )
    data = json.loads(_invoke(monkeypatch, py_project, ["where", "helper"]).output)
    assert data["count"] == 1
    r = data["results"][0]
    assert r["owning_subsystem"] == "models" and r["exposed"] is False


def test_where_no_bounds_is_fatal(monkeypatch, tmp_path):
    res = _invoke(monkeypatch, tmp_path, ["where", "Thing"])
    assert res.exit_code == 2
    assert "E_MANIFEST_NOT_FOUND" in res.output


def test_impact_no_bounds_is_fatal(monkeypatch, tmp_path):
    res = _invoke(monkeypatch, tmp_path, ["impact", "models"])
    assert res.exit_code == 2
    assert "E_MANIFEST_NOT_FOUND" in res.output


def test_impact_verify_unknown_subsystem_is_fatal(monkeypatch, py_project):
    # Unknown subsystem must fail fast (before any extraction), even with --verify.
    res = _invoke(monkeypatch, py_project, ["impact", "ghost", "--verify"])
    assert res.exit_code == 2
    assert "E_SUBSYSTEM_NOT_FOUND" in res.output


def test_where_empty_project_returns_no_matches(monkeypatch, tmp_path):
    # A .bounds/ with a subsystem whose paths hold no source → where returns 0, not a crash.
    (tmp_path / ".bounds" / "manifests").mkdir(parents=True)
    (tmp_path / ".bounds" / "root.yaml").write_text(
        'version: "1"\nproject: empty\nsubsystems: [thing]\n', encoding="utf-8"
    )
    (tmp_path / ".bounds" / "manifests" / "thing.yaml").write_text(
        "name: thing\nrole: library\ncriticality: leaf\npaths: [src/thing]\nexposes: []\n",
        encoding="utf-8",
    )
    res = _invoke(monkeypatch, tmp_path, ["where", "Anything"])
    assert res.exit_code == 0
    assert json.loads(res.output)["count"] == 0


def test_describe_namespace_status_is_per_subsystem(monkeypatch, py_project):
    # Both subsystems in one namespace; drift only in models. Under --namespace, each subsystem's
    # validation_status is scoped to itself while project_status is the shared rollup.
    for n in ("models", "svc"):
        p = py_project / ".bounds" / "manifests" / f"{n}.yaml"
        p.write_text("namespace: core\n" + p.read_text(encoding="utf-8"), encoding="utf-8")
    m = py_project / ".bounds" / "manifests" / "models.yaml"
    m.write_text(
        m.read_text(encoding="utf-8").replace(
            "  - { name: Thing, kind: class }",
            "  - { name: Thing, kind: class }\n  - { name: Missing, kind: class }",
        ),
        encoding="utf-8",
    )
    data = json.loads(_invoke(monkeypatch, py_project, ["describe", "--namespace", "core"]).output)
    by_name = {s["name"]: s for s in data["subsystems"]}
    assert by_name["models"]["validation_status"] == "stale"
    assert by_name["svc"]["validation_status"] == "fresh"
    assert by_name["svc"]["project_status"] == "stale"  # rollup sees the models drift


# ---------------------------------------------------------------------------
# de-spaghetti — golden / determinism guards
#
# describe's Tier-1+2 merge now lives in describe.py and reuses the shared
# scan.iter_subsystem_files + scan.extract_file (one home for the owned-file
# walk). These pin the JSON shape + byte-stability so a future refactor can't
# silently change describe/validate output.
# ---------------------------------------------------------------------------
def _strip_duration(obj):
    """Recursively drop the only non-deterministic field (stats.duration_ms)."""
    if isinstance(obj, dict):
        return {k: _strip_duration(v) for k, v in obj.items() if k != "duration_ms"}
    if isinstance(obj, list):
        return [_strip_duration(x) for x in obj]
    return obj


def test_describe_is_byte_stable_and_merges_tiers(monkeypatch, py_project):
    r1 = _invoke(monkeypatch, py_project, ["describe", "models"])
    r2 = _invoke(monkeypatch, py_project, ["describe", "models"])
    assert r1.exit_code == 0 and r2.exit_code == 0
    assert r1.output == r2.output  # byte-identical across runs (deterministic)

    data = json.loads(r1.output)
    assert data["name"] == "models"
    # Lean default: a file COUNT, not the roster (the roster is behind --full).
    assert data["file_count"] == 1
    assert "files" not in data
    assert "validation_status" in data
    thing = next(e for e in data["exposes"] if e["name"] == "Thing")
    assert thing["verified"] is True

    # --full restores the file roster (the contract — exposes — is unchanged).
    full = json.loads(_invoke(monkeypatch, py_project, ["describe", "models", "--full"]).output)
    assert full["files"] == ["src/models/thing.py"]
    assert full["file_count"] == 1
    assert thing["file"] == "src/models/thing.py"


def test_validate_json_is_byte_stable(monkeypatch, py_project):
    _invoke(monkeypatch, py_project, ["validate"])  # warm the cache first
    r1 = _invoke(monkeypatch, py_project, ["validate"])
    r2 = _invoke(monkeypatch, py_project, ["validate"])
    assert r1.exit_code in (0, 1) and r2.exit_code in (0, 1)
    assert _strip_duration(json.loads(r1.output)) == _strip_duration(json.loads(r2.output))


# ---------------------------------------------------------------------------
# subsystem-scoped describe status + coverage signals
# ---------------------------------------------------------------------------
def test_describe_status_is_subsystem_scoped(monkeypatch, py_project):
    # Introduce drift in `models` only (declare a class the source doesn't export).
    m = py_project / ".bounds" / "manifests" / "models.yaml"
    m.write_text(
        m.read_text(encoding="utf-8").replace(
            "  - { name: Thing, kind: class }",
            "  - { name: Thing, kind: class }\n  - { name: Missing, kind: class }",
        ),
        encoding="utf-8",
    )
    svc = json.loads(_invoke(monkeypatch, py_project, ["describe", "svc"]).output)
    assert svc["validation_status"] == "fresh"   # svc itself is clean...
    assert svc["project_status"] == "stale"      # ...but the project has drift (in models)

    models = json.loads(_invoke(monkeypatch, py_project, ["describe", "models"]).output)
    assert models["validation_status"] == "stale"   # the drift is scoped to models
    assert models["project_status"] == "stale"


# ---------------------------------------------------------------------------
# bounds where
# ---------------------------------------------------------------------------
def test_where_python_exact(monkeypatch, py_project):
    data = json.loads(_invoke(monkeypatch, py_project, ["where", "Thing"]).output)
    assert data["match"] == "exact" and data["count"] == 1
    r = data["results"][0]
    assert r["file"] == "src/models/thing.py"
    assert r["owning_subsystem"] == "models"
    assert r["exposed"] is True   # Thing is declared in models.exposes
    assert r["kind"] == "class"


def test_where_prefix(monkeypatch, py_project):
    data = json.loads(_invoke(monkeypatch, py_project, ["where", "Th", "--prefix"]).output)
    assert data["match"] == "prefix"
    assert any(r["symbol"] == "Thing" for r in data["results"])


def test_where_ts_exposed_flag(monkeypatch, sample_project):
    # TS parity + exposed flag: login is declared (exposed); the internal UserRepository is not.
    login = json.loads(_invoke(monkeypatch, sample_project, ["where", "login"]).output)["results"]
    assert login and login[0]["owning_subsystem"] == "auth" and login[0]["exposed"] is True
    repo = json.loads(_invoke(monkeypatch, sample_project, ["where", "UserRepository"]).output)["results"]
    assert repo and repo[0]["owning_subsystem"] == "database" and repo[0]["exposed"] is False


def test_where_collision_returns_all_sorted(monkeypatch, py_project):
    # Same symbol name defined in two subsystems → both reported, deterministically sorted by file.
    (py_project / "src" / "svc" / "dup.py").write_text("class Thing:\n    pass\n", encoding="utf-8")
    data = json.loads(_invoke(monkeypatch, py_project, ["where", "Thing"]).output)
    assert [r["file"] for r in data["results"]] == ["src/models/thing.py", "src/svc/dup.py"]


def test_where_not_found(monkeypatch, py_project):
    data = json.loads(_invoke(monkeypatch, py_project, ["where", "Nonexistent"]).output)
    assert data["count"] == 0 and data["results"] == []


def test_validate_reports_coverage_block(monkeypatch, py_project):
    cov = json.loads(_invoke(monkeypatch, py_project, ["validate"]).output)["stats"]["coverage"]
    assert cov["files_owned"] >= 2
    assert cov["unresolved_local_imports"] == 0
    assert cov["extraction_failures"] == 0


def test_coverage_counts_local_unresolved_not_external(monkeypatch, py_project):
    # A relative import to a non-existent local module is local-looking but unresolved (counted);
    # a stdlib import (os) is not local-looking and must NOT inflate the count.
    (py_project / "src" / "svc" / "extra.py").write_text(
        "import os\nfrom ..models.ghost import Ghost\n", encoding="utf-8"
    )
    cov = json.loads(_invoke(monkeypatch, py_project, ["validate"]).output)["stats"]["coverage"]
    assert cov["unresolved_local_imports"] == 1


def test_quick_flags_oversized_unchanged_file(monkeypatch, py_project, git_init):
    # PR fix: the oversized guard runs BEFORE the quick-mode cache hit, so an unchanged-but-
    # oversized owned file is flagged loudly, never silently served from a stale cache record.
    import bounds.config as cfg
    git_init(py_project)
    _invoke(monkeypatch, py_project, ["validate"])  # warm the cache at normal size
    monkeypatch.setattr(cfg, "MAX_FILE_BYTES", 5)   # now every file is "oversized"
    data = json.loads(_invoke(monkeypatch, py_project, ["validate", "--quick"]).output)
    assert any(
        i["code"] == "E_EXTRACTION_FAILED" and "MAX_FILE_BYTES" in i["message"]
        for i in data["issues"]
    )


def test_walk_does_not_descend_external_symlink_dir(monkeypatch, py_project, tmp_path):
    # PR fix: a directory symlink that escapes the repo is never traversed, so `where` cannot
    # surface symbols from outside the project (no arbitrary external extraction / unbounded walk).
    import os
    external = tmp_path / "outside"
    external.mkdir()
    (external / "secret.py").write_text("def Sekret():\n    pass\n", encoding="utf-8")
    os.symlink(external, py_project / "src" / "models" / "ext")
    data = json.loads(_invoke(monkeypatch, py_project, ["where", "Sekret"]).output)
    assert data["count"] == 0


# ---------------------------------------------------------------------------
# Human renderers: action/query commands now produce summaries, not raw JSON
# dumps; and (non-TTY) they still emit the JSON contract by default.
# ---------------------------------------------------------------------------
def test_human_renderers_are_clean_summaries(monkeypatch, py_project):
    lst = _invoke(monkeypatch, py_project, ["list", "--human"]).output
    assert "subsystem" in lst and not lst.lstrip().startswith("{")  # a summary, not JSON
    ov = _invoke(monkeypatch, py_project, ["overview", "--human"]).output
    assert "roles:" in ov and "dependency edges" in ov
    im = _invoke(monkeypatch, py_project, ["impact", "models", "--human"]).output
    assert "blast radius" in im and "consumers" in im
    cal = _invoke(monkeypatch, py_project, ["calibrate", "--human"]).output
    assert cal.startswith("calibrate:")


def test_action_commands_emit_json_when_not_a_tty(monkeypatch, py_project):
    # CliRunner stdout is not a TTY (like an agent/pipe), so TTY-aware action commands
    # must still emit the JSON contract by default — agents are never given the prose view.
    out = _invoke(monkeypatch, py_project, ["cache", "--inspect"]).output
    assert json.loads(out)["backend"] in ("sqlite", "json", "none")
    # ...and a query command stays JSON-default regardless.
    assert json.loads(_invoke(monkeypatch, py_project, ["overview"]).output)["project"]
