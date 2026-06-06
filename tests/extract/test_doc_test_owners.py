"""Doc/test ownership resolution: link a subsystem to the docs and tests that cover it.

These lock in the hybrid model (explicit manifest `docs:`/`tests:` ALWAYS wins over convention
auto-detection), the convention rules (name-match / under-path), the unlinked→None case, and the
determinism of the tie-break — the foundation of "map source↔docs↔tests" coverage. See
docs/coverage.md and `scan.resolve_test_owners`/`scan.resolve_doc_owners`.
"""
from __future__ import annotations

from bounds.core.extract import scan
from bounds.shared.models import SubsystemCompact


def _sub(name, paths=None, tests=None, docs=None, files=None):
    return SubsystemCompact(
        name=name, paths=list(paths or []), tests=list(tests or []),
        docs=list(docs or []), files=list(files or []),
    )


def test_is_test_file_moved_to_scan_recognizes_conventions(tmp_path):
    """is_test_file (moved from discover to scan as the single shared home) still recognizes the
    pytest/Jest/Vitest/Mocha conventions — a directory segment or a filename marker."""
    assert scan.is_test_file("tests/test_auth.py")
    assert scan.is_test_file("src/auth/auth.test.ts")
    assert scan.is_test_file("e2e/login.spec.ts")
    assert scan.is_test_file("conftest.py")
    assert not scan.is_test_file("src/auth/login.py")
    assert not scan.is_test_file("README.md")


def test_is_test_symbol_moved_to_scan(tmp_path):
    """is_test_symbol (moved to scan) still flags a test_* function / Test* class as a test case,
    not a consumable interface — so discover keeps these out of a subsystem's public surface."""
    assert scan.is_test_symbol("test_login", "function")
    assert scan.is_test_symbol("TestAuth", "class")
    assert not scan.is_test_symbol("login", "function")
    assert not scan.is_test_symbol("AuthService", "class")


def test_explicit_tests_wins_over_convention(tmp_path):
    """Explicit `tests:` is authoritative: when one subsystem declares a test file by name, it owns
    it even if convention would otherwise route it elsewhere — human curation always wins."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_shared.py").write_text("def test_x():\n    pass\n")
    # `auth` claims it explicitly; `billing` would catch nothing by convention here.
    auth = _sub("auth", paths=["src/auth"], tests=["tests/test_shared.py"])
    billing = _sub("billing", paths=["src/billing"])
    owners = scan.resolve_test_owners(tmp_path, {"auth": auth, "billing": billing})
    assert owners["tests/test_shared.py"] == "auth"


def test_convention_test_under_subsystem_path(tmp_path):
    """A test file living directly under a subsystem's declared `paths` belongs to it by convention
    (deepest declared path wins) — the common in-tree `__tests__` layout maps with zero config."""
    d = tmp_path / "src" / "auth" / "__tests__"
    d.mkdir(parents=True)
    (d / "login.test.ts").write_text("test('x', () => {});\n")
    auth = _sub("auth", paths=["src/auth"])
    owners = scan.resolve_test_owners(tmp_path, {"auth": auth})
    assert owners["src/auth/__tests__/login.test.ts"] == "auth"


def test_convention_test_name_match_by_area_segment(tmp_path):
    """A `tests/<area>/...` path segment maps to a subsystem named `<area>` when one exists — the
    standard mirrored-tests layout (tests/auth/ ↔ subsystem `auth`) links automatically."""
    d = tmp_path / "tests" / "auth"
    d.mkdir(parents=True)
    (d / "test_login.py").write_text("def test_login():\n    pass\n")
    auth = _sub("auth", paths=["src/auth"])
    owners = scan.resolve_test_owners(tmp_path, {"auth": auth})
    assert owners["tests/auth/test_login.py"] == "auth"


def test_convention_test_filename_area_match(tmp_path):
    """A filename `test_<area>.py` (or `<area>.test.ts`/`<area>.spec.ts`) maps to a subsystem named
    `<area>` — a flat tests/ dir with per-area test files still links by name."""
    d = tmp_path / "tests"
    d.mkdir()
    (d / "test_billing.py").write_text("def test_b():\n    pass\n")
    billing = _sub("billing", paths=["src/billing"])
    owners = scan.resolve_test_owners(tmp_path, {"billing": billing})
    assert owners["tests/test_billing.py"] == "billing"


def test_unlinked_test_is_none_never_a_gap(tmp_path):
    """A test that matches no subsystem (by explicit, path, or name) resolves to None — unlinked is
    informational, never an error, so a repo's miscellaneous tests are never flagged."""
    d = tmp_path / "tests"
    d.mkdir()
    (d / "test_orphan.py").write_text("def test_o():\n    pass\n")
    auth = _sub("auth", paths=["src/auth"])
    owners = scan.resolve_test_owners(tmp_path, {"auth": auth})
    assert owners["tests/test_orphan.py"] is None


