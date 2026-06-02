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
    """With no tsconfig.json present, load returns None so the resolver falls back to plain relative resolution — never raises on a repo that has no TS config."""
    assert tsconfig.load(tmp_path) is None


def test_load_none_when_no_paths_or_baseurl(tmp_path):
    """A tsconfig with neither paths nor baseUrl yields None — there's no alias map to apply, so the resolver shouldn't carry an empty one that could mask plain resolution."""
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"strict": true}}')
    assert tsconfig.load(tmp_path) is None


def test_garbled_tsconfig_is_soft_none(tmp_path):
    """A malformed tsconfig.json returns None rather than raising — a broken config must degrade to no-alias resolution, never crash the whole run (fail-soft)."""
    _write(tmp_path, "tsconfig.json", "{ this is not valid json at all ")
    assert tsconfig.load(tmp_path) is None  # fail soft, never raise


def test_jsonc_comments_and_trailing_commas(tmp_path):
    """tsconfig is JSONC: the loader must tolerate line/block comments and trailing commas — real tsconfigs use them, and a strict JSON parse would drop every alias on those repos."""
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
    """A `@/*` -> `*` alias under baseUrl=src must expand `@/common/types` to the baseUrl-relative stem src/common/types — the core path-alias rewrite that recovers `@/…` import edges."""
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": "src", "paths": {"@/*": ["*"]}}}')
    al = tsconfig.load(tmp_path)
    assert "src/common/types" in al.candidate_stems("@/common/types")


def test_candidate_stems_ignores_relative_specifiers(tmp_path):
    """A relative specifier (./local) yields no alias candidates — relatives are the resolver's job, and rewriting them through aliases would produce bogus stems."""
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": "src", "paths": {"@/*": ["*"]}}}')
    al = tsconfig.load(tmp_path)
    assert al.candidate_stems("./local") == []  # relative handled by the resolver, not aliases


def test_overlapping_aliases_prefer_longest_prefix(tmp_path):
    """TS resolves to the pattern with the longest literal prefix before `*`, not the
    lexicographically-first — so a specific `@/foo/*` must win over a broad `@/*`."""
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": ".", "paths": '
           '{"@/*": ["src/*"], "@/foo/*": ["packages/foo/src/*"]}}}')
    al = tsconfig.load(tmp_path)
    stems = al.candidate_stems("@/foo/bar")
    # The specific alias's target must be emitted before the broad alias's target.
    assert stems.index("packages/foo/src/bar") < stems.index("src/foo/bar")


def test_exact_alias_wins_over_wildcard(tmp_path):
    """An exact, wildcard-free pattern is the most specific and must outrank an overlapping `@/*`."""
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": ".", "paths": '
           '{"@/*": ["src/*"], "@/foo": ["vendor/foo/index"]}}}')
    al = tsconfig.load(tmp_path)
    stems = al.candidate_stems("@/foo")
    assert stems.index("vendor/foo/index") < stems.index("src/foo")


def test_resolve_prefers_specific_alias_target(tmp_path):
    """End-to-end: when both the broad and specific alias targets exist as files, the import
    resolves to the specific one (the bug: broad `@/*` shadowed `@/foo/*`)."""
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": ".", "paths": '
           '{"@/*": ["src/*"], "@/foo/*": ["packages/foo/src/*"]}}}')
    al = tsconfig.load(tmp_path)
    known = {
        "src/foo/bar": "src/foo/bar.ts",
        "packages/foo/src/bar": "packages/foo/src/bar.ts",
    }
    assert (resolve_import("src/app.ts", "@/foo/bar", known, None, al)
            == "packages/foo/src/bar.ts")


# ---- end-to-end through resolve_import ----
def test_resolve_follows_wildcard_alias_to_dot_named_file(tmp_path):
    """A wildcard alias must resolve to a dot-named file (types.common.ts) — combines alias rewrite with the dotted-filename fix so `@/common/types.common` lands on the real module."""
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": "src", "paths": {"@/*": ["*"]}}}')
    al = tsconfig.load(tmp_path)
    known = {"src/common/types.common": "src/common/types.common.ts"}
    assert (resolve_import("src/auth/auth.service.ts", "@/common/types.common", known, None, al)
            == "src/common/types.common.ts")


def test_resolve_alias_to_package_index(tmp_path):
    """An aliased directory import (`@app/users`) must resolve to its index.ts — directory-as-package resolution still applies after the alias rewrite."""
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": ".", "paths": {"@app/*": ["src/app/*"]}}}')
    al = tsconfig.load(tmp_path)
    known = {"src/app/users/index": "src/app/users/index.ts"}
    assert resolve_import("src/x.ts", "@app/users", known, None, al) == "src/app/users/index.ts"


def test_resolve_exact_alias_no_wildcard(tmp_path):
    """An exact wildcard-free alias (`@config` -> src/config/env.ts) must resolve its single literal target — the non-wildcard path-mapping form, distinct from `@/*` patterns."""
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": ".", "paths": {"@config": ["src/config/env.ts"]}}}')
    al = tsconfig.load(tmp_path)
    known = {"src/config/env": "src/config/env.ts"}
    assert resolve_import("src/x.ts", "@config", known, None, al) == "src/config/env.ts"


def test_resolve_baseurl_relative_bare_import(tmp_path):
    """With baseUrl=src and no paths, a bare import (`common/types`) must resolve baseUrl-relative to src/common/types.ts — baseUrl alone is a resolution root, not just an alias prefix."""
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": "src"}}')
    al = tsconfig.load(tmp_path)
    known = {"src/common/types": "src/common/types.ts"}
    assert resolve_import("src/auth/x.ts", "common/types", known, None, al) == "src/common/types.ts"


def test_aliases_never_apply_to_python_importer(tmp_path):
    """TS path aliases must never apply to a Python importer (.py) — a mixed repo's tsconfig must not rewrite Python imports, or it would fabricate cross-language edges."""
    # A mixed repo's tsconfig must not influence Python import resolution.
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": "src", "paths": {"@/*": ["*"]}}}')
    al = tsconfig.load(tmp_path)
    known = {"src/common/types": "src/common/types.ts"}
    assert resolve_import("pkg/mod.py", "@/common/types", known, None, al) is None


def test_real_package_does_not_falsely_resolve(tmp_path):
    """Real npm packages (@nestjs/common, rxjs) must NOT resolve to a local file just because an alias exists — node_modules deps stay external (no false-positive internal edge)."""
    _write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": "src", "paths": {"@/*": ["*"]}}}')
    al = tsconfig.load(tmp_path)
    known = {"src/common/types": "src/common/types.ts"}
    assert resolve_import("src/x.ts", "@nestjs/common", known, None, al) is None
    assert resolve_import("src/x.ts", "rxjs", known, None, al) is None


def test_extends_chain_inherits_paths(tmp_path):
    """A leaf tsconfig that only `extends` a base must inherit the base's baseUrl + paths — the common monorepo shape; without extends resolution every aliased edge in those repos is lost."""
    # Monorepo shape: the leaf tsconfig only `extends` a base that declares baseUrl + paths.
    _write(tmp_path, "tsconfig.base.json",
           '{"compilerOptions": {"baseUrl": "src", "paths": {"@/*": ["*"]}}}')
    _write(tmp_path, "tsconfig.json", '{"extends": "./tsconfig.base.json"}')
    al = tsconfig.load(tmp_path)
    assert al is not None
    known = {"src/common/types": "src/common/types.ts"}
    assert resolve_import("src/x.ts", "@/common/types", known, None, al) == "src/common/types.ts"


def test_no_aliases_is_prior_behavior(tmp_path):
    """With aliases=None, resolve_import must behave exactly as before the feature — a bare `@/…` import stays unresolved, guaranteeing the alias feature is purely additive."""
    # With aliases=None, resolution is exactly the pre-feature behavior (no bare alias resolution).
    known = {"src/common/types": "src/common/types.ts"}
    assert resolve_import("src/x.ts", "@/common/types", known, None, None) is None
