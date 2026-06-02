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
    """registry.get_adapter must dispatch a .py file to the python adapter purely by extension — the one is-this-language-X check."""
    adapter = get_adapter("mod.py")
    assert adapter is not None
    assert adapter.language_name == "python"


def test_python_exports_public_symbols_only():
    """The public surface must exclude leading-underscore names — a leaked _private would bloat the token-lean export map describe emits."""
    res = get_adapter("mod.py").extract("mod.py", PY_SRC)
    exported = {s.name for s in res.symbols if s.exported}
    assert "public_fn" in exported
    assert "PublicClass" in exported
    assert "_private" not in exported  # leading underscore => internal


def test_python_symbol_kinds():
    """Symbol kind classification must distinguish function/class/const/variable — kind drives describe rendering and contract checks downstream."""
    res = get_adapter("mod.py").extract("mod.py", PY_SRC)
    kinds = {s.name: s.kind for s in res.symbols}
    assert kinds["public_fn"] == "function"
    assert kinds["PublicClass"] == "class"
    assert kinds["CONST_VALUE"] == "const"  # uppercase top-level assignment
    assert kinds["lower_var"] == "variable"


def test_python_imports():
    """Import edges (module + imported names) must be captured — they are the propagation graph validate walks; a dropped edge hides a real dependency."""
    res = get_adapter("mod.py").extract("mod.py", PY_SRC)
    modules = {imp.module for imp in res.imports}
    assert "os" in modules
    assert "a.b" in modules
    ab = next(i for i in res.imports if i.module == "a.b")
    assert {"c", "d"} <= set(ab.names)


def test_python_orm_model_exports_table_name():
    """Django Meta.db_table and SQLAlchemy __tablename__ must surface as the real table name, not the class name — the schema surface keys on table identity."""
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
    """Both .ts and .tsx must dispatch to the typescript adapter — a missed .tsx extension would silently drop every React component file from the map."""
    assert get_adapter("auth.ts").language_name == "typescript"
    assert get_adapter("comp.tsx") is not None


def test_typescript_exports_all_declaration_kinds():
    """Every exported TS declaration form (function/const/class/interface/type/default) must surface with its correct kind — a missed form undercounts the public API."""
    res = get_adapter("auth.ts").extract("auth.ts", TS_SRC)
    exported = {s.name for s in res.symbols if s.exported}
    assert {"login", "verify", "Repo", "User", "Id", "mainFn"} <= exported
    kinds = {s.name: s.kind for s in res.symbols}
    assert kinds["login"] == "function"
    assert kinds["Repo"] == "class"
    assert kinds["User"] == "interface"
    assert kinds["Id"] == "type"


def test_typescript_internal_not_exported():
    """A function without the `export` keyword must be marked exported=False — leaking module-internal helpers into the public surface bloats the token map."""
    res = get_adapter("auth.ts").extract("auth.ts", TS_SRC)
    by_name = {s.name: s.exported for s in res.symbols}
    assert by_name.get("internalHelper") is False


def test_typescript_imports():
    """A TS named import must record both the module specifier and the imported names — these edges feed the resolver and propagation graph."""
    res = get_adapter("auth.ts").extract("auth.ts", TS_SRC)
    modules = {i.module for i in res.imports}
    assert "../database" in modules
    db = next(i for i in res.imports if i.module == "../database")
    assert {"findUser", "createUser"} <= set(db.names)


def test_typescript_orm_exports_table_name():
    """Drizzle pgTable("users",…) and a @Entity("accounts") decorator must yield the declared table name — the data-boundary surface keys on real table identity, not the JS binding."""
    src = b"""import { pgTable } from "drizzle-orm/pg-core";
export const users = pgTable("users", {});

@Entity("accounts")
export class Account {}
"""
    res = get_adapter("schema.ts").extract("schema.ts", src)
    tables = {s.name: s.metadata.get("model") for s in res.symbols if s.kind == "table"}
    assert tables == {"users": "users", "accounts": "Account"}


def _py_tables(src: bytes) -> dict[str, str]:
    res = get_adapter("m.py").extract("m.py", src)
    return {s.name: s.metadata.get("model") for s in res.symbols if s.kind == "table"}


def _py_kinds(src: bytes) -> dict[str, str]:
    res = get_adapter("m.py").extract("m.py", src)
    return {s.name: s.kind for s in res.symbols}


