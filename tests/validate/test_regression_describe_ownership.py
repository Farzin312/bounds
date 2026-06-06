"""Regression guards for the describe/locate ownership bugs (BOUNDS-006, BOUNDS-010, BOUNDS-007).

BOUNDS-006: ``describe`` counted files via the blind owned-file walk, so a parent subsystem whose
path is an ancestor of a child's double-counted the child's files — disagreeing with ``validate``,
which already used most-specific-path-wins ownership (``scan.resolve_owners``). describe now resolves
ownership the same way, and additionally flags a *genuine* same-path conflict (two subsystems
declaring the identical path at equal specificity) as a non-fatal ``E_SUBSYSTEM_OVERLAP`` diagnostic.

BOUNDS-010: the describe JSON omitted ``files`` while ``--full``/--human rendered the roster — a
JSON-first violation. The JSON now carries ``files`` under the same gate the human view uses.

BOUNDS-007: ``where`` took a symbol only, so a path-shaped arg returned zero results; it now detects a
path-shaped query and reports the file's owning subsystem plus the symbols it defines.
"""
from __future__ import annotations

from pathlib import Path

from bounds.core import describe
from bounds.shared import errors
from bounds.shared.ignore import IgnoreMatcher
from bounds.core.manifest import loader


def _nested_project(tmp_path: Path) -> Path:
    """parent `alpha` (src/alpha) is an ancestor of child `beta` (src/alpha/beta); alpha sorts first."""
    cfg = tmp_path / ".bounds"
    (cfg / "manifests").mkdir(parents=True)
    (cfg / "root.yaml").write_text(
        'version: "1"\nproject: nested\nlanguages: [python]\nsubsystems: [alpha, beta]\n',
        encoding="utf-8",
    )
    (cfg / "manifests" / "alpha.yaml").write_text(
        "name: alpha\nrole: library\ncriticality: leaf\npaths:\n  - src/alpha\n"
        "exposes:\n  - { name: parent_fn, kind: function }\n",
        encoding="utf-8",
    )
    (cfg / "manifests" / "beta.yaml").write_text(
        "name: beta\nrole: library\ncriticality: leaf\npaths:\n  - src/alpha/beta\n"
        "exposes:\n  - { name: child_fn, kind: function }\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "alpha" / "beta").mkdir(parents=True)
    (tmp_path / "src" / "alpha" / "core.py").write_text("def parent_fn():\n    pass\n", encoding="utf-8")
    (tmp_path / "src" / "alpha" / "beta" / "mod.py").write_text("def child_fn():\n    pass\n", encoding="utf-8")
    return tmp_path


def _overlap_project(tmp_path: Path) -> Path:
    """Two subsystems (`aaa`, `zzz`) declare the IDENTICAL path src/shared — a genuine ambiguity."""
    cfg = tmp_path / ".bounds"
    (cfg / "manifests").mkdir(parents=True)
    (cfg / "root.yaml").write_text(
        'version: "1"\nproject: ov\nlanguages: [python]\nsubsystems: [aaa, zzz]\n', encoding="utf-8"
    )
    (cfg / "manifests" / "aaa.yaml").write_text(
        "name: aaa\nrole: library\ncriticality: leaf\npaths:\n  - src/shared\nexposes: []\n", encoding="utf-8"
    )
    (cfg / "manifests" / "zzz.yaml").write_text(
        "name: zzz\nrole: library\ncriticality: leaf\npaths:\n  - src/shared\nexposes: []\n", encoding="utf-8"
    )
    (tmp_path / "src" / "shared").mkdir(parents=True)
    (tmp_path / "src" / "shared" / "x.py").write_text("def x():\n    pass\n", encoding="utf-8")
    return tmp_path


def _describe(root: Path, name: str, full: bool = False) -> dict:
    rootm, subs, _ = loader.load_all(root)
    em = IgnoreMatcher(rootm.entry_points)
    return describe.describe_one(root, subs[name], False, None, em, full=full)


def test_describe_file_count_does_not_double_count_nested_child(tmp_path):
    """BOUNDS-006: a parent subsystem counts only its own files, not a more-specific child's."""
    _nested_project(tmp_path)
    alpha = _describe(tmp_path, "alpha", full=True)
    beta = _describe(tmp_path, "beta", full=True)
    # alpha must NOT swallow beta's file: most-specific-path-wins, the same map validate uses.
    assert alpha["file_count"] == 1
    assert alpha["files"] == ["src/alpha/core.py"]
    assert beta["file_count"] == 1
    assert beta["files"] == ["src/alpha/beta/mod.py"]


def test_describe_agrees_with_resolve_owners(tmp_path):
    """BOUNDS-006: describe's file roster equals scan.resolve_owners' assignment for the subsystem."""
    from bounds.core.extract import scan

    _nested_project(tmp_path)
    _root, subs, _ = loader.load_all(tmp_path)
    owners = scan.resolve_owners(tmp_path, subs, {".py"})
    expected_alpha = sorted(rel for rel, (o, _a) in owners.items() if o == "alpha")
    assert _describe(tmp_path, "alpha", full=True)["files"] == expected_alpha


def test_describe_owned_files_respects_boundsignore(tmp_path):
    """BOUNDS-006 parity (PR #31 review): describe's roster must exclude a .boundsignore'd file
    exactly as validate skips it — else file_count/files (and Tier-1 extraction) count files validate
    never scans, breaking the very parity this fix is for. Locks the ignore-aware ownership path."""
    cfg = tmp_path / ".bounds"
    (cfg / "manifests").mkdir(parents=True)
    (cfg / "root.yaml").write_text(
        'version: "1"\nproject: ig\nlanguages: [python]\nsubsystems: [alpha]\n', encoding="utf-8")
    (cfg / "manifests" / "alpha.yaml").write_text(
        "name: alpha\nrole: library\ncriticality: leaf\npaths:\n  - src/alpha\n", encoding="utf-8")
    (tmp_path / "src" / "alpha").mkdir(parents=True)
    (tmp_path / "src" / "alpha" / "keep.py").write_text("def kept():\n    pass\n", encoding="utf-8")
    (tmp_path / "src" / "alpha" / "skip.py").write_text("def skipped():\n    pass\n", encoding="utf-8")
    (tmp_path / ".boundsignore").write_text("src/alpha/skip.py\n", encoding="utf-8")
    rootm, subs, _ = loader.load_all(tmp_path)
    em = IgnoreMatcher(rootm.entry_points)
    payload = describe.describe_one(tmp_path, subs["alpha"], False, None, em, full=True)
    assert payload["files"] == ["src/alpha/keep.py"]  # skip.py excluded by .boundsignore
    assert payload["file_count"] == 1


def test_nested_paths_are_not_flagged_as_overlap(tmp_path):
    """BOUNDS-006: differing specificity (nested paths) is legitimate ownership, never an overlap —
    asserted under --full (where the overlap diagnostic is actually computed)."""
    _nested_project(tmp_path)
    assert "overlaps" not in _describe(tmp_path, "alpha", full=True)
    assert "overlaps" not in _describe(tmp_path, "beta", full=True)


def test_same_path_conflict_emits_subsystem_overlap_issue(tmp_path):
    """BOUNDS-006: two subsystems declaring the identical path emit a non-fatal E_SUBSYSTEM_OVERLAP."""
    _overlap_project(tmp_path)
    aaa = _describe(tmp_path, "aaa", full=True)  # overlap diagnostic is a --full output
    zzz = _describe(tmp_path, "zzz", full=True)
    # The file is still deterministically owned (sorted-first wins) — describe does not crash.
    assert aaa["file_count"] == 1  # 'aaa' < 'zzz' so it wins the tie
    assert zzz["file_count"] == 0
    for payload, other in ((aaa, "zzz"), (zzz, "aaa")):
        overlaps = payload.get("overlaps")
        assert overlaps, "expected an overlap diagnostic on a genuine same-path conflict"
        issue = overlaps[0]
        assert issue["code"] == errors.E_SUBSYSTEM_OVERLAP
        assert issue["severity"] == "warning"  # advisory, never blocks
        assert issue["file"] == "src/shared/x.py"
        assert other in issue["fix"]


def test_subsystem_overlap_code_is_registered_with_severity():
    """The new code is in the append-only registry with its warning severity mapping."""
    assert errors.E_SUBSYSTEM_OVERLAP == "E_SUBSYSTEM_OVERLAP"
    assert errors.SEVERITY[errors.E_SUBSYSTEM_OVERLAP] == "warning"


def test_describe_files_json_human_parity(tmp_path):
    """BOUNDS-010: the JSON carries `files` exactly when --full does — the gate the human view uses.

    JSON-first invariant: the human renderer lists the roster only when payload['files'] is present,
    so the JSON must carry it under the same gate (never render a list the JSON omitted).
    """
    from bounds.shared import output

    _nested_project(tmp_path)
    default = _describe(tmp_path, "alpha", full=False)
    full = _describe(tmp_path, "alpha", full=True)
    # Default: count present, list omitted (token-lean) — and the human view falls back to the count.
    assert "files" not in default
    assert default["file_count"] == 1
    assert "(use --full to list)" in output._render_subsystem_human(default)
    # --full: list present in the JSON, and the human view renders that SAME list, not more.
    assert full["files"] == ["src/alpha/core.py"]
    human = output._render_subsystem_human(full)
    assert "src/alpha/core.py" in human


def _project_with_docs_tests(tmp_path: Path) -> Path:
    """A subsystem `auth` (src/auth) with a convention-linked test dir and an explicit doc."""
    cfg = tmp_path / ".bounds"
    (cfg / "manifests").mkdir(parents=True)
    (cfg / "root.yaml").write_text(
        'version: "1"\nproject: dt\nlanguages: [python]\nsubsystems: [auth]\n', encoding="utf-8"
    )
    (cfg / "manifests" / "auth.yaml").write_text(
        "name: auth\nrole: library\ncriticality: leaf\npaths:\n  - src/auth\n"
        "docs:\n  - docs/auth.md\nexposes:\n  - { name: login, kind: function }\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "auth").mkdir(parents=True)
    (tmp_path / "src" / "auth" / "login.py").write_text("def login():\n    pass\n", encoding="utf-8")
    (tmp_path / "tests" / "auth").mkdir(parents=True)
    (tmp_path / "tests" / "auth" / "test_login.py").write_text("def test_login():\n    pass\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "auth.md").write_text("# auth\n", encoding="utf-8")
    return tmp_path


def test_describe_docs_tests_gated_under_full(tmp_path):
    """BOUNDS-010 parity: describe surfaces linked docs/tests (explicit + convention) only under
    --full, in BOTH the JSON and the human view — a default describe stays token-lean."""
    from bounds.shared import output

    _project_with_docs_tests(tmp_path)
    default = _describe(tmp_path, "auth", full=False)
    full = _describe(tmp_path, "auth", full=True)
    # Default: omitted (token-lean), even though the manifest declares docs and convention links tests.
    assert "docs" not in default
    assert "tests" not in default
    # --full: resolved set present in the JSON (explicit doc + convention-linked test).
    assert full["docs"] == ["docs/auth.md"]
    assert full["tests"] == ["tests/auth/test_login.py"]
    # …and the human view renders the SAME data (JSON-first parity).
    human = output._render_subsystem_human(full)
    assert "docs/auth.md" in human
    assert "tests/auth/test_login.py" in human
