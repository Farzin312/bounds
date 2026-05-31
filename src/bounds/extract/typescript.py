"""TypeScript / JavaScript adapter: deterministic tree-sitter extraction.

Covers ``.ts``/``.tsx``/``.mts``/``.cts`` and the JS family (``.js``/``.jsx``/
``.mjs``/``.cjs``). ``.tsx`` and ``.jsx`` use the TSX grammar; everything else
uses the TypeScript grammar (a superset that also parses plain JS).

Top-level ``export_statement`` nodes yield exported symbols (functions, classes,
const/let bindings, interfaces, type aliases, enums, default exports and
re-export clauses). Non-exported top-level declarations are still captured (with
``exported=False``) so boundary checks can see that internals exist.
``import_statement`` and ``export ... from`` clauses yield import references.

CommonJS is covered too: a ``require("mod")`` call (including inside a
``const x = require(...)`` or ``const {a} = require(...)`` binding) yields an
import reference, and a top-level ``module.exports = ...`` / ``module.exports.foo
= ...`` / ``exports.foo = ...`` assignment yields an exported symbol. Barrel
re-exports (``export * from "./m"`` and ``export * as ns from "./m"``) yield an
import reference so the dependency stays visible to validation/propagation; the
cross-file union of ``export *`` is not expanded into individual symbols here.
"""

from __future__ import annotations

import tree_sitter as ts
import tree_sitter_typescript as tsts

from ..models import ExtractResult, ImportRef, Symbol
from .base import LanguageAdapter, make_result

# Grammars are built lazily and cached so Language objects are reused.
_TS_LANG: ts.Language | None = None
_TSX_LANG: ts.Language | None = None

_TSX_EXTS = (".tsx", ".jsx")

# Declaration node type -> the Symbol kind it produces.
_DECL_KIND = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "const",
}


def _parser(rel_path: str) -> ts.Parser:
    """Return a parser using the TSX grammar for .tsx/.jsx, else TypeScript."""
    global _TS_LANG, _TSX_LANG
    use_tsx = any(rel_path.endswith(ext) for ext in _TSX_EXTS)
    if use_tsx:
        if _TSX_LANG is None:
            _TSX_LANG = ts.Language(tsts.language_tsx())
        return ts.Parser(_TSX_LANG)
    if _TS_LANG is None:
        _TS_LANG = ts.Language(tsts.language_typescript())
    return ts.Parser(_TS_LANG)


def _text(node: ts.Node, source: bytes) -> str:
    """Decode the source slice spanned by ``node``."""
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _line(node: ts.Node) -> int:
    """1-based start line of ``node`` (tree-sitter rows are 0-based)."""
    return node.start_point[0] + 1


def _string_value(node: ts.Node, source: bytes) -> str:
    """Return a string literal's value with surrounding quotes stripped."""
    raw = _text(node, source)
    if len(raw) >= 2 and raw[0] in "\"'`" and raw[-1] == raw[0]:
        return raw[1:-1]
    return raw


def _decl_symbols(decl: ts.Node, source: bytes, line: int, exported: bool) -> list[Symbol]:
    """Build Symbol(s) from a declaration node (one per name)."""
    t = decl.type
    if t in _DECL_KIND:
        name_node = decl.child_by_field_name("name")
        name = _text(name_node, source) if name_node is not None else "default"
        return [Symbol(name=name, kind=_DECL_KIND[t], line=line, exported=exported)]
    if t in ("lexical_declaration", "variable_declaration"):
        out: list[Symbol] = []
        for declarator in decl.named_children:
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            if name_node is None:
                continue
            out.append(
                Symbol(name=_text(name_node, source), kind="const", line=line, exported=exported)
            )
        return out
    return []


def _export_clause_symbols(clause: ts.Node, source: bytes, line: int) -> list[Symbol]:
    """Symbols for ``export { a as b, c }`` -- one per exported alias name."""
    out: list[Symbol] = []
    for spec in clause.named_children:
        if spec.type != "export_specifier":
            continue
        # First identifier is the local name; the optional alias is the second.
        idents = [c for c in spec.named_children if c.type in ("identifier", "type_identifier")]
        if not idents:
            continue
        exported_name = _text(idents[-1], source)
        out.append(Symbol(name=exported_name, kind="unknown", line=line, exported=True))
    return out