# ---- Python ORM false-positives / false-negatives ----
def test_orm_django_abstract_model_is_not_a_table():
    """An abstract Django base must not fabricate a phantom table — a false-positive table pollutes the schema surface and triggers spurious data-boundary contract checks."""
    # Meta.abstract = True is a base/mixin, never a real table (no false positive).
    assert _py_tables(b"class Base(models.Model):\n    class Meta:\n        abstract = True\n") == {}


def test_orm_tablename_substring_is_not_a_table():
    """A class whose body merely mentions "__tablename__" inside a string stays a class, not a table — detection must be structural, not a substring match (no false positive)."""
    # A string literal that merely contains "__tablename__" must not fabricate a table.
    assert _py_kinds(b'class Foo(Bar):\n    note = "see __tablename__ docs"\n')["Foo"] == "class"


def test_orm_fstring_tablename_falls_back_to_class_name():
    """A dynamic f-string __tablename__ is not statically knowable, so the table name falls back to the class name rather than fabricating a bogus literal (zero-LLM, static-only)."""
    # A dynamic f-string name is not statically knowable → fall back, don't fabricate.
    assert _py_tables(b'class A(Base):\n    __tablename__ = f"p_{x}"\n') == {"A": "A"}


def test_orm_sqlalchemy_imperative_table_is_detected():
    """SQLAlchemy's imperative __table__ = Table("widgets",…) form must be detected, not just declarative __tablename__ — both spellings map to the same real table."""
    assert _py_tables(b'class A(Base):\n    __table__ = Table("widgets", meta)\n') == {"widgets": "A"}


def test_orm_plain_class_stays_class():
    """A class with no ORM markers stays kind=class — ORM table detection must not over-trigger on ordinary classes (no false positive)."""
    assert _py_kinds(b"class Foo(Bar):\n    pass\n")["Foo"] == "class"


# ---- Python __all__ export gating (Tier 4) ----
def _py_exported(src: bytes) -> dict[str, bool]:
    res = get_adapter("m.py").extract("m.py", src)
    return {s.name: s.exported for s in res.symbols}


def test_python_all_restricts_export_surface():
    """A literal __all__ is the authoritative public surface: only its members are exported, and a non-listed public-cased name is exported=False even without a leading underscore — honouring the author's explicit choice over the underscore heuristic."""
    src = b'''__all__ = ["public_a", "PublicClass"]

def public_a():
    pass

def public_b():
    pass

class PublicClass:
    pass
'''
    exported = _py_exported(src)
    assert exported["public_a"] is True
    assert exported["PublicClass"] is True
    assert exported["public_b"] is False  # public-cased but omitted from __all__ => private


def test_python_all_can_export_underscore_name():
    """A leading-underscore name listed in __all__ is public — __all__ overrides the underscore convention (the author deliberately exported it)."""
    src = b'__all__ = ["_internal"]\n\ndef _internal():\n    pass\n\ndef helper():\n    pass\n'
    exported = _py_exported(src)
    assert exported["_internal"] is True       # listed => public despite the underscore
    assert exported["helper"] is False         # not listed => private


def test_python_no_all_keeps_underscore_rule():
    """With no __all__ the leading-underscore convention still governs exports — the existing default is preserved for the (common) modules that don't declare one."""
    src = b"def public_fn():\n    pass\n\ndef _private():\n    pass\n"
    exported = _py_exported(src)
    assert exported["public_fn"] is True
    assert exported["_private"] is False


def test_python_all_tuple_form():
    """The tuple form __all__ = ("a",) is recognised like the list form — both are common literal spellings."""
    src = b'__all__ = ("kept",)\n\ndef kept():\n    pass\n\ndef dropped():\n    pass\n'
    exported = _py_exported(src)
    assert exported["kept"] is True
    assert exported["dropped"] is False


def test_python_dynamic_all_falls_back_to_underscore_rule():
    """A non-literal __all__ (built dynamically) is not statically knowable, so extraction falls back to the underscore rule rather than guessing — zero-LLM, static-only, fail-soft."""
    src = b'__all__ = _base + ["x"]\n\ndef public_fn():\n    pass\n\ndef _private():\n    pass\n'
    exported = _py_exported(src)
    assert exported["public_fn"] is True   # fell back to underscore rule
    assert exported["_private"] is False


