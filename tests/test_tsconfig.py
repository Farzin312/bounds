"""Tests for TypeScript tsconfig path-alias resolution (bounds.tsconfig + resolver integration).

The dotted-filename fix (test_validate.py) recovers relative TS edges; this recovers the OTHER
big undercount source on real NestJS/monorepo backends — `@/…` path aliases and baseUrl-relative
bare imports declared in tsconfig.json.
"""

from __future__ import annotations

from bounds import tsconfig
from bounds.validate.checks import resolve_import


def _write(tmp_path, name, text):
    (tmp_path / name).write_text(text, encoding="utf-8")


# ---- loader ----
def test_load_none_when_no_tsconfig(tmp_path):
    assert tsconfig.load(tmp_path) is None


def test_load_none_when_no_paths_or_baseurl(tmp_path):
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"strict": true}}')
    assert tsconfig.load(tmp_path) is None


def test_garbled_tsconfig_is_soft_none(tmp_path):
    _write(tmp_path, "tsconfig.json", "{ this is not valid json at all ")
    assert tsconfig.load(tmp_path) is None  # fail soft, never raise


def test_jsonc_comments_and_trailing_commas(tmp_path):
    _write(tmp_path, "tsconfig.json", """{
      // path aliases for the app
      "compilerOptions": {
        "baseUrl": "src",
        "paths": { "@/*": ["*"], }, /* trailing comma + block comment */
      },
    }""")
    al = tsconfig.load(tmp_path)
    assert al is not None
    assert "src/common/x" in al.candidate_stems("@/common/x")


# ---- candidate expansion ----
def test_wildcard_alias_with_baseurl(tmp_path):
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": "src", "paths": {"@/*": ["*"]}}}')
    al = tsconfig.load(tmp_path)
    assert "src/common/types" in al.candidate_stems("@/common/types")


def test_candidate_stems_ignores_relative_specifiers(tmp_path):
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": "src", "paths": {"@/*": ["*"]}}}')
    al = tsconfig.load(tmp_path)
    assert al.candidate_stems("./local") == []  # relative handled by the resolver, not aliases


# ---- end-to-end through resolve_import ----
def test_resolve_follows_wildcard_alias_to_dot_named_file(tmp_path):
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": "src", "paths": {"@/*": ["*"]}}}')
    al = tsconfig.load(tmp_path)
    known = {"src/common/types.common": "src/common/types.common.ts"}
    assert (resolve_import("src/auth/auth.service.ts", "@/common/types.common", known, None, al)
            == "src/common/types.common.ts")


def test_resolve_alias_to_package_index(tmp_path):
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": ".", "paths": {"@app/*": ["src/app/*"]}}}')
    al = tsconfig.load(tmp_path)
    known = {"src/app/users/index": "src/app/users/index.ts"}
    assert resolve_import("src/x.ts", "@app/users", known, None, al) == "src/app/users/index.ts"


def test_resolve_exact_alias_no_wildcard(tmp_path):
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": ".", "paths": {"@config": ["src/config/env.ts"]}}}')
    al = tsconfig.load(tmp_path)
    known = {"src/config/env": "src/config/env.ts"}
    assert resolve_import("src/x.ts", "@config", known, None, al) == "src/config/env.ts"


def test_resolve_baseurl_relative_bare_import(tmp_path):
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": "src"}}')
    al = tsconfig.load(tmp_path)
    known = {"src/common/types": "src/common/types.ts"}
    assert resolve_import("src/auth/x.ts", "common/types", known, None, al) == "src/common/types.ts"


def test_aliases_never_apply_to_python_importer(tmp_path):
    # A mixed repo's tsconfig must not influence Python import resolution.
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": "src", "paths": {"@/*": ["*"]}}}')
    al = tsconfig.load(tmp_path)
    known = {"src/common/types": "src/common/types.ts"}
    assert resolve_import("pkg/mod.py", "@/common/types", known, None, al) is None


def test_real_package_does_not_falsely_resolve(tmp_path):
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": "src", "paths": {"@/*": ["*"]}}}')
    al = tsconfig.load(tmp_path)
    known = {"src/common/types": "src/common/types.ts"}
    assert resolve_import("src/x.ts", "@nestjs/common", known, None, al) is None
    assert resolve_import("src/x.ts", "rxjs", known, None, al) is None


def test_extends_chain_inherits_paths(tmp_path):
    # Monorepo shape: the leaf tsconfig only `extends` a base that declares baseUrl + paths.
    _write(tmp_path, "tsconfig.base.json",
           '{"compilerOptions": {"baseUrl": "src", "paths": {"@/*": ["*"]}}}')
    _write(tmp_path, "tsconfig.json", '{"extends": "./tsconfig.base.json"}')
    al = tsconfig.load(tmp_path)
    assert al is not None
    known = {"src/common/types": "src/common/types.ts"}
    assert resolve_import("src/x.ts", "@/common/types", known, None, al) == "src/common/types.ts"


def test_no_aliases_is_prior_behavior(tmp_path):
    # With aliases=None, resolution is exactly the pre-feature behavior (no bare alias resolution).
    known = {"src/common/types": "src/common/types.ts"}
    assert resolve_import("src/x.ts", "@/common/types", known, None, None) is None
