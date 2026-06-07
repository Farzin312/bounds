"""Every TypeScript export form is detected exported with the right kind and defining file.

Guards BOUNDS-008: type/re-export under-detection plus enum/namespace/re-export kind mislabels
that desynced ``discover`` (which writes ``exposes``) from the ``validate`` extractor (which
must re-detect every name as exported), causing false ``E_STRUCTURAL_DRIFT`` on TS type-heavy
modules. The contract under test is the extractor's per-symbol ``(name, kind, exported, file)``.
"""

from bounds.core.extract.registry import get_adapter

# One module exercising every export form BOUNDS-008 covers.
TS_ALL_FORMS = b"""\
export type Alias = number;
export interface IFace { a: number; }
export enum Color { Red, Green }
export const enum Dir { Up, Down }
export class Widget {}
export abstract class Base {}
export function overloaded(a: number): number;
export function overloaded(a: string): string;
export function overloaded(a: unknown): unknown { return a; }
export const C1 = 1, C2 = 2;
export let mutableL = 3;
export var legacyV = 4;
export default function mainFn() {}
const localConst = 1;
class LocalClass {}
export { localConst, LocalClass };
export { localConst as renamedConst };
export { Imported, Other } from "./dep";
export { Renamed as Public } from "./dep2";
export type { OnlyType } from "./types";
export * as ns from "./ns-mod";
export * from "./barrel";
"""


def _extract(rel: str = "mod.ts", src: bytes = TS_ALL_FORMS):
    """Run the TS adapter and return its ExtractResult."""
    return get_adapter(rel).extract(rel, src)


def test_declaration_kinds_are_correct():
    """Each declared export carries the kind matching its declaration node."""
    res = _extract()
    kinds = {s.name: s.kind for s in res.symbols if s.exported}
    assert kinds["Alias"] == "type"
    assert kinds["IFace"] == "interface"
    assert kinds["Color"] == "enum"          # export enum
    assert kinds["Dir"] == "enum"            # export const enum (same node type)
    assert kinds["Widget"] == "class"
    assert kinds["Base"] == "class"          # abstract class
    assert kinds["overloaded"] == "function"
    assert kinds["C1"] == "const" and kinds["C2"] == "const"
    assert kinds["mutableL"] == "const" and kinds["legacyV"] == "const"


def test_overloaded_function_is_one_symbol():
    """An overload set (signatures + impl) collapses to ONE exported `function`, not duplicates."""
    res = _extract()
    overloaded = [s for s in res.symbols if s.name == "overloaded"]
    assert len(overloaded) == 1
    assert overloaded[0].kind == "function"
    assert overloaded[0].exported is True


def test_default_export_detected():
    """`export default function mainFn() {}` is exported (named declaration wins over `default`)."""
    res = _extract()
    exported = {s.name for s in res.symbols if s.exported}
    assert "mainFn" in exported


def test_local_reexport_takes_local_declaration_kind():
    """`export { X }` with no `from` resolves X's kind from its in-file declaration."""
    res = _extract()
    kinds = {s.name: s.kind for s in res.symbols if s.exported}
    assert kinds["localConst"] == "const"     # over `const localConst = 1`
    assert kinds["LocalClass"] == "class"      # over `class LocalClass {}`
    assert kinds["renamedConst"] == "const"    # `localConst as renamedConst`


def test_cross_module_reexport_is_exported_unknown_kind():
    """`export { A } from "./m"` and `export type { T } from "./m"` are exported (kind unknown)."""
    res = _extract()
    exported = {s.name for s in res.symbols if s.exported}
    assert {"Imported", "Other", "Public", "OnlyType"} <= exported
    kinds = {s.name: s.kind for s in res.symbols if s.exported}
    # No local declaration to resolve -> kind stays unknown, but the name IS on the surface.
    assert kinds["Imported"] == "unknown"
    assert kinds["Public"] == "unknown"        # `Renamed as Public`
    assert kinds["OnlyType"] == "unknown"


def test_namespace_reexport_kind():
    """`export * as ns from "./m"` binds an exported `namespace` symbol."""
    res = _extract()
    kinds = {s.name: s.kind for s in res.symbols if s.exported}
    assert kinds["ns"] == "namespace"


def test_star_reexport_emits_no_symbol_but_keeps_import_edge():
    """`export * from "./m"` has no enumerable names -> no symbol, but the import edge stays."""
    res = _extract()
    # No symbol named after the star target; the module is recorded as an import.
    assert "barrel" not in {s.name for s in res.symbols}
    assert "./barrel" in {i.module for i in res.imports}


def test_internal_declarations_not_exported():
    """A bare `const`/`class` with no `export` is captured but flagged exported=False."""
    res = _extract()
    internal = {s.name: s.exported for s in res.symbols if not s.exported}
    assert internal.get("localConst") is False
    assert internal.get("LocalClass") is False


def test_defining_file_is_the_declaration_file():
    """A symbol's file is where it is declared/exported (here, the single module)."""
    res = _extract()
    assert all(s.line >= 1 for s in res.symbols)
    # Path round-trips verbatim (posix), so `_exposes_for` attributes the symbol to this file.
    assert res.path == "mod.ts"


def test_extraction_is_deterministic():
    """Re-extracting the same source yields a byte-stable structure hash (no set ordering)."""
    a = _extract()
    b = _extract()
    assert a.structure_hash == b.structure_hash
    assert [s.to_dict() for s in a.symbols] == [s.to_dict() for s in b.symbols]