# ---- Python same-file Django inheritance (Tier 4) ----
def test_orm_django_same_file_inheritance_is_a_table():
    """A model inheriting from a Django base defined IN THE SAME FILE is recognised as a table — real Django codebases use a project AbstractBase(models.Model) that concrete models extend."""
    src = b'''from django.db import models

class AbstractBase(models.Model):
    class Meta:
        abstract = True

class Order(AbstractBase):
    pass
'''
    res = get_adapter("models.py").extract("models.py", src)
    kinds = {s.name if s.kind != "table" else s.metadata.get("model"): s.kind for s in res.symbols}
    # AbstractBase is abstract -> not a table; Order inherits Django-ness transitively -> table.
    assert kinds["AbstractBase"] == "class"
    assert kinds["Order"] == "table"
    tables = {s.metadata.get("model"): s.name for s in res.symbols if s.kind == "table"}
    assert tables == {"Order": "Order"}


def test_orm_django_cross_file_base_is_not_a_table():
    """A base class merely IMPORTED from another file is NOT resolved — same-file-only inheritance keeps extraction per-file cacheable; the subclass stays a plain class, never a phantom table."""
    src = b'''from .common import AbstractBase

class Order(AbstractBase):
    pass
'''
    res = get_adapter("models.py").extract("models.py", src)
    assert {s.name: s.kind for s in res.symbols}["Order"] == "class"
    assert [s for s in res.symbols if s.kind == "table"] == []


def test_orm_django_inheritance_cycle_does_not_crash():
    """A pathological inheritance cycle with no real Model root must not hang or crash extraction (fixpoint converges) — fail-soft on degenerate input."""
    src = b"class A(B):\n    pass\n\nclass B(A):\n    pass\n"
    res = get_adapter("m.py").extract("m.py", src)
    assert {s.name: s.kind for s in res.symbols} == {"A": "class", "B": "class"}


def test_orm_typeorm_entity_object_name_form():
    """TypeORM's @Entity({ name: "accounts" }) options-object form must yield the declared table name, like the positional @Entity("accounts") form — both spellings are real."""
    src = b'@Entity({ name: "accounts" })\nexport class Account {}\n'
    res = get_adapter("e.ts").extract("e.ts", src)
    assert {s.name: s.metadata.get("model") for s in res.symbols if s.kind == "table"} == {"accounts": "Account"}


def test_orm_drizzle_sqlite_and_mysql_tables():
    """Drizzle's sqliteTable/mysqlTable builders (not just pgTable) must be recognized — Bounds must cover every Drizzle dialect or it undercounts tables on non-Postgres repos."""
    src = (b'export const a = sqliteTable("a_tbl", {});\n'
           b'export const b = mysqlTable("b_tbl", {});\n')
    res = get_adapter("d.ts").extract("d.ts", src)
    assert {s.name for s in res.symbols if s.kind == "table"} == {"a_tbl", "b_tbl"}


def test_prisma_adapter_extracts_models_as_tables():
    """Prisma models become tables: @@map("users") sets the table name and @map("post_title") renames a column — the surface reflects the mapped DB name, not the Prisma field name."""
    src = (b'model User {\n  id Int @id\n  email String\n  @@map("users")\n}\n'
           b'model Post {\n  id Int @id\n  title String @map("post_title")\n}\n')
    res = get_adapter("schema.prisma").extract("schema.prisma", src)
    assert res.language == "prisma"
    tables = {s.name: sorted(s.metadata.get("columns", [])) for s in res.symbols if s.kind == "table"}
    assert tables == {"users": ["email", "id"], "Post": ["id", "post_title"]}


def test_prisma_brace_inside_string_does_not_desync():
    """A brace inside a string default must not desync the model-block scanner — else model B is dropped or A gains a phantom 'model' column, corrupting the table surface."""
    # A `{`/`}` inside a string default must not swallow or split the next model.
    src = (b'model A {\n  id Int @id\n  cfg String @default("{")\n}\n'
           b'model B {\n  id Int @id\n}\n')
    res = get_adapter("schema.prisma").extract("schema.prisma", src)
    names = {s.name for s in res.symbols if s.kind == "table"}
    assert names == {"A", "B"}  # B not dropped; no phantom 'model' column on A
    a = next(s for s in res.symbols if s.name == "A")
    assert "model" not in a.metadata.get("columns", [])