def _import_names(clause: ts.Node, source: bytes) -> list[str]:
    """Imported names from an import_clause.

    Default import -> its local identifier; named imports -> the imported
    identifier of each specifier; namespace import (`* as ns`) -> nothing.
    """
    names: list[str] = []
    for child in clause.named_children:
        if child.type == "identifier":  # default import
            names.append(_text(child, source))
        elif child.type == "named_imports":
            for spec in child.named_children:
                if spec.type != "import_specifier":
                    continue
                idents = [c for c in spec.named_children if c.type in ("identifier", "type_identifier")]
                if idents:
                    names.append(_text(idents[0], source))
        # namespace_import contributes no named imports
    return names


def _require_module(call: ts.Node, source: bytes) -> str | None:
    """If ``call`` is a ``require("mod")`` call, return ``"mod"``, else ``None``.

    Recognizes a ``call_expression`` whose callee is the identifier ``require`` and
    whose first argument is a string literal. A dynamic ``require(expr)`` with a
    non-literal argument yields ``None`` (no statically knowable module).
    """
    if call.type != "call_expression":
        return None
    fn = call.child_by_field_name("function")
    if fn is None or fn.type != "identifier" or _text(fn, source) != "require":
        return None
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    first = next(iter(args.named_children), None)
    if first is None or first.type != "string":
        return None
    return _string_value(first, source)


def _collect_requires(node: ts.Node, source: bytes, imports: list[ImportRef]) -> None:
    """Append an ImportRef for every ``require("mod")`` call found under ``node``.

    Walks the subtree so a require inside a binding (``const x = require("m")``,
    ``const {a} = require("m")``) or a bare side-effect ``require("m")`` is captured.
    Module specifiers are deduplicated per statement (a single statement requiring the
    same module twice records it once) while preserving source order.
    """
    seen: set[str] = set()
    stack: list[ts.Node] = [node]
    while stack:
        cur = stack.pop()
        mod = _require_module(cur, source)
        if mod is not None and mod not in seen:
            seen.add(mod)
            imports.append(ImportRef(module=mod, names=[], line=_line(cur)))
        stack.extend(reversed(cur.named_children))


def _commonjs_export_target(left: ts.Node, source: bytes, line: int) -> Symbol | None:
    """The exported Symbol named by a single CommonJS assignment target, or None.

    A target is the ``left`` of one assignment: ``module.exports`` (a ``default``
    export), or ``module.exports.<name>`` / ``exports.<name>`` (named export ``name``).
    """
    if left.type != "member_expression":
        return None
    obj = left.child_by_field_name("object")
    prop = left.child_by_field_name("property")
    if obj is None or prop is None:
        return None
    obj_text = _text(obj, source)
    prop_text = _text(prop, source)
    # `module.exports = ...` -> a single default-style export.
    if obj_text == "module" and prop_text == "exports":
        return Symbol(name="default", kind="default", line=line, exported=True)
    # `module.exports.foo = ...` -> named export `foo`.
    if obj.type == "member_expression":
        inner_obj = obj.child_by_field_name("object")
        inner_prop = obj.child_by_field_name("property")
        if (
            inner_obj is not None
            and inner_prop is not None
            and _text(inner_obj, source) == "module"
            and _text(inner_prop, source) == "exports"
        ):
            return Symbol(name=prop_text, kind="unknown", line=line, exported=True)
    # `exports.foo = ...` -> named export `foo`.
    if obj_text == "exports":
        return Symbol(name=prop_text, kind="unknown", line=line, exported=True)
    return None


def _commonjs_export_symbols(node: ts.Node, source: bytes) -> list[Symbol]:
    """Exported Symbol(s) from a top-level CommonJS assignment expression.

    Handles ``module.exports = <expr>`` (a single ``default`` export), and the named
    forms ``module.exports.<name> = <expr>`` / ``exports.<name> = <expr>`` (one symbol
    per assigned property name). Chained assignments
    (``exports.foo = exports.bar = <expr>``) yield one symbol per target in source
    order. A non-export assignment yields nothing.
    """
    if node.type != "expression_statement":
        return []
    assign = next((c for c in node.named_children if c.type == "assignment_expression"), None)
    if assign is None:
        return []
    line = _line(node)
    symbols: list[Symbol] = []
    # Walk the assignment chain left-to-right, collecting each export target.
    while assign is not None and assign.type == "assignment_expression":
        left = assign.child_by_field_name("left")
        if left is not None:
            sym = _commonjs_export_target(left, source, line)
            if sym is not None:
                symbols.append(sym)
        assign = assign.child_by_field_name("right")
    return symbols


