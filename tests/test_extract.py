"""Tests for the tree-sitter extraction layer (Python + TypeScript adapters + hashing)."""

from __future__ import annotations

from compact.extract import (
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


# ---- registry ----
def test_supported_extensions():
    exts = supported_extensions()
    assert ".py" in exts
    assert ".ts" in exts
    assert ".tsx" in exts


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