def test_prisma_relation_fields_are_not_columns():
    """Prisma relation fields (model/enum-typed) are joins, not DB columns, so they must be excluded from a table's column surface — else describe reports phantom columns and a column-granular contract like User.posts passes check_contract incorrectly."""
    # Relation fields (PascalCase model/enum types) are joins, not database columns, so they
    # must not appear in a table's column surface — else `describe` reports phantom columns and
    # a column-granular contract like `User.posts` passes check_contract incorrectly.
    src = (b"model User {\n"
           b"  id Int @id\n"
           b"  email String\n"
           b"  posts Post[]\n"            # relation (model list)
           b"  profile Profile?\n"        # relation (optional model)
           b"  role Role\n"               # relation (enum)
           b"  tags String[]\n"           # scalar list — still a column
           b"}\n"
           b"model Post {\n"
           b"  id Int @id\n"
           b"  title String\n"
           b"  author User @relation(fields: [authorId], references: [id])\n"  # relation
           b"  authorId Int\n"            # scalar FK — a real column
           b"}\n")
    res = get_adapter("schema.prisma").extract("schema.prisma", src)
    cols = {s.name: sorted(s.metadata.get("columns", [])) for s in res.symbols if s.kind == "table"}
    assert cols == {
        "User": ["email", "id", "tags"],
        "Post": ["authorId", "id", "title"],
    }


def test_sql_adapter_extracts_ddl_operations():
    """CREATE TABLE, ADD COLUMN, and RENAME COLUMN must each surface as a typed symbol carrying its schema_op — these ops are what the migration fold replays to derive the final table set."""
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


def test_sql_adapter_extracts_functions_views_indexes_triggers_types():
    """Functions/views/indexes/triggers/types must each surface as a typed symbol (with index/trigger recording their target table) — previously dropped, leaving the non-table schema surface blank."""
    # Grammar-native statements that were previously dropped: functions (RPCs), views,
    # indexes, triggers, and types must each surface as a typed symbol.
    src = b"""create or replace function public.bump() returns trigger language plpgsql as $$ begin return new; end; $$;
create view active_users as select * from profiles where active;
create index idx_email on profiles (email);
create trigger trg_bump before update on profiles execute function bump();
create type mood as enum ('happy', 'sad');
"""
    res = get_adapter("010_objects.sql").extract("010_objects.sql", src)
    assert res.error is None
    ops = {(s.kind, s.name) for s in res.symbols}
    assert ("function", "public.bump") in ops
    assert ("view", "active_users") in ops
    assert ("index", "idx_email") in ops
    assert ("trigger", "trg_bump") in ops
    assert ("type", "mood") in ops
    # index/trigger record the table they target
    by_name = {s.name: s for s in res.symbols}
    assert by_name["idx_email"].metadata.get("table") == "profiles"
    assert by_name["trg_bump"].metadata.get("table") == "profiles"


def test_sql_adapter_recovers_policies_and_rls():
    """tree-sitter-sql can't parse Postgres RLS; the adapter must recover policy + enable_rls from the misparse rather than drop them as schema_error, and emit no phantom column or error."""
    # tree-sitter-sql can't parse Postgres RLS; the adapter recovers policies from ERROR-node
    # text and RLS structurally from the misparsed alter, instead of dropping them as
    # schema_error, and emits no phantom column for the RLS statement.
    src = b"""create table profiles (id uuid primary key);
alter table profiles enable row level security;
create policy "owner can read" on profiles for select using (auth.uid() = id);
"""
    res = get_adapter("011_rls.sql").extract("011_rls.sql", src)
    assert res.error is None
    by_kind = {s.kind: s for s in res.symbols}
    assert by_kind["rls"].name == "profiles"
    assert by_kind["rls"].metadata.get("schema_op") == "enable_rls"
    assert by_kind["policy"].name == "profiles.owner can read"
    assert by_kind["policy"].metadata.get("table") == "profiles"
    # No phantom column named "enable" from the misparsed RLS statement, and no schema_error.
    assert not any(s.name.endswith(".enable") for s in res.symbols)
    assert "schema_error" not in by_kind


