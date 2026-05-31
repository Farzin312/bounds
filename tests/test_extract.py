"""Tests for the tree-sitter extraction layer (Python + TypeScript adapters + hashing)."""

from __future__ import annotations

from bounds.extract import (
    content_hash,
    get_adapter,
    supported_extensions,
)

PY_SRC = b"""import os
from a.b import c, d
from . import sibling

def public_fn(x):
    return x

class PublicClass:
    pass

def _private():
    pass

CONST_VALUE = 3
lower_var = 5
"""

TS_SRC = b"""import { findUser, createUser } from "../database";
import express from "express";

export function login(u) { return true; }
export const verify = (t) => t.length > 0;
export class Repo {}
export interface User { id: string; }
export type Id = string;
function internalHelper() {}
export default function mainFn() {}
"""


# ---- Python adapter ----
def test_python_adapter_resolves_by_extension():
    adapter = get_adapter("mod.py")
    assert adapter is not None
    assert adapter.language_name == "python"


def test_python_exports_public_symbols_only():
    res = get_adapter("mod.py").extract("mod.py", PY_SRC)
    exported = {s.name for s in res.symbols if s.exported}
    assert "public_fn" in exported
    assert "PublicClass" in exported
    assert "_private" not in exported  # leading underscore => internal


def test_python_symbol_kinds():
    res = get_adapter("mod.py").extract("mod.py", PY_SRC)
    kinds = {s.name: s.kind for s in res.symbols}
    assert kinds["public_fn"] == "function"
    assert kinds["PublicClass"] == "class"
    assert kinds["CONST_VALUE"] == "const"  # uppercase top-level assignment
    assert kinds["lower_var"] == "variable"


def test_python_imports():
    res = get_adapter("mod.py").extract("mod.py", PY_SRC)
    modules = {imp.module for imp in res.imports}
    assert "os" in modules
    assert "a.b" in modules
    ab = next(i for i in res.imports if i.module == "a.b")
    assert {"c", "d"} <= set(ab.names)


def test_python_orm_model_exports_table_name():
    src = b"""from django.db import models

class User(models.Model):
    class Meta:
        db_table = "users"

class Account(Base):
    __tablename__ = "accounts"
"""
    res = get_adapter("models.py").extract("models.py", src)
    tables = {s.name: s.metadata.get("model") for s in res.symbols if s.kind == "table"}
    assert tables == {"users": "User", "accounts": "Account"}


# ---- TypeScript adapter ----
def test_typescript_adapter_resolves_ts_and_tsx():
    assert get_adapter("auth.ts").language_name == "typescript"
    assert get_adapter("comp.tsx") is not None


def test_typescript_exports_all_declaration_kinds():
    res = get_adapter("auth.ts").extract("auth.ts", TS_SRC)
    exported = {s.name for s in res.symbols if s.exported}
    assert {"login", "verify", "Repo", "User", "Id", "mainFn"} <= exported
    kinds = {s.name: s.kind for s in res.symbols}
    assert kinds["login"] == "function"
    assert kinds["Repo"] == "class"
    assert kinds["User"] == "interface"
    assert kinds["Id"] == "type"


def test_typescript_internal_not_exported():
    res = get_adapter("auth.ts").extract("auth.ts", TS_SRC)
    by_name = {s.name: s.exported for s in res.symbols}
    assert by_name.get("internalHelper") is False


def test_typescript_imports():
    res = get_adapter("auth.ts").extract("auth.ts", TS_SRC)
    modules = {i.module for i in res.imports}
    assert "../database" in modules
    db = next(i for i in res.imports if i.module == "../database")
    assert {"findUser", "createUser"} <= set(db.names)


def test_typescript_orm_exports_table_name():
    src = b"""import { pgTable } from "drizzle-orm/pg-core";
export const users = pgTable("users", {});

@Entity("accounts")
export class Account {}
"""
    res = get_adapter("schema.ts").extract("schema.ts", src)
    tables = {s.name: s.metadata.get("model") for s in res.symbols if s.kind == "table"}
    assert tables == {"users": "users", "accounts": "Account"}


def test_sql_adapter_extracts_ddl_operations():
    src = b"""CREATE TABLE users (id integer primary key, email text);
ALTER TABLE users ADD COLUMN name text;
ALTER TABLE users RENAME COLUMN name TO full_name;
"""
    res = get_adapter("001_init.sql").extract("001_init.sql", src)
    assert res.language == "sql"
    ops = [(s.kind, s.name, s.metadata.get("schema_op")) for s in res.symbols]
    assert ("table", "users", "create_table") in ops
    assert ("column", "users.name", "add_column") in ops
    assert ("rename", "users.name", "rename_column") in ops


