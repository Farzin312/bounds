"""Shell (bash) adapter: top-level functions are the public surface, ``source``/``.`` are imports.

Locks in Stage 5 — shell moved from unsupported to a first-class adapter, so shell files are
extracted, counted in supported coverage, and their exposes verified like any other language.
"""
from __future__ import annotations

from bounds.extract import get_adapter, supported_extensions
from bounds.extract.shell import ShellAdapter


def _extract(src: str):
    return ShellAdapter().extract("scripts/lib.sh", src.encode())


def test_top_level_functions_both_forms_are_exported():
    """Both ``name() { … }`` and ``function name { … }`` are recognised as exported functions."""
    r = _extract("deploy() { echo go; }\nfunction rollback {\n  echo back\n}\n")
    funcs = {(s.name, s.kind, s.exported) for s in r.symbols}
    assert ("deploy", "function", True) in funcs
    assert ("rollback", "function", True) in funcs
    assert r.error is None


def test_leading_underscore_function_is_private():
    """A ``_helper`` is conventionally private — not part of the exported surface."""
    by_name = {s.name: s.exported for s in _extract("_helper() { :; }\npublic() { :; }\n").symbols}
    assert by_name["_helper"] is False
    assert by_name["public"] is True


def test_source_with_literal_path_is_an_import_dynamic_is_skipped():
    """``source FILE`` / ``. FILE`` with a literal path becomes an import; a dynamic path is skipped."""
    r = _extract('source ./lib/util.sh\n. helpers.sh\nsource "$DIR/dyn.sh"\n')
    modules = {i.module for i in r.imports}
    assert "./lib/util.sh" in modules
    assert "helpers.sh" in modules
    assert not any("DIR" in m for m in modules)  # dynamic/quoted path skipped, never guessed


def test_variables_are_not_extracted_as_surface():
    """A shell library's API is its functions; top-level vars (incl. readonly) are not the surface."""
    r = _extract("FOO=1\nreadonly BAR=2\ndeploy() { :; }\n")
    assert {s.name for s in r.symbols} == {"deploy"}


def test_nested_function_is_not_top_level_surface():
    """Only top-level functions are the surface; one nested inside another is local."""
    r = _extract("outer() {\n  inner() { :; }\n  inner\n}\n")
    assert {s.name for s in r.symbols} == {"outer"}


def test_structure_hash_is_body_insensitive():
    """Same exported surface (the ``deploy`` fn) → same structure hash, regardless of body edits."""
    a = _extract("deploy() { echo one; }\n")
    b = _extract("deploy() {\n  echo a completely different body\n  ls -la\n}\n")
    assert a.structure_hash == b.structure_hash
    assert a.content_hash != b.content_hash  # content differs (cache invalidates), structure doesn't


def test_garbage_input_is_soft_a_result_never_a_raise():
    """Fail soft: unparseable bytes return an ExtractResult (never raise) — the adapter contract."""
    r = ShellAdapter().extract("x.sh", b"\xff\xfe {{{ not valid")
    assert r.path == "x.sh"  # a result came back; no exception escaped


def test_registry_registers_shell_for_sh_bash_zsh():
    """The adapter registry treats .sh, .bash, and .zsh as supported shell source files."""
    assert isinstance(get_adapter("x.sh"), ShellAdapter)
    assert isinstance(get_adapter("x.bash"), ShellAdapter)
    assert isinstance(get_adapter("x.zsh"), ShellAdapter)
    assert {".sh", ".bash", ".zsh"} <= supported_extensions()