def test_sql_disable_rls_is_recovered():
    """DISABLE ROW LEVEL SECURITY must be recovered with schema_op=disable_rls (not dropped as schema_error) so the fold can net an enabled-then-disabled table out of the RLS posture."""
    res = get_adapter("d.sql").extract("d.sql", b"alter table bar disable row level security;\n")
    assert res.error is None
    rls = [s for s in res.symbols if s.kind == "rls"]
    assert rls and rls[0].name == "bar" and rls[0].metadata.get("schema_op") == "disable_rls"
    assert not any(s.kind == "schema_error" for s in res.symbols)


def test_sql_policy_in_a_comment_is_not_a_symbol():
    """A commented-out CREATE POLICY must not produce a phantom policy — recovery scans only ERROR-node text and a comment is its own node type (no false positive)."""
    # Policy recovery scans only ERROR-node text; a comment is its own node type, never an
    # ERROR, so a commented-out CREATE POLICY must not produce a phantom policy symbol.
    src = b"-- create policy fake on nope for all using (true)\ncreate table t (id int);\n"
    res = get_adapter("c.sql").extract("c.sql", src)
    assert res.error is None
    assert not any(s.kind == "policy" for s in res.symbols)
    assert {s.kind for s in res.symbols} == {"table"}


def test_sql_genuine_error_beside_policy_is_not_masked():
    """A genuine broken statement beside a recoverable policy must still surface as schema_error — RLS/policy recovery must never discount a real parse error into silence (fail-soft, report-hard)."""
    # A real broken statement next to a policy must still report as schema_error — the RLS/
    # policy recovery must never discount a genuine parse error into silence.
    src = b"create table t (id int);\nalter table t add column;\ncreate policy p on t for all using (true);\n"
    res = get_adapter("b.sql").extract("b.sql", src)
    schema_errors = [s for s in res.symbols if s.kind == "schema_error"]
    assert schema_errors, "a genuine parse error must still surface as schema_error"
    assert any(s.kind == "table" for s in res.symbols)  # the valid statement still folds


def test_sql_broken_statement_fused_into_policy_error_is_not_masked():
    """When tree-sitter fuses a CREATE POLICY and an adjacent broken statement into one ERROR node, recovering the policy must not mark the whole node explained — the adapter re-parses with policies blanked to recount the genuine error (report-hard)."""
    # tree-sitter fuses a CREATE POLICY and an adjacent broken statement into ONE ERROR node.
    # Recovering the policy must NOT mark the whole node "explained" and swallow the real
    # error: the adapter blanks recovered policies and re-parses to recount genuine errors.
    src = b"create policy p on t for all using (true);\nalter table t add column;\n"
    res = get_adapter("f.sql").extract("f.sql", src)
    kinds = {s.kind for s in res.symbols}
    assert "policy" in kinds          # policy still recovered
    assert "schema_error" in kinds    # the fused broken statement still reports


def test_sql_all_error_with_revision_header_is_hard_failure():
    """A wholly-unparsable migration is a hard failure even with a valid `-- revision` header — the schema_meta header is not a parsed statement and must not be folded as a partial result that masks the breakage (report-hard)."""
    # A migration whose body is wholly unparsable is a hard extraction failure even when it
    # carries a valid `-- revision` header: the schema_meta header is not a parsed statement,
    # so it must not be folded as a partial/cached result that masks the breakage.
    src = b"-- revision: 1\n-- down_revision: 0\n@@@ not sql at all @@@\n"
    res = get_adapter("001_broken.sql").extract("001_broken.sql", src)
    assert res.error  # non-empty hard error, not a partial result
    assert not res.symbols  # no schema_meta / partial symbols leak through


def test_sql_ddl_inside_transaction_block_is_extracted():
    """The Supabase BEGIN;…COMMIT; shape: the walk must descend into the transaction container so table, RLS, and policy are all recovered — none dropped because they're nested."""
    # The common Supabase shape: DDL wrapped in BEGIN; … COMMIT;. The walk must descend into
    # the transaction container — table, RLS, and policy are all recovered, none dropped.
    src = b"""BEGIN;
CREATE TABLE accounts (id uuid primary key, owner uuid);
ALTER TABLE accounts ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_rw ON accounts FOR ALL USING (owner = auth.uid());
COMMIT;
"""
    res = get_adapter("010_txn.sql").extract("010_txn.sql", src)
    assert res.error is None
    by_kind = {s.kind: s for s in res.symbols}
    assert by_kind["table"].name == "accounts"
    assert by_kind["rls"].metadata.get("schema_op") == "enable_rls"
    assert by_kind["policy"].name == "accounts.owner_rw"