class TypeScriptAdapter(LanguageAdapter):
    """Extracts top-level exports and imports from TS/JS source files."""

    language_name = "typescript"
    extensions = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")

    def extract(self, rel_path: str, source: bytes) -> ExtractResult:
        symbols: list[Symbol] = []
        imports: list[ImportRef] = []
        try:
            tree = _parser(rel_path).parse(source)
            for node in tree.root_node.named_children:
                t = node.type
                if t == "export_statement":
                    symbols.extend(self._export_symbols(node, source, imports))
                elif t in _DECL_KIND or t in ("lexical_declaration", "variable_declaration"):
                    # Non-exported top-level declaration: capture as internal.
                    symbols.extend(_decl_symbols(node, source, _line(node), exported=False))
                    # A `const x = require("mod")` binding still carries an import edge.
                    _collect_requires(node, source, imports)
                elif t == "import_statement":
                    self._collect_import(node, source, imports)
                elif t == "expression_statement":
                    # CommonJS: `module.exports = ...` / `exports.foo = ...` exports,
                    # and bare `require("mod")` calls (e.g. side-effect requires).
                    symbols.extend(_commonjs_export_symbols(node, source))
                    _collect_requires(node, source, imports)
        except Exception as e:  # fail soft: bad file -> result carrying the error
            return make_result(rel_path, self.language_name, [], [], source, error=str(e))

        return make_result(rel_path, self.language_name, symbols, imports, source)

    def _export_symbols(
        self, node: ts.Node, source: bytes, imports: list[ImportRef]
    ) -> list[Symbol]:
        """Symbols (and any re-export ImportRefs) from one export_statement."""
        line = _line(node)
        is_default = any(c.type == "default" for c in node.children)

        # Source string present only for `export ... from "mod"`.
        source_string = None
        for c in node.named_children:
            if c.type == "string":
                source_string = c
                break

        # Barrel re-export: `export * from "mod"` / `export * as ns from "mod"`.
        # Record the dependency as an import edge so validation/propagation can see
        # it; the cross-file union of `export *` is not expanded into symbols here.
        # `export * as ns` additionally binds a namespace symbol, which we emit.
        is_star = any(c.type in ("namespace_export", "*") for c in node.children)
        if is_star and source_string is not None:
            imports.append(
                ImportRef(module=_string_value(source_string, source), names=[], line=line)
            )

        out: list[Symbol] = []
        for child in node.named_children:
            ct = child.type
            if ct == "namespace_export":
                # `export * as ns from "mod"`: the namespace alias is an exported symbol.
                name_node = next(
                    (c for c in child.named_children if c.type == "identifier"), None
                )
                if name_node is not None:
                    out.append(
                        Symbol(name=_text(name_node, source), kind="const", line=line, exported=True)
                    )
            elif ct == "export_clause":
                clause_syms = _export_clause_symbols(child, source, line)
                out.extend(clause_syms)
                if source_string is not None:
                    # `export { ... } from "mod"`: also record the import edge.
                    imports.append(
                        ImportRef(
                            module=_string_value(source_string, source),
                            names=[s.name for s in clause_syms],
                            line=line,
                        )
                    )
            elif ct in _DECL_KIND or ct in ("lexical_declaration", "variable_declaration"):
                out.extend(_decl_symbols(child, source, line, exported=True))
            elif ct == "string":
                continue  # handled as source_string above
            elif is_default:
                # `export default <expr>` where expr is not a named declaration.
                name_node = child.child_by_field_name("name")
                name = _text(name_node, source) if name_node is not None else "default"
                out.append(Symbol(name=name, kind="default", line=line, exported=True))

        if is_default and not out:
            # `export default 42;` / anonymous function/class expression.
            out.append(Symbol(name="default", kind="default", line=line, exported=True))
        return out

    def _collect_import(self, node: ts.Node, source: bytes, imports: list[ImportRef]) -> None:
        """Append an ImportRef for a plain `import ... from "mod"` statement."""
        module = None
        names: list[str] = []
        for child in node.named_children:
            if child.type == "string":
                module = _string_value(child, source)
            elif child.type == "import_clause":
                names = _import_names(child, source)
        if module is not None:
            imports.append(ImportRef(module=module, names=names, line=_line(node)))
