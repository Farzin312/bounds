"""Tests for the single import resolver (resolve_import) and its per-language edge semantics.

The resolver is the single point where a recorded import string becomes a dependency edge, and it is
LANGUAGE-SENSITIVE: a "." means a package separator in Python but a literal filename character in
TS/JS. Conflating the two silently drops edges in large TS backends.
"""

from __future__ import annotations

from bounds.core.validate.checks import resolve_import


# ===========================================================================
# Import resolution
# ===========================================================================
def test_resolve_relative_typescript_index():
    """A relative TS import of a directory resolves to its index.ts barrel, the canonical TS package entry."""
    known = {"src/database/index": "src/database/index.ts", "src/auth/index": "src/auth/index.ts"}
    assert resolve_import("src/auth/index.ts", "../database", known) == "src/database/index.ts"


def test_resolve_python_dotted_relative():
    """A Python ``..models`` relative import resolves up one package then to the module — dots are package separators here."""
    known = {"src/bounds/models": "src/bounds/models.py"}
    assert resolve_import("src/bounds/extract/base.py", "..models", known) == "src/bounds/models.py"


def test_resolve_relative_typescript_dotted_filename():
    """NestJS/Angular files are dot-named (`auth.service.ts`); a relative import of one must
    resolve. Regression for the bug where `./auth.service` became the bogus stem
    `src/auth/auth/service` and silently produced no edge."""
    known = {"src/auth/auth.service": "src/auth/auth.service.ts",
             "src/auth/auth.module": "src/auth/auth.module.ts"}
    assert resolve_import("src/auth/auth.module.ts", "./auth.service", known) == "src/auth/auth.service.ts"


def test_resolve_relative_typescript_dotted_parent_dir():
    """`../dto/login.dto` from a dot-named importer resolves up-and-over without mangling dots."""
    known = {"src/auth/dto/login.dto": "src/auth/dto/login.dto.ts"}
    assert (resolve_import("src/auth/guards/jwt.guard.ts", "../dto/login.dto", known)
            == "src/auth/dto/login.dto.ts")


def test_resolve_external_is_none():
    """A bare package specifier with no local file resolves to None, so external deps never fabricate an intra-repo edge."""
    known = {"src/auth/index": "src/auth/index.ts"}
    assert resolve_import("src/auth/index.ts", "express", known) is None


# ===========================================================================
# Per-language import-resolution matrix
# ---------------------------------------------------------------------------
# The resolver is the single point where a recorded import string becomes a
# dependency edge, and it is LANGUAGE-SENSITIVE: a "." means a package separator
# in Python but a literal filename character in TS/JS. Conflating the two silently
# drops edges in large TS backends. These tests lock each
# supported language's distinct resolution semantics so a future change to one
# dialect can't quietly break another.
# ===========================================================================

# ---- Python: dots are package separators ----
def test_py_resolve_sibling_module():
    """Python ``.sibling`` resolves to a same-package module — the single leading dot means 'this package'."""
    known = {"pkg/sibling": "pkg/sibling.py"}
    assert resolve_import("pkg/mod.py", ".sibling", known) == "pkg/sibling.py"


def test_py_resolve_parent_package_dotted_segments():
    """`..a.b` → up one package, then a/b — the dots after the leading dots ARE separators."""
    known = {"pkg/a/b": "pkg/a/b.py"}
    assert resolve_import("pkg/sub/mod.py", "..a.b", known) == "pkg/a/b.py"


def test_py_resolve_package_via_init():
    """A Python package import resolves to its ``__init__.py`` so package-level edges aren't lost when there's no same-named module file."""
    known = {"pkg/models/__init__": "pkg/models/__init__.py"}
    assert resolve_import("pkg/sub/mod.py", "..models", known) == "pkg/models/__init__.py"


def test_py_resolve_absolute_dotted_intra_repo():
    """A bare absolute dotted import (`pkg.models.user`) splits on dots to a path — absolute Python imports still form intra-repo edges."""
    known = {"pkg/models/user": "pkg/models/user.py"}
    assert resolve_import("pkg/a/mod.py", "pkg.models.user", known) == "pkg/models/user.py"


def test_py_resolve_stdlib_and_third_party_are_none():
    """stdlib (`os`) and third-party (`django.db`) imports resolve to None — only intra-repo modules become edges."""
    known = {"pkg/mod": "pkg/mod.py"}
    assert resolve_import("pkg/mod.py", "os", known) is None
    assert resolve_import("pkg/mod.py", "django.db", known) is None


# ---- TypeScript/JS: a relative specifier is a real path; dots in filenames are literal ----
def test_ts_resolve_dot_named_sibling():
    """The dominant NestJS/Angular shape: `./auth.service` must resolve to the dot-named file, NOT be split into auth/service."""
    known = {"src/auth/auth.service": "src/auth/auth.service.ts"}
    assert resolve_import("src/auth/auth.module.ts", "./auth.service", known) == "src/auth/auth.service.ts"