def test_sql_table_with_inline_constraint_is_recovered():
    """A table-level CONSTRAINT clause errors the whole CREATE TABLE in tree-sitter-sql; best-effort recovery must keep the table + its columns and raise no schema_error (the unmodeled constraint tail is not a catalog loss)."""
    # tree-sitter-sql cannot parse a table-level CONSTRAINT clause and errors the whole
    # CREATE TABLE; best-effort recovery keeps the table + its columns (the unmodeled
    # constraint tail is not a catalog loss, so no schema_error is raised).
    src = b"""CREATE TABLE IF NOT EXISTS public.saved (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  CONSTRAINT saved_unique UNIQUE (user_id, id)
);"""
    res = get_adapter("011_constraint.sql").extract("011_constraint.sql", src)
    assert res.error is None
    table = next(s for s in res.symbols if s.kind == "table")
    assert table.name == "saved"  # schema qualifier dropped (canonical bare name)
    assert table.metadata["columns"] == ["id", "user_id"]
    assert not any(s.kind == "schema_error" for s in res.symbols)


def test_sql_table_name_is_canonical_bare_across_ops():
    """A table CREATEd as public.t but ALTERed bare (or vice-versa) must canonicalise to one bare name — else the fold sees two tables and the ALTER never lands on the CREATE."""
    # A table CREATEd schema-qualified but ALTERed bare (and vice-versa) must fold to one
    # entry: every table reference canonicalises to its bare name.
    create = get_adapter("a.sql").extract("a.sql", b"CREATE TABLE public.t (id int);")
    alter = get_adapter("b.sql").extract("b.sql", b"ALTER TABLE t ADD COLUMN x int;")
    cnames = [s.name for s in create.symbols if s.kind == "table"]
    anames = [s.metadata.get("table") for s in alter.symbols if s.kind == "column"]
    assert cnames == ["t"] and anames == ["t"]


def test_sql_policy_lifecycle_create_drop_alter():
    """DROP and ALTER POLICY must be extracted (not just CREATE), each carrying its schema_op so the fold can net a dropped or renamed policy out of the live RLS surface."""
    # DROP/ALTER POLICY are extracted (not just CREATE), each carrying its schema_op so the
    # fold can net a dropped or renamed policy out of the live surface.
    src = b"""CREATE POLICY p ON t FOR ALL USING (true);
DROP POLICY IF EXISTS p ON t;
CREATE POLICY q ON t FOR SELECT USING (true);
ALTER POLICY q ON t RENAME TO q2;
"""
    res = get_adapter("p.sql").extract("p.sql", src)
    ops = [(s.metadata.get("schema_op"), s.name) for s in res.symbols if s.kind == "policy"]
    assert ("create_policy", "t.p") in ops
    assert ("drop_policy", "t.p") in ops
    assert ("alter_policy", "t.q") in ops


def test_sql_force_rls_is_recovered():
    """FORCE ROW LEVEL SECURITY (the owner-bypass-off variant) must be recovered with schema_op=force_rls so the RLS posture distinguishes it from a plain enable."""
    res = get_adapter("f.sql").extract("f.sql", b"ALTER TABLE t FORCE ROW LEVEL SECURITY;\n")
    rls = [s for s in res.symbols if s.kind == "rls"]
    assert rls and rls[0].metadata.get("schema_op") == "force_rls"


def test_sql_policy_in_string_literal_is_not_a_symbol():
    """A 'CREATE POLICY …' string in seed data (INSERT values) must be masked, never a phantom policy — string contents are not DDL (no false positive)."""
    # A 'CREATE POLICY …' string inside seed data (or an EXECUTE) is masked, never a phantom.
    src = b"INSERT INTO audit (action) VALUES ('CREATE POLICY hacker ON t FOR ALL USING (true)');\n"
    res = get_adapter("s.sql").extract("s.sql", src)
    assert not any(s.kind == "policy" for s in res.symbols)


