"""Mapping-coverage honesty: Bounds must report how much source it mapped and what it could not.

Polyglot repos were the motivation — Bounds supports only some languages, so it used to silently map
the supported files and leave the rest dark. These tests lock in the loud, by-language coverage
signal (`scan.mapping_coverage` + the `E_COVERAGE_GAP` warning). See docs/coverage.md.
"""
from __future__ import annotations

import subprocess

from bounds import config, errors
from bounds.extract import scan
from bounds.validate import engine


def _git_init(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


def _polyglot(tmp_path):
    """3 mapped Python files + 5 unsupported Go files."""
    svc = tmp_path / "svc"
    go = tmp_path / "goservice"
    svc.mkdir()
    go.mkdir()
    for i in range(3):
        (svc / f"m{i}.py").write_text(f"def f{i}():\n    pass\n")
    for i in range(5):
        (go / f"f{i}.go").write_text(f"package main\nfunc F{i}() {{}}\n")
    cfg = tmp_path / config.BOUNDS_DIR
    (cfg / config.MANIFESTS_DIR).mkdir(parents=True)
    (cfg / config.ROOT_FILE).write_text(
        'version: "1"\nproject: poly\nlanguages: [python]\nsubsystems: [svc]\n'
    )
    (cfg / config.MANIFESTS_DIR / "svc.yaml").write_text(
        "name: svc\nrole: library\ncriticality: leaf\npaths:\n  - svc\nexposes: []\n"
    )
    _git_init(tmp_path)
    return tmp_path


def test_mapping_coverage_counts_unsupported_by_language(tmp_path):
    """mapping_coverage must count unsupported files (Go) as unmapped and bucket them by language; docs/config/assets must never dilute the source denominator."""
    _polyglot(tmp_path)
    cov = scan.mapping_coverage(tmp_path, {"svc/m0.py", "svc/m1.py", "svc/m2.py"})
    assert cov["files_source_total"] == 8
    assert cov["files_mapped"] == 3
    assert cov["files_unmapped"] == 5
    assert cov["mapped_pct"] == 37.5
    assert cov["unmapped_by_language"] == {"go": 5}
    assert cov["unsupported_languages"] == ["go"]
    # The dict now always carries informational tests/docs buckets (empty here — no tests/docs).
    assert cov["tests"] == {"total": 0, "linked": 0, "unlinked": 0, "unlinked_sample": []}
    assert cov["docs"] == {"total": 0, "linked": 0, "unlinked": 0, "unlinked_sample": []}
    # docs/config/assets must NOT dilute the denominator.
    (tmp_path / "README.md").write_text("# docs\n")
    (tmp_path / "data.json").write_text("{}\n")
    cov2 = scan.mapping_coverage(tmp_path, {"svc/m0.py", "svc/m1.py", "svc/m2.py"})
    assert cov2["files_source_total"] == 8  # unchanged: non-source excluded
    assert cov2["docs"]["total"] == 1  # README.md now tracked as a doc (unlinked → never a gap)


def test_test_files_excluded_from_source_denominator_and_never_a_gap(tmp_path):
    """A repo's tests must never count as unmapped source: they're bucketed separately, so an
    all-source-mapped repo with unlinked tests still reports 100% and emits no E_COVERAGE_GAP."""
    svc = tmp_path / "svc"
    svc.mkdir()
    for i in range(3):
        (svc / f"m{i}.py").write_text(f"def f{i}():\n    pass\n")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    for i in range(4):  # four UNLINKED test files in a top-level tests/ dir (no `svc` segment)
        (tests_dir / f"test_thing{i}.py").write_text("def test_x():\n    assert True\n")
    cfg = tmp_path / config.BOUNDS_DIR
    (cfg / config.MANIFESTS_DIR).mkdir(parents=True)
    (cfg / config.ROOT_FILE).write_text(
        'version: "1"\nproject: t\nlanguages: [python]\nsubsystems: [svc]\n'
    )
    (cfg / config.MANIFESTS_DIR / "svc.yaml").write_text(
        "name: svc\nrole: library\ncriticality: leaf\npaths:\n  - svc\nexposes: []\n"
    )
    _git_init(tmp_path)
    report = engine.run(tmp_path, mode="full", enforce="on", include_gitignored=True)
    mapping = report.stats["coverage"]["mapping"]
    # The 4 tests are NOT in the source denominator (only the 3 mapped svc files are).
    assert mapping["files_source_total"] == 3
    assert mapping["mapped_pct"] == 100.0
    assert [i for i in report.issues if i.code == errors.E_COVERAGE_GAP] == []
    # …but they ARE tracked in the tests bucket (unlinked here — informational, not a gap).
    assert mapping["tests"]["total"] == 4
    assert mapping["tests"]["unlinked"] == 4


def test_validate_emits_loud_coverage_gap_nonblocking(tmp_path):
    """A coverage gap is a loud warning with a next step — visible, but never blocks on its own."""
    _polyglot(tmp_path)
    report = engine.run(tmp_path, mode="full", enforce="on", include_gitignored=True)
    gaps = [i for i in report.issues if i.code == errors.E_COVERAGE_GAP]
    assert len(gaps) == 1
    assert "37.5%" in gaps[0].message and "go" in gaps[0].message
    fix = gaps[0].fix
    assert fix  # actionable next step present
    # The fix distinguishes the unsupported-language gap (no adapter → hand-author a DURABLE manifest)
    # and points teams who want a hard gate at the opt-in flag — neither should be silently dropped.
    assert "hand-author" in fix.lower()
    assert "durable" in fix.lower()
    assert "--fail-on-unowned" in fix
    assert "docs/coverage.md" in fix
    assert gaps[0].severity == "warning"
    # the gap alone is non-blocking (advisory); enforce=on still reports ok unless a real error exists
    assert report.stats["coverage"]["mapping"]["mapped_pct"] == 37.5


def test_full_coverage_has_no_gap(tmp_path):
    """An all-supported, fully-owned repo reports 100% and emits no coverage gap."""
    svc = tmp_path / "svc"
    svc.mkdir()
    for i in range(3):
        (svc / f"m{i}.py").write_text(f"def f{i}():\n    pass\n")
    cfg = tmp_path / config.BOUNDS_DIR
    (cfg / config.MANIFESTS_DIR).mkdir(parents=True)
    (cfg / config.ROOT_FILE).write_text(
        'version: "1"\nproject: full\nlanguages: [python]\nsubsystems: [svc]\n'
    )
    (cfg / config.MANIFESTS_DIR / "svc.yaml").write_text(
        "name: svc\nrole: library\ncriticality: leaf\npaths:\n  - svc\nexposes: []\n"
    )
    _git_init(tmp_path)
    report = engine.run(tmp_path, mode="full", include_gitignored=True)
    assert [i for i in report.issues if i.code == errors.E_COVERAGE_GAP] == []
    assert report.stats["coverage"]["mapping"]["mapped_pct"] == 100.0


def test_quick_mode_skips_coverage_scan(tmp_path):
    """--quick stays on the hot path: no full-tree coverage walk, no mapping block."""
    _polyglot(tmp_path)
    report = engine.run(tmp_path, mode="quick", include_gitignored=True)
    assert "mapping" not in report.stats["coverage"]
    assert [i for i in report.issues if i.code == errors.E_COVERAGE_GAP] == []


def test_gitignored_owned_file_not_counted_as_unmapped(tmp_path):
    """Gitignore parity: a gitignored file owned by a subsystem is excluded from both numerator and
    denominator, so it never falsely registers as unmapped or fires a spurious coverage gap."""
    svc = tmp_path / "svc"
    svc.mkdir()
    (svc / "real.py").write_text("def f():\n    pass\n")
    (svc / "gen.py").write_text("def g():\n    pass\n")     # owned by svc/ but gitignored
    (tmp_path / ".gitignore").write_text("svc/gen.py\n")
    cfg = tmp_path / config.BOUNDS_DIR
    (cfg / config.MANIFESTS_DIR).mkdir(parents=True)
    (cfg / config.ROOT_FILE).write_text(
        'version: "1"\nproject: gi\nlanguages: [python]\nsubsystems: [svc]\n'
    )
    (cfg / config.MANIFESTS_DIR / "svc.yaml").write_text(
        "name: svc\nrole: library\ncriticality: leaf\npaths:\n  - svc\nexposes: []\n"
    )
    _git_init(tmp_path)
    report = engine.run(tmp_path, mode="full")  # default include_gitignored=False
    assert report.stats["coverage"]["mapping"]["mapped_pct"] == 100.0
    assert [i for i in report.issues if i.code == errors.E_COVERAGE_GAP] == []
