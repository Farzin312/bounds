"""End-to-end CLI smoke tests for the gen-3 commands (via Click's CliRunner)."""

from __future__ import annotations

import json

from click.testing import CliRunner

from bounds.cli import main


def _invoke(monkeypatch, cwd, args):
    monkeypatch.chdir(cwd)
    return CliRunner().invoke(main, args)


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
    assert data["files"] == ["src/models/thing.py"]
    assert "validation_status" in data
    thing = next(e for e in data["exposes"] if e["name"] == "Thing")
    assert thing["verified"] is True
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
