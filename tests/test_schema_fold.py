"""Direct unit tests for the SQL/Prisma schema fold (validate.schema).

Covers the false-negative / false-positive surface the data-boundary feature lives or dies on:
the migration fold to a final set, DROP/RENAME, deterministic ordering (filename prefix,
revision chain, explicit order, and the no-order advisory), per-statement fail-soft, quoted
identifiers, the deterministic schema hash, and Prisma model folding.
"""

from __future__ import annotations

from bounds.extract import get_adapter
from bounds.validate.schema import (
    order_migrations,
    schema_catalog,
    schema_diagnostics,
    schema_objects,
    schema_structure_hash,
)


def _extracts(files: dict[str, bytes]) -> tuple[dict, dict]:
    """Build ``(extracts, file_owner)`` for one ``db`` subsystem from inline source by extension."""
    extracts = {}
    file_owner = {}
    for rel, src in files.items():
        extracts[rel] = get_adapter(rel).extract(rel, src)
        file_owner[rel] = "db"
    return extracts, file_owner


def _tables(files: dict[str, bytes]) -> dict[str, list[str]]:
    extracts, file_owner = _extracts(files)
    return {t["name"]: t["columns"] for t in schema_catalog("db", extracts, file_owner)}


# ---- the full migration fold ----
def test_fold_create_add_drop_rename_to_final_set():
    tables = _tables({
        "001_create.sql": b"CREATE TABLE users (id int, email text, tmp text);",
        "002_add.sql": b"ALTER TABLE users ADD COLUMN name text;",
        "003_drop_col.sql": b"ALTER TABLE users DROP COLUMN tmp;",
        "004_rename_col.sql": b"ALTER TABLE users RENAME COLUMN name TO full_name;",
        "005_rename_tbl.sql": b"ALTER TABLE users RENAME TO members;",
    })
    assert tables == {"members": ["email", "full_name", "id"]}


def test_fold_drop_table_removes_it():
    tables = _tables({
        "001.sql": b"CREATE TABLE a (id int); CREATE TABLE b (id int);",
        "002.sql": b"DROP TABLE a;",
    })
    assert set(tables) == {"b"}


def test_fold_quoted_postgres_identifiers_survive():
    tables = _tables({"001.sql": b'CREATE TABLE "Users" ("Id" int, "Full Name" text);'})
    assert tables == {"Users": ["Full Name", "Id"]}


# ---- ordering determinism ----
def test_filename_numeric_prefix_orders_beyond_lexical():
    # 10 must apply AFTER 2 (numeric order), not before (lexical) — the drop in 10 must win.
    files = {
        "2_add.sql": b"CREATE TABLE t (id int); ALTER TABLE t ADD COLUMN a int;",
        "10_drop.sql": b"ALTER TABLE t DROP COLUMN a;",
    }
    extracts, fo = _extracts(files)
    ordered, no_order = order_migrations("db", extracts, fo)
    assert [o.split("_")[0] for o in ordered] == ["2", "10"]
    assert no_order is False
    assert _tables(files) == {"t": ["id"]}  # 10's drop applied after 2's add


def test_revision_chain_orders_unprefixed_files():
    # No numeric prefix; order is driven by the down_revision chain, not the filename.
    files = {
        "b_second.sql": b"-- revision: 2\n-- down_revision: 1\nALTER TABLE t DROP COLUMN a;",
        "a_first.sql": b"-- revision: 1\nCREATE TABLE t (id int, a int);",
    }
    extracts, fo = _extracts(files)
    ordered, no_order = order_migrations("db", extracts, fo)
    assert ordered == ["a_first.sql", "b_second.sql"]
    assert no_order is False
    assert _tables(files) == {"t": ["id"]}


def test_no_deterministic_order_is_flagged():
    files = {
        "alpha.sql": b"CREATE TABLE t (id int);",
        "beta.sql": b"ALTER TABLE t DROP COLUMN id;",
    }
    extracts, fo = _extracts(files)
    _, no_order = order_migrations("db", extracts, fo)
    assert no_order is True
    codes = {c for c, _, _ in schema_diagnostics("db", extracts, fo)}
    assert "E_SCHEMA_NO_ORDER" in codes


def test_explicit_order_header_wins():
    files = {
        "z.sql": b"-- bounds:order 1\nCREATE TABLE t (id int, a int);",
        "a.sql": b"-- bounds:order 2\nALTER TABLE t DROP COLUMN a;",
    }
    assert _tables(files) == {"t": ["id"]}


# ---- fail soft ----
def test_unparsable_statement_keeps_siblings_and_reports():
    files = {"001.sql": b"CREATE TABLE a (id int); GARBAGE NONSENSE; CREATE TABLE b (x int);"}
    extracts, fo = _extracts(files)
    assert extracts["001.sql"].error is None  # not a whole-file failure
    assert set(_tables(files)) == {"a", "b"}  # both valid tables survive
    codes = {c for c, _, _ in schema_diagnostics("db", extracts, fo)}
    assert "E_SCHEMA_UNPARSED" in codes


def test_totally_unparsable_file_is_hard_error():
    res = get_adapter("x.sql").extract("x.sql", b"@@@ not sql at all @@@")
    assert res.error is not None


# ---- deterministic hash ----
def test_schema_structure_hash_is_stable_and_order_insensitive():
    a = _extracts({"001.sql": b"CREATE TABLE t (id int, a int);"})
    b = _extracts({"001.sql": b"CREATE TABLE t (a int, id int);"})  # columns reordered in DDL
    assert schema_structure_hash("db", *a) == schema_structure_hash("db", *b)
    c = _extracts({"001.sql": b"CREATE TABLE t (id int);"})  # different surface
    assert schema_structure_hash("db", *a) != schema_structure_hash("db", *c)


# ---- prisma ----
def test_prisma_models_fold_like_tables():
    tables = _tables({
        "schema.prisma": b'model User {\n  id Int @id\n  email String\n  @@map("users")\n}\n'
                         b'model Post {\n  id Int @id\n  title String @map("post_title")\n}\n'
    })
    assert tables == {"Post": ["id", "post_title"], "users": ["email", "id"]}


# ---- non-table schema objects (functions/views/indexes/triggers/types/policies/rls) ----
def test_schema_objects_surface_functions_policies_and_rls():
    extracts, file_owner = _extracts({
        "001.sql": b"create table profiles (id uuid);\n"
                   b"alter table profiles enable row level security;\n"
                   b'create policy "owner read" on profiles for select using (true);\n'
                   b"create function bump() returns trigger language plpgsql as $$ begin return new; end; $$;\n"
                   b"create view active as select * from profiles;\n"
                   b"create index idx_id on profiles (id);\n"
    })
    objects = schema_objects("db", extracts, file_owner)
    by_kind = {o["kind"]: o for o in objects}
    # tables are NOT in schema_objects (they have their own catalog)
    assert "table" not in by_kind
    assert by_kind["function"]["name"] == "bump"
    assert by_kind["view"]["name"] == "active"
    assert by_kind["index"]["table"] == "profiles"
    assert by_kind["policy"]["name"] == "profiles.owner read"
    assert by_kind["rls"]["name"] == "profiles"
    # deterministic: sorted by (kind, name)
    assert objects == sorted(objects, key=lambda o: (o["kind"], o["name"]))