def test_ts_resolve_dot_named_parent_path():
    """A dot-named TS file imported via a parent-dir relative path resolves without treating the filename dots as separators."""
    known = {"src/auth/dto/login.dto": "src/auth/dto/login.dto.ts"}
    assert (resolve_import("src/auth/guards/jwt.guard.ts", "../dto/login.dto", known)
            == "src/auth/dto/login.dto.ts")


def test_ts_resolve_barrel_index():
    """A relative TS import of a directory resolves to its index.ts barrel — the standard TS folder-as-module entry."""
    known = {"src/database/index": "src/database/index.ts"}
    assert resolve_import("src/auth/auth.service.ts", "../database", known) == "src/database/index.ts"


def test_ts_resolve_package_and_scoped_are_none():
    """Bare and scoped (`@nestjs/common`) package specifiers have no local file → None; tsconfig path aliases are tracked, not silently 'resolved'."""
    known = {"src/common/types.common": "src/common/types.common.ts"}
    assert resolve_import("src/auth/auth.service.ts", "rxjs", known) is None
    assert resolve_import("src/auth/auth.service.ts", "@nestjs/common", known) is None


def test_ts_resolution_is_invariant_across_importer_extension():
    """The same dot-named relative import resolves whether the importer is .ts/.tsx/.mts/.js/.jsx/.cjs — resolution keys on the specifier, not the importer's extension."""
    known = {"src/auth/auth.service": "src/auth/auth.service.ts"}
    for importer in (
        "src/auth/auth.module.ts", "src/auth/page.tsx", "src/auth/x.mts",
        "src/auth/y.js", "src/auth/z.jsx", "src/auth/w.cjs",
    ):
        assert resolve_import(importer, "./auth.service", known) == "src/auth/auth.service.ts", importer


def test_py_and_ts_same_dotted_segment_resolve_differently():
    """The crux distinction side by side: Python ``..auth.service`` → package auth/module service; TS ``./auth.service`` → the file auth.service. Same dots, opposite meaning."""
    py_known = {"pkg/auth/service": "pkg/auth/service.py"}
    assert resolve_import("pkg/sub/m.py", "..auth.service", py_known) == "pkg/auth/service.py"
    ts_known = {"pkg/sub/auth.service": "pkg/sub/auth.service.ts"}
    assert resolve_import("pkg/sub/m.ts", "./auth.service", ts_known) == "pkg/sub/auth.service.ts"


# ---- SQL & Prisma: no import edges (linkage is schema-level, tested in test_extract.py) ----
def test_sql_and_prisma_emit_no_import_edges():
    """Schema languages record zero imports, so cross-subsystem coupling comes from the folded schema catalog, never from resolve_import."""
    from bounds.core.extract import get_adapter

    sql = get_adapter("001_init.sql").extract("001_init.sql", b"CREATE TABLE t (id int);\n")
    prisma = get_adapter("schema.prisma").extract("schema.prisma", b"model T {\n  id Int @id\n}\n")
    assert sql.imports == []
    assert prisma.imports == []


# ===========================================================================
# NodeNext / ESM emitted-specifier resolution
# ===========================================================================
def test_resolve_nodenext_js_specifier_to_ts_source():
    """NodeNext requires TS source to import a sibling TS module by its EMITTED specifier:
    `./pricing.js` on disk is `pricing.ts`. Regression for the bug where the stem kept `.js`,
    matched nothing in the extension-less index, and dropped the edge — which in a NodeNext
    codebase drops nearly every local edge and makes cycle detection unusable."""
    known = {"src/billing/pricing": "src/billing/pricing.ts"}
    assert (resolve_import("src/ai/router.ts", "../billing/pricing.js", known)
            == "src/billing/pricing.ts")


def test_resolve_nodenext_mjs_and_cjs_specifiers():
    """`.mjs`/`.cjs` specifiers resolve to their `.mts`/`.cts` sources the same way."""
    known = {"src/a/m": "src/a/m.mts", "src/a/c": "src/a/c.cts"}
    assert resolve_import("src/a/x.mts", "./m.mjs", known) == "src/a/m.mts"
    assert resolve_import("src/a/x.cts", "./c.cjs", known) == "src/a/c.cts"


def test_resolve_prefers_real_js_file_over_desuffixed_stem():
    """When both `weird.js.ts` and `weird.js` exist, the exact stem wins — the de-suffixed
    candidate is only a fallback, so a genuine on-disk .js file is never shadowed."""
    known = {"src/weird.js": "src/weird.js.ts", "src/weird": "src/weird.js"}
    assert resolve_import("src/app.ts", "./weird.js", known) == "src/weird.js.ts"


def test_resolve_python_dot_js_is_not_desuffixed():
    """A Python importer never gets NodeNext treatment; dots stay package separators."""
    known = {"pkg/js": "pkg/js.py"}
    assert resolve_import("pkg/main.py", ".js", known) == "pkg/js.py"
