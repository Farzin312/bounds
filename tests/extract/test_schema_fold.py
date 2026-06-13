"""Direct unit tests for the SQL/Prisma schema fold (validate.schema).

Covers the false-negative / false-positive surface the data-boundary feature lives or dies on:
the migration fold to a final set, DROP/RENAME, deterministic ordering (filename prefix,
revision chain, explicit order, and the no-order advisory), per-statement fail-soft, quoted
identifiers, the deterministic schema hash, and Prisma model folding.
"""

from __future__ import annotations

from bounds.core.extract import get_adapter
from bounds.core.validate.schema import (
    order_migrations,
    schema_catalog,
    schema_diagnostics,
    schema_objects,
    schema_rls_posture,
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
    """The full migration fold must replay CREATE→ADD→DROP→RENAME-col→RENAME-table to the final live set — describe/validate report the current schema, not the union of every migration."""
    tables = _tables({
        "001_create.sql": b"CREATE TABLE users (id int, email text, tmp text);",
        "002_add.sql": b"ALTER TABLE users ADD COLUMN name text;",
        "003_drop_col.sql": b"ALTER TABLE users DROP COLUMN tmp;",
        "004_rename_col.sql": b"ALTER TABLE users RENAME COLUMN name TO full_name;",
        "005_rename_tbl.sql": b"ALTER TABLE users RENAME TO members;",
    })
    assert tables == {"members": ["email", "full_name", "id"]}


def test_fold_drop_table_removes_it():
    """A DROP TABLE in a later migration must remove the table from the live catalog — a dropped table left in the surface would report phantom data boundaries."""
    tables = _tables({
        "001.sql": b"CREATE TABLE a (id int); CREATE TABLE b (id int);",
        "002.sql": b"DROP TABLE a;",
    })
    assert set(tables) == {"b"}


def test_fold_quoted_postgres_identifiers_survive():
    """Double-quoted Postgres identifiers must survive with their exact case and spaces ("Full Name") — stripping quotes or normalising case would mismatch the real DB column name."""
    tables = _tables({"001.sql": b'CREATE TABLE "Users" ("Id" int, "Full Name" text);'})
    assert tables == {"Users": ["Full Name", "Id"]}


# ---- ordering determinism ----
def test_filename_numeric_prefix_orders_beyond_lexical():
    """Numeric filename prefixes must order 10 after 2 (numeric, not lexical) so 10's drop wins — lexical ordering would apply migrations out of sequence and derive the wrong final schema (determinism)."""
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
    """With no numeric prefix, the Alembic-style revision/down_revision chain drives order (a_first before b_second) so the fold is deterministic on unprefixed migrations."""
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
    """When no prefix, revision chain, or explicit order exists, the fold result is order-dependent so order_migrations must set no_order and emit E_SCHEMA_NO_ORDER (report-hard, not a silent guess)."""
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
    """An explicit `-- bounds:order N` header must override filename order so z.sql (order 1) applies before a.sql (order 2) — the human-declared order is authoritative."""
    files = {
        "z.sql": b"-- bounds:order 1\nCREATE TABLE t (id int, a int);",
        "a.sql": b"-- bounds:order 2\nALTER TABLE t DROP COLUMN a;",
    }
    assert _tables(files) == {"t": ["id"]}


# ---- fail soft ----
def test_unparsable_ddl_statement_keeps_siblings_and_reports():
    """A broken DDL statement between two valid CREATE TABLEs must not fail the whole file: both tables survive and the broken statement is reported as E_SCHEMA_UNPARSED (fail-soft, report-hard)."""
    # A broken DDL statement between two valid CREATE TABLEs: both tables survive and the
    # broken statement is reported (E_SCHEMA_UNPARSED) — it carried DDL we couldn't parse.
    files = {"001.sql": b"CREATE TABLE a (id int); ALTER TABLE a ADD COLUMN; CREATE TABLE b (x int);"}
    extracts, fo = _extracts(files)
    assert extracts["001.sql"].error is None  # not a whole-file failure
    assert set(_tables(files)) == {"a", "b"}  # both valid tables survive
    codes = {c for c, _, _ in schema_diagnostics("db", extracts, fo)}
    assert "E_SCHEMA_UNPARSED" in codes


def test_plpgsql_dollar_quoted_bodies_are_opaque_not_unparsed():
    """Valid PL/pgSQL — anonymous DO blocks, CREATE FUNCTION bodies, EXECUTE format(), RAISE — must
    never raise E_SCHEMA_UNPARSED: the $$…$$ body is opaque, the enclosing DDL shell still folds.
    These are the four real-world migration shapes the audit hit; none should warn."""
    files = {
        # DO block creating a policy conditionally (no modeled DDL leaks; no warning)
        "001_do_policy.sql": b"DO $$ BEGIN\n  CREATE POLICY p ON t FOR SELECT USING (true);\nEND $$;",
        # CREATE FUNCTION with a plpgsql body — the function still folds
        "002_function.sql": b"CREATE OR REPLACE FUNCTION refresh_contract() RETURNS trigger AS $$\n"
                            b"BEGIN RETURN NEW; END;\n$$ LANGUAGE plpgsql;",
        # DO block raising an exception with USING clauses
        "003_raise.sql": b"DO $$ BEGIN\n  RAISE EXCEPTION USING MESSAGE = 'x', ERRCODE = 'P0001';\nEND $$;",
        # DO block running dynamic ALTER via EXECUTE format()
        "004_execute.sql": b"DO $$ BEGIN\n  EXECUTE format('ALTER TABLE %I ADD COLUMN x int', 'c');\nEND $$;",
    }
    extracts, fo = _extracts(files)
    for rel in files:
        assert extracts[rel].error is None, f"{rel} hard-failed unexpectedly"
    codes = {c for c, _, _ in schema_diagnostics("db", extracts, fo)}
    assert "E_SCHEMA_UNPARSED" not in codes
    # The function shell still folded as a schema object.
    objs = {(o["kind"], o["name"]) for o in schema_objects("db", extracts, fo)}
    assert ("function", "refresh_contract") in objs


def test_non_ddl_parse_error_is_not_reported_as_schema_loss():
    """A non-DDL statement the grammar trips on (a cron.schedule call) must NOT raise E_SCHEMA_UNPARSED when all tables parsed cleanly — no DDL was lost, so flagging it is cry-wolf."""
    # A file whose tables parse cleanly but which also contains a non-DDL statement the
    # grammar trips on (a Postgres cron registration) must NOT be flagged: no DDL was lost.
    files = {"001.sql": b"CREATE TABLE a (id int);\n"
                        b"SELECT cron.schedule('job', '0 * * * *', $$ delete from a where id < 0 $$);\n"}
    extracts, fo = _extracts(files)
    assert extracts["001.sql"].error is None
    assert set(_tables(files)) == {"a"}
    codes = {c for c, _, _ in schema_diagnostics("db", extracts, fo)}
    assert "E_SCHEMA_UNPARSED" not in codes


def test_data_only_file_is_not_a_failure():
    """A pure seed (INSERT) or grant (GRANT/REVOKE) file has no schema DDL: it must extract to empty symbols with NO error so describe/validate never mislabel it as unparsed (the cry-wolf fix)."""
    # A pure seed / grant file has no schema DDL: it extracts to empty symbols and NO error,
    # so describe/validate never mislabel it as "unparsed" (the cry-wolf bug this fixes).
    for src in (b"INSERT INTO t (id) VALUES (1) ON CONFLICT DO NOTHING;\n",
                b"GRANT SELECT ON t TO anon;\nREVOKE ALL ON t FROM public;\n"):
        res = get_adapter("x.sql").extract("x.sql", src)
        assert res.error is None
        assert not [s for s in res.symbols if s.kind not in ("schema_meta",)]


def test_unparsable_ddl_file_is_hard_error():
    """A file that intends DDL (mentions CREATE TABLE) but is wholly unparsable is a hard error with no symbols — real schema was lost, so it surfaces loudly, never as a silent partial (report-hard)."""
    # A file that *means* to carry DDL (it mentions CREATE TABLE) but is wholly unparsable is
    # a hard extraction failure — real schema was lost, surfaced loudly (not a partial result).
    res = get_adapter("x.sql").extract("x.sql", b"CREATE TABLE @#$%^&*( totally broken !!!")
    assert res.error is not None
    assert not res.symbols


def test_no_ddl_garbage_is_not_a_failure():
    """A .sql file with no DDL intent and no revision header must not hard-fail (no error, no symbols) — only DDL-bearing or revision-headered files can fail; the counterpart to the cry-wolf fix."""
    # A .sql file with no DDL intent and no migration header contributes nothing to the schema:
    # Bounds (a schema tool) has no opinion on it and never hard-fails it — only DDL-bearing or
    # revision-headered files can fail. This is the counterpart to the cry-wolf fix.
    res = get_adapter("x.sql").extract("x.sql", b"@@@ not sql at all @@@")
    assert res.error is None
    assert not res.symbols


def test_create_schema_only_migration_is_not_a_failure():
    """A migration that only runs CREATE SCHEMA / CREATE EXTENSION (real DDL Bounds doesn't model as tables) must not hard-fail or emit schema_error — it parsed, it just yields no table surface."""
    # A valid migration that only does CREATE SCHEMA / CREATE EXTENSION (real DDL Bounds does
    # not model into a table surface) must NOT hard-fail — it parsed, it just has no tables.
    for src in (b"CREATE SCHEMA IF NOT EXISTS private;\n",
                b"CREATE EXTENSION IF NOT EXISTS pg_trgm;\n"):
        res = get_adapter("x.sql").extract("x.sql", src)
        assert res.error is None, src
        assert not [s for s in res.symbols if s.kind == "schema_error"]


# ---- deterministic hash ----
def test_schema_structure_hash_is_stable_and_order_insensitive():
    """schema_structure_hash must be identical when only column DDL order differs (sorted at the boundary) but differ when the column set changes — a deterministic, byte-stable schema fingerprint."""
    a = _extracts({"001.sql": b"CREATE TABLE t (id int, a int);"})
    b = _extracts({"001.sql": b"CREATE TABLE t (a int, id int);"})  # columns reordered in DDL
    assert schema_structure_hash("db", *a) == schema_structure_hash("db", *b)
    c = _extracts({"001.sql": b"CREATE TABLE t (id int);"})  # different surface
    assert schema_structure_hash("db", *a) != schema_structure_hash("db", *c)


# ---- prisma ----
def test_prisma_models_fold_like_tables():
    """Prisma models must flow through the same schema_catalog fold as SQL tables, honoring @@map/@map — one catalog path serves both source kinds so describe/validate never disagree by language."""
    tables = _tables({
        "schema.prisma": b'model User {\n  id Int @id\n  email String\n  @@map("users")\n}\n'
                         b'model Post {\n  id Int @id\n  title String @map("post_title")\n}\n'
    })
    assert tables == {"Post": ["id", "post_title"], "users": ["email", "id"]}


# ---- non-table schema objects (functions/views/indexes/triggers/types/policies/rls) ----
def test_schema_objects_surface_functions_policies_and_rls():
    """schema_objects must surface functions/views/indexes/policies/rls (tables excluded — they have their own catalog), sorted by (kind, name) for deterministic, byte-stable output."""
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


# ---- policy / RLS fold (drops net out, like tables) ----
def test_policy_dropped_in_a_later_migration_is_not_live():
    """A policy created then DROPped in a later migration must net out of the live surface — like tables, policies fold to their final state, not the union of every CREATE."""
    extracts, fo = _extracts({
        "001.sql": b"create table t (id int);\ncreate policy p on t for all using (true);\n",
        "002.sql": b"drop policy if exists p on t;\n",
    })
    policies = {o["name"] for o in schema_objects("db", extracts, fo) if o["kind"] == "policy"}
    assert policies == set()  # created then dropped → not in the live surface


def test_policy_dropped_then_recreated_in_one_file_is_live():
    """The idempotent re-run pattern (DROP IF EXISTS then CREATE in one file) must leave the policy LIVE — order within the file matters, and the final CREATE wins."""
    # The idempotent re-run pattern: DROP IF EXISTS then CREATE in the same file → present.
    extracts, fo = _extracts({
        "001.sql": b"create table t (id int);\n"
                   b"drop policy if exists p on t;\ncreate policy p on t for all using (true);\n",
    })
    policies = {o["name"] for o in schema_objects("db", extracts, fo) if o["kind"] == "policy"}
    assert policies == {"t.p"}


def test_rls_enabled_then_disabled_nets_out():
    """RLS enabled then disabled across migrations must net out (no live rls object) — the posture reflects the final state, so a later DISABLE removes the table from the protected set."""
    extracts, fo = _extracts({
        "001.sql": b"create table t (id int);\nalter table t enable row level security;\n",
        "002.sql": b"alter table t disable row level security;\n",
    })
    rls = {o["name"] for o in schema_objects("db", extracts, fo) if o["kind"] == "rls"}
    assert rls == set()


# ---- RLS security posture ----
def test_rls_posture_classifies_tables():
    """RLS posture must classify each table into protected (RLS + policy), rls_without_policy (locked, a security gap), and unprotected (no RLS) — this is the data-boundary security signal validate surfaces."""
    extracts, fo = _extracts({
        "001.sql": b"create table protected (id int);\n"
                   b"create table locked (id int);\n"
                   b"create table open (id int);\n"
                   b"alter table protected enable row level security;\n"
                   b"create policy r on protected for select using (true);\n"
                   b"alter table locked enable row level security;\n",  # RLS but no policy
    })
    catalog = schema_catalog("db", extracts, fo)
    posture = schema_rls_posture("db", extracts, fo, catalog)
    assert posture["protected"] == 1 and posture["protected_tables"] == ["protected"]
    assert posture["rls_without_policy_tables"] == ["locked"]
    assert posture["unprotected_tables"] == ["open"]
    assert posture["tables"] == 3 and posture["rls_enabled"] == 2


def test_unordered_policy_lifecycle_flags_no_order():
    """An unordered migration set containing a DROP/ALTER POLICY (whose fold result depends on order) must surface E_SCHEMA_NO_ORDER — policy/RLS folding has the same order-determinism requirement as tables."""
    # Policies/RLS fold, so an unordered set with a DROP/ALTER POLICY (or DISABLE RLS) and no
    # deterministic order basis must surface E_SCHEMA_NO_ORDER — the fold result depends on order.
    extracts, fo = _extracts({
        "a.sql": b"drop policy p on t;\n",
        "b.sql": b"create table t (id int);\ncreate policy p on t for all using (true);\n",
    })
    codes = {c for c, _, _ in schema_diagnostics("db", extracts, fo)}
    assert "E_SCHEMA_NO_ORDER" in codes


def test_no_force_rls_keeps_rls_enabled():
    """NO FORCE ROW LEVEL SECURITY clears only the owner-bypass exemption, so RLS stays enabled — it must not be misread as a disable that drops the table from the protected set."""
    # NO FORCE clears only the owner-bypass exemption; RLS stays enabled.
    extracts, fo = _extracts({
        "001.sql": b"create table t (id int);\n"
                   b"alter table t enable row level security;\n"
                   b"alter table t no force row level security;\n",
    })
    rls = {o["name"] for o in schema_objects("db", extracts, fo) if o["kind"] == "rls"}
    assert rls == {"t"}  # still enabled, not dropped


def test_rls_posture_empty_when_schema_has_no_rls():
    """A schema that never uses RLS (plain SQL or Prisma) must return an empty posture, not "all unprotected" — reporting a posture for a non-RLS schema would be a false security alarm."""
    # A schema that never uses RLS (here: plain tables) has no posture to report — emitting
    # "all unprotected" would be a false alarm. Same for a Prisma schema.
    extracts, fo = _extracts({"001.sql": b"create table t (id int);\n"})
    assert schema_rls_posture("db", extracts, fo, schema_catalog("db", extracts, fo)) == {}
    pex, pfo = _extracts({"s.prisma": b'model User {\n id Int @id\n @@map("users")\n}\n'})
    assert schema_rls_posture("db", pex, pfo, schema_catalog("db", pex, pfo)) == {}