# ---- TypeScript/JS: CommonJS ----
CJS_SRC = b"""const fs = require("fs");
const { readFile, writeFile } = require("./io");
require("./side-effect");

function helper() {}
module.exports = helper;
module.exports.named = helper;
exports.flag = true;
"""


def test_commonjs_require_becomes_import_edge():
    res = get_adapter("io.cjs").extract("io.cjs", CJS_SRC)
    modules = {i.module for i in res.imports}
    # Plain, destructured, and bare side-effect requires all record an edge.
    assert {"fs", "./io", "./side-effect"} <= modules


def test_commonjs_module_exports_become_symbols():
    res = get_adapter("io.cjs").extract("io.cjs", CJS_SRC)
    exported = {s.name for s in res.symbols if s.exported}
    # `module.exports = helper` -> default; named property assignments -> their names.
    assert "default" in exported  # module.exports = helper
    assert "named" in exported  # module.exports.named = ...
    assert "flag" in exported  # exports.flag = ...


def test_commonjs_chained_named_exports_become_symbols():
    # `exports.foo = exports.bar = helper` exports BOTH names, not just the outermost.
    res = get_adapter("chain.cjs").extract(
        "chain.cjs", b"function helper() {}\nexports.foo = exports.bar = helper;\n"
    )
    exported = {s.name for s in res.symbols if s.exported}
    assert "foo" in exported
    assert "bar" in exported


def test_commonjs_dynamic_require_records_no_edge():
    # require() with a non-literal argument has no statically knowable module.
    res = get_adapter("dyn.js").extract(
        "dyn.js", b'const name = "x";\nconst mod = require(name);\nconst ok = require("static");\n'
    )
    modules = {i.module for i in res.imports}
    assert "static" in modules
    assert "name" not in modules and "x" not in modules


# ---- TypeScript/JS: barrel re-exports ----
def test_barrel_export_star_records_import_edge():
    # `export * from "./m"` is a propagation dependency; capture it as an import edge.
    res = get_adapter("barrel.ts").extract("barrel.ts", b'export * from "./m";\n')
    modules = {i.module for i in res.imports}
    assert "./m" in modules
    # The cross-file union of `export *` is intentionally not expanded into symbols.
    assert not res.symbols


def test_barrel_export_star_as_namespace():
    # `export * as ns from "./m"` records the edge AND binds the namespace symbol `ns`.
    res = get_adapter("barrel.ts").extract("barrel.ts", b'export * as ns from "./other";\n')
    modules = {i.module for i in res.imports}
    assert "./other" in modules
    exported = {s.name for s in res.symbols if s.exported}
    assert "ns" in exported


def test_esm_named_reexport_still_records_edge_and_symbols():
    # Regression guard: `export { a as b } from "mod"` keeps both symbol and edge.
    res = get_adapter("re.ts").extract("re.ts", b'export { a as b, c } from "./named";\n')
    modules = {i.module for i in res.imports}
    assert "./named" in modules
    exported = {s.name for s in res.symbols if s.exported}
    assert {"b", "c"} <= exported


# ---- registry ----
def test_supported_extensions():
    exts = supported_extensions()
    assert ".py" in exts
    assert ".ts" in exts
    assert ".tsx" in exts
    assert ".sql" in exts


def test_unsupported_extension_returns_none():
    assert get_adapter("README.md") is None
    assert get_adapter("main.rs") is None


# ---- hashing ----
def test_content_hash_is_deterministic_and_sensitive():
    assert content_hash(b"abc") == content_hash(b"abc")
    assert content_hash(b"abc") != content_hash(b"abd")


def test_structure_hash_ignores_body_changes():
    adapter = get_adapter("m.py")
    a = adapter.extract("m.py", b"def f():\n    return 1\n")
    b = adapter.extract("m.py", b"def f():\n    return 2  # different body\n")
    assert a.structure_hash == b.structure_hash  # same interface surface
    assert a.content_hash != b.content_hash  # but the bytes differ


def test_structure_hash_changes_on_new_export():
    adapter = get_adapter("m.py")
    a = adapter.extract("m.py", b"def f():\n    return 1\n")
    b = adapter.extract("m.py", b"def f():\n    return 1\n\n\ndef g():\n    return 2\n")
    assert a.structure_hash != b.structure_hash


def test_parse_failure_is_soft():
    # tree-sitter is error-tolerant; a broken file must never raise.
    res = get_adapter("m.py").extract("m.py", b"def (((:\n")
    assert res is not None
    assert res.language == "python"
