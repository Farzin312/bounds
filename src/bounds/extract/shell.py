"""Shell (bash) language adapter: deterministic tree-sitter extraction.

A shell script's public surface is its top-level **function definitions** — the functions another
script gets when it ``source``s this file. Both forms (``name() { … }`` and ``function name { … }``)
are recognised. A leading-underscore name (``_helper``) is treated as private (not exported),
mirroring the convention Bounds uses for other languages. Top-level ``source FILE`` / ``. FILE``
statements with a *literal* path become :class:`ImportRef`s so dependency edges work; a dynamic path
(``source "$DIR/lib.sh"``) is skipped rather than guessed.

Variables are intentionally NOT extracted — a shell library's consumable API is its functions, and
capturing every ``FOO=1`` would be drift noise. The bash grammar approximates ``.zsh`` closely enough
for function extraction; zsh-only syntax that fails to parse falls back to a soft error on the result
(never a raised exception), like every adapter. Only top-level definitions are the surface; functions
nested inside other functions are local and not extracted.
"""
from __future__ import annotations

import tree_sitter as ts
import tree_sitter_bash as tsbash

from ..models import ExtractResult, ImportRef, Symbol
from .base import LanguageAdapter, make_result

__all__ = []

# Built lazily and cached so the Language/Parser are reused across files.
_LANG: ts.Language | None = None

# Builtins that load another file into the current shell — their literal argument is an import edge.
_SOURCE_COMMANDS = frozenset({"source", "."})


def _parser() -> ts.Parser:
    """Return a parser bound to the (lazily built, cached) bash language."""
    global _LANG
    if _LANG is None:
        _LANG = ts.Language(tsbash.language())
    return ts.Parser(_LANG)


def _text(node: ts.Node, source: bytes) -> str:
    """Decode the source slice spanned by ``node``."""
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _line(node: ts.Node) -> int:
    """1-based start line of ``node`` (tree-sitter rows are 0-based)."""
    return node.start_point[0] + 1


def _source_import(node: ts.Node, source: bytes) -> ImportRef | None:
    """An :class:`ImportRef` for a top-level ``source FILE`` / ``. FILE`` with a LITERAL path, else None.

    Only a plain-word argument (``source ./lib.sh``) is captured; a quoted/expanded path
    (``source "$DIR/lib.sh"``) is dynamic and skipped rather than recorded as a wrong edge. The
    command name is the ``name`` field (a ``command_name`` node); the path is the first ``word`` arg.
    """
    name = node.child_by_field_name("name")
    if name is None or _text(name, source) not in _SOURCE_COMMANDS:
        return None
    for child in node.named_children:
        if child.type == "word":  # first plain-word arg = a literal path; a `string` arg is dynamic
            return ImportRef(module=_text(child, source), names=[], line=_line(node))
    return None


class ShellAdapter(LanguageAdapter):
    """Extracts top-level function definitions and ``source`` imports from shell scripts."""

    language_name = "shell"
    extensions = (".sh", ".bash", ".zsh")

    def extract(self, rel_path: str, source: bytes) -> ExtractResult:
        symbols: list[Symbol] = []
        imports: list[ImportRef] = []
        try:
            root = _parser().parse(source).root_node
            for node in root.named_children:
                if node.type == "function_definition":
                    name_node = node.child_by_field_name("name")
                    if name_node is None:
                        continue
                    name = _text(name_node, source)
                    symbols.append(
                        Symbol(
                            name=name,
                            kind="function",
                            line=_line(node),
                            exported=not name.startswith("_"),  # `_helper` is conventionally private
                        )
                    )
                elif node.type == "command":
                    imp = _source_import(node, source)
                    if imp is not None:
                        imports.append(imp)
        except Exception as e:  # fail soft: bad file -> result carrying the error, never a raise
            return make_result(rel_path, self.language_name, [], [], source, error=str(e))

        return make_result(rel_path, self.language_name, symbols, imports, source)
