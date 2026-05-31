"""Python language adapter: deterministic tree-sitter extraction.

Walks only the top level of a module. Functions, classes, decorated
definitions, and plain-identifier assignments become :class:`Symbol`s; a symbol
is ``exported`` iff its name does not start with an underscore. ``import`` and
``from ... import`` statements become :class:`ImportRef`s.
"""

from __future__ import annotations

import tree_sitter as ts
import tree_sitter_python as tspy

from ..models import ExtractResult, ImportRef, Symbol
from .base import LanguageAdapter, make_result

# Built lazily and cached so the Language/Parser are reused across files.
_LANG: ts.Language | None = None


def _parser() -> ts.Parser:
    """Return a parser bound to the (lazily built, cached) Python language."""
    global _LANG
    if _LANG is None:
        _LANG = ts.Language(tspy.language())
    return ts.Parser(_LANG)


def _text(node: ts.Node, source: bytes) -> str:
    """Decode the source slice spanned by ``node``."""
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _string_literal(node: ts.Node, source: bytes) -> str | None:
    """Return a static Python string literal's value, or None for dynamic expressions."""
    if node.type != "string":
        return None
    content = "".join(_text(c, source) for c in node.named_children if c.type == "string_content")
    return content if content else _text(node, source).strip("\"'")


def _line(node: ts.Node) -> int:
    """1-based start line of ``node`` (tree-sitter rows are 0-based)."""
    return node.start_point[0] + 1


def _symbol_from_definition(node: ts.Node, source: bytes) -> Symbol | None:
    """Build a Symbol from a function_definition or class_definition node."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _text(name_node, source)
    kind = "function" if node.type == "function_definition" else "class"
    if node.type == "class_definition":
        table_name = _orm_table_name(node, name, source)
        if table_name is not None:
            return Symbol(
                name=table_name,
                kind="table",
                line=_line(node),
                exported=not name.startswith("_"),
                metadata={"model": name},
            )
    return Symbol(name=name, kind=kind, line=_line(node), exported=not name.startswith("_"))


def _orm_table_name(node: ts.Node, class_name: str, source: bytes) -> str | None:
    """Static ORM table name for common Python model classes, or None when not a model."""
    header = _text(node, source).split(":", 1)[0]
    body = node.child_by_field_name("body")
    explicit: str | None = None
    django_table: str | None = None
    if body is not None:
        for stmt in body.named_children:
            if stmt.type == "expression_statement":
                lhs, rhs = _assignment_parts(stmt)
                if lhs == "__tablename__" and rhs is not None:
                    explicit = _string_literal(rhs, source) or class_name
            elif stmt.type == "class_definition":
                meta_name = stmt.child_by_field_name("name")
                if meta_name is not None and _text(meta_name, source) == "Meta":
                    meta_body = stmt.child_by_field_name("body")
                    if meta_body is not None:
                        for meta_stmt in meta_body.named_children:
                            lhs, rhs = _assignment_parts(meta_stmt)
                            if lhs == "db_table" and rhs is not None:
                                django_table = _string_literal(rhs, source) or class_name
    if explicit is not None:
        return explicit
    if "models.Model" in header:
        return django_table or class_name
    if "__tablename__" in _text(node, source):
        return explicit or class_name
    return None


def _assignment_parts(stmt: ts.Node) -> tuple[str | None, ts.Node | None]:
    for child in stmt.named_children:
        if child.type != "assignment":
            continue
        lhs = child.child_by_field_name("left")
        rhs = child.child_by_field_name("right")
        if lhs is not None and lhs.type == "identifier":
            return lhs.text.decode("utf-8", "replace"), rhs
    return None, None


def _symbol_from_assignment(stmt: ts.Node, source: bytes) -> Symbol | None:
    """Build a Symbol from an expression_statement wrapping a plain assignment.

    Only assignments whose left-hand side is a bare identifier are captured.
    UPPER_CASE names are ``const``; everything else is ``variable``.
    """
    for child in stmt.named_children:
        if child.type != "assignment":
            continue
        lhs = child.child_by_field_name("left")
        if lhs is None or lhs.type != "identifier":
            return None
        name = _text(lhs, source)
        kind = "const" if name.isupper() else "variable"
        return Symbol(name=name, kind=kind, line=_line(child), exported=not name.startswith("_"))
    return None


def _import_names(stmt: ts.Node, source: bytes) -> list[str]:
    """Imported identifier names from a from-import statement's name fields."""
    names: list[str] = []
    for name_node in stmt.children_by_field_name("name"):
        if name_node.type == "aliased_import":
            target = name_node.child_by_field_name("name")
            ident = target if target is not None else name_node.named_children[0]
        else:  # dotted_name
            ident = name_node
        names.append(_text(ident, source))
    return names


def _module_text(module_node: ts.Node, source: bytes) -> str:
    """Module path of a from-import; relative imports keep their leading dots."""
    return _text(module_node, source)


class PythonAdapter(LanguageAdapter):
    """Extracts top-level symbols and imports from Python source files."""

    language_name = "python"
    extensions = (".py",)

    def extract(self, rel_path: str, source: bytes) -> ExtractResult:
        symbols: list[Symbol] = []
        imports: list[ImportRef] = []
        try:
            tree = _parser().parse(source)
            for node in tree.root_node.named_children:
                t = node.type
                if t in ("function_definition", "class_definition"):
                    sym = _symbol_from_definition(node, source)
                    if sym is not None:
                        symbols.append(sym)
                elif t == "decorated_definition":
                    # Unwrap to the inner function/class definition.
                    inner = node.child_by_field_name("definition")
                    if inner is None:
                        for c in node.named_children:
                            if c.type in ("function_definition", "class_definition"):
                                inner = c
                                break
                    if inner is not None:
                        sym = _symbol_from_definition(inner, source)
                        if sym is not None:
                            symbols.append(sym)
                elif t == "expression_statement":
                    sym = _symbol_from_assignment(node, source)
                    if sym is not None:
                        symbols.append(sym)
                elif t == "import_statement":
                    for child in node.named_children:
                        if child.type == "aliased_import":
                            mod = child.child_by_field_name("name")
                            module = _text(mod, source) if mod is not None else _text(child, source)
                            imports.append(ImportRef(module=module, names=[], line=_line(node)))
                        elif child.type == "dotted_name":
                            imports.append(
                                ImportRef(module=_text(child, source), names=[], line=_line(node))
                            )
                elif t == "import_from_statement":
                    module_node = node.child_by_field_name("module_name")
                    module = _module_text(module_node, source) if module_node is not None else "."
                    imports.append(
                        ImportRef(
                            module=module,
                            names=_import_names(node, source),
                            line=_line(node),
                        )
                    )
        except Exception as e:  # fail soft: bad file -> result carrying the error
            return make_result(rel_path, self.language_name, [], [], source, error=str(e))

        return make_result(rel_path, self.language_name, symbols, imports, source)
