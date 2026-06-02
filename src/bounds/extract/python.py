"""Python language adapter: deterministic tree-sitter extraction.

Walks only the top level of a module. Functions, classes, decorated
definitions, and plain-identifier assignments become :class:`Symbol`s; ``import``
and ``from ... import`` statements become :class:`ImportRef`s.

Export rule: when a module declares a *literal* ``__all__`` (a list/tuple of
string names at module level), a symbol is ``exported`` iff its name is a member
of ``__all__`` — the explicit public surface the author chose. When ``__all__``
is absent (or built dynamically, so not statically knowable), a symbol is
``exported`` iff its name does not start with an underscore. The rule is per-file
and cacheable: it reads only the module's own tree, never another file.
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
    """Return a *static* Python string literal's value, or None for dynamic expressions.

    An f-string with interpolation (``f"prefix_{x}"``) is dynamic — we return None so the
    caller falls back to the class name rather than fabricating a wrong literal table name.
    """
    if node.type != "string":
        return None
    if any(c.type == "interpolation" for c in node.named_children):
        return None  # f-string with a {placeholder}: not statically knowable
    content = "".join(_text(c, source) for c in node.named_children if c.type == "string_content")
    return content or None


def _base_names(node: ts.Node, source: bytes) -> set[str]:
    """Base-class names of a class_definition, read structurally (not by substring)."""
    sc = node.child_by_field_name("superclasses")
    if sc is None:
        return set()
    return {_text(c, source) for c in sc.named_children if c.type in ("identifier", "attribute")}


def _dunder_all_from_tree(root: ts.Node, source: bytes) -> frozenset[str] | None:
    """The set of names in a module-level *literal* ``__all__``, or None when absent/dynamic.

    Recognises the common ``__all__ = ["a", "b"]`` / ``(...)`` literal forms (assignment to a
    bare ``__all__`` identifier whose RHS is a list/tuple of string literals). A non-literal RHS
    (``__all__ = something``, ``__all__ += [...]``, comprehension, concatenation) is NOT statically
    knowable, so we return None and the caller falls back to the leading-underscore rule rather than
    guessing. Determinism: the returned set is order-free (membership only); callers never iterate it
    into serialized output.
    """
    for stmt in root.named_children:
        if stmt.type != "expression_statement":
            continue
        for child in stmt.named_children:
            if child.type != "assignment":
                continue
            lhs = child.child_by_field_name("left")
            if lhs is None or lhs.type != "identifier" or _text(lhs, source) != "__all__":
                continue
            rhs = child.child_by_field_name("right")
            if rhs is None or rhs.type not in ("list", "tuple"):
                return None  # __all__ exists but is built dynamically -> fall back, don't guess
            names: set[str] = set()
            for elem in rhs.named_children:
                value = _string_literal(elem, source)
                if value is None:
                    return None  # a non-string-literal element -> not statically knowable
                names.add(value)
            return frozenset(names)
    return None


def _call_first_string(node: ts.Node, source: bytes) -> str | None:
    """First string argument of a ``Name(...)`` call (e.g. ``Table("users", meta)``), or None."""
    if node.type != "call":
        return None
    args = node.child_by_field_name("arguments")
    if args is None:
        return None
    first = next(iter(args.named_children), None)
    return _string_literal(first, source) if first is not None else None


def _line(node: ts.Node) -> int:
    """1-based start line of ``node`` (tree-sitter rows are 0-based)."""
    return node.start_point[0] + 1


def _is_exported(name: str, allowed: frozenset[str] | None) -> bool:
    """Whether a top-level name is part of the public surface.

    When the module declares a literal ``__all__`` (``allowed`` is a set), only its members are
    exported. Otherwise (``allowed is None``) the leading-underscore convention applies.
    """
    if allowed is not None:
        return name in allowed
    return not name.startswith("_")


def _symbol_from_definition(
    node: ts.Node, source: bytes, allowed: frozenset[str] | None, django_bases: frozenset[str]
) -> Symbol | None:
    """Build a Symbol from a function_definition or class_definition node.

    ``allowed`` gates ``exported`` (literal ``__all__`` membership, or None for the underscore rule).
    ``django_bases`` is the set of same-file class names already known to be Django models, used to
    resolve ORM-table inheritance transitively without crossing file boundaries.
    """
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = _text(name_node, source)
    kind = "function" if node.type == "function_definition" else "class"
    if node.type == "class_definition":
        table_name = _orm_table_name(node, name, source, django_bases)
        if table_name is not None:
            return Symbol(
                name=table_name,
                kind="table",
                line=_line(node),
                exported=_is_exported(name, allowed),
                metadata={"model": name},
            )
    return Symbol(name=name, kind=kind, line=_line(node), exported=_is_exported(name, allowed))


def _orm_table_name(
    node: ts.Node, class_name: str, source: bytes, django_bases: frozenset[str] = frozenset()
) -> str | None:
    """Static ORM table name for common Python model classes, or None when not a model.

    Detection is structural, never substring-based:
      * SQLAlchemy — a real ``__tablename__ = "..."`` (or ``__table__ = Table("...", ...)``)
        assignment in the class body. Dynamic names fall back to the class name.
      * Django — a base class named ``Model`` / ``*.Model``, OR a base defined *in the same file*
        that is itself a Django model (resolved transitively via ``django_bases``); table =
        ``Meta.db_table`` or the class name. An abstract model (``Meta.abstract = True``) is a
        base/mixin, not a table. Same-file only: inheritance is never resolved across files, so the
        per-file-cacheable invariant holds.
    """
    bases = _base_names(node, source)
    is_django = any(
        b == "Model" or b.endswith(".Model") or b in django_bases for b in bases
    )
    body = node.child_by_field_name("body")
    table_attr: str | None = None      # SQLAlchemy __tablename__/__table__ literal
    has_table_attr = False
    django_table: str | None = None
    abstract = False
    if body is not None:
        for stmt in body.named_children:
            if stmt.type == "expression_statement":
                lhs, rhs = _assignment_parts(stmt)
                if lhs == "__tablename__" and rhs is not None:
                    has_table_attr = True
                    table_attr = _string_literal(rhs, source)
                elif lhs == "__table__" and rhs is not None:
                    name = _call_first_string(rhs, source)
                    if name:
                        has_table_attr = True
                        table_attr = name
            elif stmt.type == "class_definition":
                meta_name = stmt.child_by_field_name("name")
                if meta_name is not None and _text(meta_name, source) == "Meta":
                    meta_body = stmt.child_by_field_name("body")
                    for meta_stmt in (meta_body.named_children if meta_body is not None else []):
                        lhs, rhs = _assignment_parts(meta_stmt)
                        if lhs == "db_table" and rhs is not None:
                            django_table = _string_literal(rhs, source)
                        elif lhs == "abstract" and rhs is not None and _text(rhs, source) == "True":
                            abstract = True
    if is_django and abstract:
        return None  # abstract base / mixin maps to no table
    if has_table_attr:
        return table_attr or class_name  # SQLAlchemy: literal name, else the class name
    if is_django:
        return django_table or class_name
    return None


def _inner_class_def(node: ts.Node) -> ts.Node | None:
    """The class_definition for a top-level node, unwrapping a ``decorated_definition``, else None.

    Used to enumerate every top-level class (including ``@decorator``-wrapped ones) when building the
    same-file Django-inheritance closure, so a decorated model base still participates.
    """
    if node.type == "class_definition":
        return node
    if node.type == "decorated_definition":
        inner = node.child_by_field_name("definition")
        if inner is not None and inner.type == "class_definition":
            return inner
        for c in node.named_children:
            if c.type == "class_definition":
                return c
    return None


def _django_model_bases(class_defs: list[tuple[str, ts.Node]], source: bytes) -> frozenset[str]:
    """Same-file class names that are Django models, resolved transitively through local bases.

    A class is a Django model if it inherits (directly or via a chain of same-file base classes)
    from ``Model`` / ``*.Model``. ``class_defs`` is the list of ``(name, class_node)`` at module
    top level. Same-file only — a base not defined here is never resolved (per-file cacheable).
    Cycle-safe: a fixpoint pass over a bounded local graph (a class can be marked at most once).

    The returned set names the classes usable as *Django model bases* for resolving a subclass —
    it does NOT decide table-ness on its own (an abstract base is in this set yet maps to no table).
    """
    bases_of: dict[str, set[str]] = {}
    is_model: dict[str, bool] = {}
    for name, node in class_defs:
        bases = _base_names(node, source)
        bases_of[name] = bases
        is_model[name] = any(b == "Model" or b.endswith(".Model") for b in bases)
    # Fixpoint: a class becomes a model once any of its same-file bases is known to be one. Iterate
    # until stable; each pass can only flip False->True, so it converges in <= len(class_defs) passes
    # even with inheritance cycles (a cycle simply never flips and exits cleanly).
    changed = True
    while changed:
        changed = False
        for name, bases in bases_of.items():
            if is_model[name]:
                continue
            if any(is_model.get(b, False) for b in bases):
                is_model[name] = True
                changed = True
    return frozenset(name for name, flag in is_model.items() if flag)


def _assignment_parts(stmt: ts.Node) -> tuple[str | None, ts.Node | None]:
    for child in stmt.named_children:
        if child.type != "assignment":
            continue
        lhs = child.child_by_field_name("left")
        rhs = child.child_by_field_name("right")
        if lhs is not None and lhs.type == "identifier":
            return lhs.text.decode("utf-8", "replace"), rhs
    return None, None


def _symbol_from_assignment(
    stmt: ts.Node, source: bytes, allowed: frozenset[str] | None
) -> Symbol | None:
    """Build a Symbol from an expression_statement wrapping a plain assignment.

    Only assignments whose left-hand side is a bare identifier are captured.
    UPPER_CASE names are ``const``; everything else is ``variable``. ``allowed`` gates ``exported``
    (literal ``__all__`` membership, or None for the leading-underscore rule).
    """
    for child in stmt.named_children:
        if child.type != "assignment":
            continue
        lhs = child.child_by_field_name("left")
        if lhs is None or lhs.type != "identifier":
            return None
        name = _text(lhs, source)
        kind = "const" if name.isupper() else "variable"
        return Symbol(
            name=name, kind=kind, line=_line(child), exported=_is_exported(name, allowed)
        )
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
            root = tree.root_node
            # Module-level literal __all__ gates the public surface (None => underscore rule).
            allowed = _dunder_all_from_tree(root, source)
            # Same-file Django-model inheritance closure, computed once before symbol emission so a
            # subclass of an in-file model base is recognised as a table (same-file only).
            class_defs: list[tuple[str, ts.Node]] = []
            for node in root.named_children:
                cls = _inner_class_def(node)
                if cls is not None:
                    nm = cls.child_by_field_name("name")
                    if nm is not None:
                        class_defs.append((_text(nm, source), cls))
            django_bases = _django_model_bases(class_defs, source)
            for node in root.named_children:
                t = node.type
                if t in ("function_definition", "class_definition"):
                    sym = _symbol_from_definition(node, source, allowed, django_bases)
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
                        sym = _symbol_from_definition(inner, source, allowed, django_bases)
                        if sym is not None:
                            symbols.append(sym)
                elif t == "expression_statement":
                    sym = _symbol_from_assignment(node, source, allowed)
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