def test_sql_policy_in_dollar_quoted_body_is_not_a_symbol():
    """A CREATE POLICY inside a $$-quoted function body must be masked (never a phantom) while a real CREATE POLICY beside the function is still recovered — body text is not live DDL."""
    # A CREATE POLICY inside a function body ($$ … $$ / EXECUTE) is masked, never a phantom —
    # tree-sitter spans only the $$ delimiters, so the body is blanked textually. The real
    # CREATE POLICY beside the function is still recovered; the in-body one never is.
    src = b"""CREATE POLICY real ON t FOR ALL USING (true);
CREATE FUNCTION f() RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  EXECUTE 'CREATE POLICY sneaky ON t FOR ALL USING (true)';
END;
$$;
"""
    res = get_adapter("fn.sql").extract("fn.sql", src)
    policies = {s.name for s in res.symbols if s.kind == "policy"}
    assert policies == {"t.real"}  # the body's "sneaky" policy is masked, never a phantom


def test_sql_policy_line_number_correct_with_multibyte_identifier():
    """A multibyte char (café ☕) before a policy must not desync its line number or blank span — byte/char-offset parity, else error counts and adjacent statements get mis-attributed."""
    # Byte/char-offset parity: a multibyte char before a policy must not desync its line number
    # or the blank span (which would mis-count errors or eat an adjacent statement).
    src = "-- café ☕ comment\ncreate policy p on t for all using (true);\n".encode("utf-8")
    res = get_adapter("u.sql").extract("u.sql", src)
    pol = next(s for s in res.symbols if s.kind == "policy")
    assert pol.line == 2  # the policy is on line 2, not line 1


def test_sql_fragmented_pgdump_recovers_policies():
    """A pg_dump-style file the grammar shreds into many ERROR fragments must still yield all its policies — recovery scans masked text rather than a single clean node."""
    # A pg_dump-style file the grammar shreds into many ERROR fragments still yields its
    # policies, because recovery scans masked text rather than a single clean node.
    src = (b'CREATE POLICY "a" ON "public"."t1" FOR SELECT USING (true);\n'
           b'CREATE POLICY "b" ON "public"."t2" FOR INSERT WITH CHECK (true);\n'
           b'@@@ garbage that fragments the parse @@@\n'
           b'CREATE POLICY "c" ON "public"."t1" FOR UPDATE USING (true);\n')
    res = get_adapter("dump.sql").extract("dump.sql", src)
    policies = {s.name for s in res.symbols if s.kind == "policy"}
    assert policies == {"t1.a", "t2.b", "t1.c"}


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
    """Plain, destructured, and bare side-effect require() calls must all record an import edge — CommonJS deps are part of the same propagation graph as ESM imports."""
    res = get_adapter("io.cjs").extract("io.cjs", CJS_SRC)
    modules = {i.module for i in res.imports}
    # Plain, destructured, and bare side-effect requires all record an edge.
    assert {"fs", "./io", "./side-effect"} <= modules


def test_commonjs_module_exports_become_symbols():
    """module.exports = helper exports `default`, while module.exports.named and exports.flag export their property names — all three CommonJS export forms surface in the public API."""
    res = get_adapter("io.cjs").extract("io.cjs", CJS_SRC)
    exported = {s.name for s in res.symbols if s.exported}
    # `module.exports = helper` -> default; named property assignments -> their names.
    assert "default" in exported  # module.exports = helper
    assert "named" in exported  # module.exports.named = ...
    assert "flag" in exported  # exports.flag = ...


def test_commonjs_chained_named_exports_become_symbols():
    """A chained assignment `exports.foo = exports.bar = helper` must export BOTH names, not just the outermost — a missed inner name undercounts the public surface."""
    # `exports.foo = exports.bar = helper` exports BOTH names, not just the outermost.
    res = get_adapter("chain.cjs").extract(
        "chain.cjs", b"function helper() {}\nexports.foo = exports.bar = helper;\n"
    )
    exported = {s.name for s in res.symbols if s.exported}
    assert "foo" in exported
    assert "bar" in exported


def test_commonjs_dynamic_require_records_no_edge():
    """require() with a non-literal argument has no statically knowable module, so no edge is recorded — the static-only extractor must not fabricate an edge from a variable name (zero-LLM)."""
    # require() with a non-literal argument has no statically knowable module.
    res = get_adapter("dyn.js").extract(
        "dyn.js", b'const name = "x";\nconst mod = require(name);\nconst ok = require("static");\n'
    )
    modules = {i.module for i in res.imports}
    assert "static" in modules
    assert "name" not in modules and "x" not in modules