def test_test_owner_tie_break_is_sorted_first_name(tmp_path):
    """Equal-specificity explicit declarations break to the sorted-first subsystem name, so
    ownership is deterministic (never dependent on dict/set iteration order)."""
    d = tmp_path / "tests"
    d.mkdir()
    (d / "test_x.py").write_text("def test_x():\n    pass\n")
    # Both declare the identical file at equal specificity → sorted-first ("aaa") must win.
    zzz = _sub("zzz", tests=["tests/test_x.py"])
    aaa = _sub("aaa", tests=["tests/test_x.py"])
    owners = scan.resolve_test_owners(tmp_path, {"zzz": zzz, "aaa": aaa})
    assert owners["tests/test_x.py"] == "aaa"


def test_explicit_docs_wins_over_convention(tmp_path):
    """Explicit `docs:` is authoritative over the `docs/<name>.md` convention — a doc named for one
    subsystem can be deliberately reassigned to another via the manifest."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "auth.md").write_text("# auth\n")
    # By convention docs/auth.md → `auth`; explicit `docs:` on `billing` overrides that.
    auth = _sub("auth", paths=["src/auth"])
    billing = _sub("billing", paths=["src/billing"], docs=["docs/auth.md"])
    owners = scan.resolve_doc_owners(tmp_path, {"auth": auth, "billing": billing})
    assert owners["docs/auth.md"] == "billing"


def test_convention_doc_stem_matches_subsystem_name(tmp_path):
    """A `docs/<name>.md` (or `<name>.md`) whose stem equals a subsystem name links by convention —
    the conventional per-subsystem doc page maps with no manifest edit."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "auth.md").write_text("# auth\n")
    (tmp_path / "billing.md").write_text("# billing\n")
    auth = _sub("auth", paths=["src/auth"])
    billing = _sub("billing", paths=["src/billing"])
    owners = scan.resolve_doc_owners(tmp_path, {"auth": auth, "billing": billing})
    assert owners["docs/auth.md"] == "auth"
    assert owners["billing.md"] == "billing"


def test_unlinked_doc_is_none(tmp_path):
    """A doc matching no subsystem resolves to None (informational) — docs are ALWAYS tracked, never
    a coverage gap, so unrelated docs never block."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "architecture.md").write_text("# arch\n")
    auth = _sub("auth", paths=["src/auth"])
    owners = scan.resolve_doc_owners(tmp_path, {"auth": auth})
    assert owners["docs/architecture.md"] is None


def test_doc_exts_recognized(tmp_path):
    """Doc resolution covers .md/.mdx/.rst (config.DOC_EXTS) and ignores non-doc files, so the docs
    bucket reflects real documentation, not arbitrary text."""
    for ext in (".md", ".mdx", ".rst"):
        (tmp_path / f"auth{ext}").write_text("doc\n")
    (tmp_path / "auth.txt").write_text("not a doc\n")
    auth = _sub("auth")
    owners = scan.resolve_doc_owners(tmp_path, {"auth": auth})
    assert set(owners) == {"auth.md", "auth.mdx", "auth.rst"}  # .txt is not a doc ext
    assert all(o == "auth" for o in owners.values())