# ---- TypeScript/JS: barrel re-exports ----
def test_barrel_export_star_records_import_edge():
    """`export * from "./m"` records an import edge (it's a propagation dependency) but is intentionally NOT expanded into symbols — the cross-file union stays unexpanded."""
    # `export * from "./m"` is a propagation dependency; capture it as an import edge.
    res = get_adapter("barrel.ts").extract("barrel.ts", b'export * from "./m";\n')
    modules = {i.module for i in res.imports}
    assert "./m" in modules
    # The cross-file union of `export *` is intentionally not expanded into symbols.
    assert not res.symbols


def test_barrel_export_star_as_namespace():
    """`export * as ns from "./other"` records the edge AND binds the named namespace symbol `ns` — unlike bare `export *`, the namespace alias is a concrete export to surface."""
    # `export * as ns from "./m"` records the edge AND binds the namespace symbol `ns`.
    res = get_adapter("barrel.ts").extract("barrel.ts", b'export * as ns from "./other";\n')
    modules = {i.module for i in res.imports}
    assert "./other" in modules
    exported = {s.name for s in res.symbols if s.exported}
    assert "ns" in exported


def test_esm_named_reexport_still_records_edge_and_symbols():
    """`export { a as b, c } from "./named"` must keep BOTH the import edge and the re-exported symbols (b, c) — a re-export is simultaneously a dependency and a public surface entry."""
    # Regression guard: `export { a as b } from "mod"` keeps both symbol and edge.
    res = get_adapter("re.ts").extract("re.ts", b'export { a as b, c } from "./named";\n')
    modules = {i.module for i in res.imports}
    assert "./named" in modules
    exported = {s.name for s in res.symbols if s.exported}
    assert {"b", "c"} <= exported


# ---- registry ----
def test_supported_extensions():
    """The registry must advertise every adapter's extension (.py/.ts/.tsx/.sql) — this set gates which files the recursive source walk feeds to extraction."""
    exts = supported_extensions()
    assert ".py" in exts
    assert ".ts" in exts
    assert ".tsx" in exts
    assert ".sql" in exts


def test_unsupported_extension_returns_none():
    """An unsupported extension (.md, .rs) returns None rather than raising — the walk silently skips it, never crashing on a mixed repo (fail-soft)."""
    assert get_adapter("README.md") is None
    assert get_adapter("main.rs") is None


# ---- hashing ----
def test_content_hash_is_deterministic_and_sensitive():
    """content_hash must be stable for identical bytes and differ for any byte change — it is the cache key the quick path uses to skip re-extracting unchanged files (determinism)."""
    assert content_hash(b"abc") == content_hash(b"abc")
    assert content_hash(b"abc") != content_hash(b"abd")


def test_structure_hash_ignores_body_changes():
    """structure_hash must stay identical when only a function body changes (same interface) while content_hash differs — this is what lets the quick path skip propagation on body-only edits."""
    adapter = get_adapter("m.py")
    a = adapter.extract("m.py", b"def f():\n    return 1\n")
    b = adapter.extract("m.py", b"def f():\n    return 2  # different body\n")
    assert a.structure_hash == b.structure_hash  # same interface surface
    assert a.content_hash != b.content_hash  # but the bytes differ


def test_structure_hash_changes_on_new_export():
    """structure_hash must change when a new export is added — otherwise the quick path would skip propagating an interface change and miss a downstream contract break."""
    adapter = get_adapter("m.py")
    a = adapter.extract("m.py", b"def f():\n    return 1\n")
    b = adapter.extract("m.py", b"def f():\n    return 1\n\n\ndef g():\n    return 2\n")
    assert a.structure_hash != b.structure_hash


def test_parse_failure_is_soft():
    """A syntactically broken Python file must return a result (with language set), never raise — a single unparsable file becomes an Issue, not a crash (fail-soft)."""
    # tree-sitter is error-tolerant; a broken file must never raise.
    res = get_adapter("m.py").extract("m.py", b"def (((:\n")
    assert res is not None
    assert res.language == "python"
