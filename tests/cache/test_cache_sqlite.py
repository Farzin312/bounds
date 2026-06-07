"""Binary SQLite cache + context armor + migration."""

from __future__ import annotations

import json
import sqlite3

from bounds.shared import config
from bounds.shared.cache import store
from bounds.shared.models import ExtractResult, ImportRef, Symbol


def _result(path: str, *, generated: bool = False) -> ExtractResult:
    return ExtractResult(
        path=path,
        language="python",
        symbols=[Symbol(name="login", kind="function", line=1, exported=True)],
        imports=[ImportRef(module="os", names=["getpid"], line=1)],
        content_hash="c" * 64,
        structure_hash="s" * 64,
        generated=generated,
    )


def _init_bounds(tmp_path):
    (tmp_path / config.BOUNDS_DIR).mkdir()


def test_save_creates_binary_sqlite_not_json(tmp_path):
    """Cache armor: cache.db must start with the SQLite magic, not JSON, so an agent can't cat it into context as tokens."""
    _init_bounds(tmp_path)
    state = store.State()
    state.put(_result("src/auth/login.py"), subsystem="auth")
    store.save_state(tmp_path, state)

    db = tmp_path / config.BOUNDS_DIR / config.CACHE_FILE
    assert db.is_file()
    # Context armor: the cache begins with the SQLite magic, not parseable JSON/text.
    with db.open("rb") as fh:
        assert fh.read(16) == b"SQLite format 3\x00"


def test_roundtrip_preserves_records_and_owner(tmp_path):
    """A save/load round-trip must preserve subsystem owner, content_hash, exported symbols, and imports, or the cache is lossy."""
    _init_bounds(tmp_path)
    state = store.State()
    state.put(_result("src/auth/login.py"), subsystem="auth")
    store.save_state(tmp_path, state)

    loaded = store.load_state(tmp_path)
    rec = loaded.get("src/auth/login.py")
    assert rec is not None
    assert rec.subsystem == "auth"
    assert rec.content_hash == "c" * 64
    result = rec.to_result()
    assert result.exported_names() == {"login"}
    assert result.imports[0].module == "os"
    assert result.generated is False


def test_roundtrip_preserves_generated_flag(tmp_path):
    """BOUNDS-021: cache generated-file state so quick validation does not reread source to skip generated exports."""
    _init_bounds(tmp_path)
    state = store.State()
    state.put(_result("src/auth/generated.py", generated=True), subsystem="auth")
    store.save_state(tmp_path, state)

    rec = store.load_state(tmp_path).get("src/auth/generated.py")
    assert rec is not None
    assert rec.generated is True
    assert rec.to_result().generated is True

    partial = store.load_subsystem_records(tmp_path, "auth")
    assert [r.generated for r in partial] == [True]


def test_save_state_migrates_v3_sqlite_schema_before_generated_insert(tmp_path):
    """A v3 cache.db without the generated column must be ALTERed before v4 inserts, or upgrades leave the cache unwritable."""
    _init_bounds(tmp_path)
    db = tmp_path / config.BOUNDS_DIR / config.CACHE_FILE
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE cache (
                path TEXT PRIMARY KEY,
                subsystem TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL,
                structure_hash TEXT NOT NULL,
                language TEXT NOT NULL,
                symbols TEXT,
                imports TEXT,
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX idx_cache_subsystem ON cache(subsystem);
            PRAGMA user_version = 3;
            """
        )
    finally:
        conn.close()

    state = store.State()
    state.put(_result("src/auth/generated.py", generated=True), subsystem="auth")
    store.save_state(tmp_path, state)

    loaded = store.load_state(tmp_path)
    rec = loaded.get("src/auth/generated.py")
    assert rec is not None
    assert rec.generated is True


def test_partial_read_by_subsystem(tmp_path):
    """Partial reads must return only the requested subsystem's records (and [] for unknown), backing token-lean targeted retrieval."""
    _init_bounds(tmp_path)
    state = store.State()
    state.put(_result("src/auth/a.py"), subsystem="auth")
    state.put(_result("src/billing/b.py"), subsystem="billing")
    store.save_state(tmp_path, state)

    auth = store.load_subsystem_records(tmp_path, "auth")
    assert [r.path for r in auth] == ["src/auth/a.py"]
    assert store.load_subsystem_records(tmp_path, "missing") == []


def test_migration_from_legacy_json(tmp_path):
    """load_state must transparently read a legacy JSON cache, and cache --migrate must convert+remove it idempotently (second run is a no-op)."""
    _init_bounds(tmp_path)
    cfg = tmp_path / config.BOUNDS_DIR
    legacy = {
        "version": config.STATE_VERSION,
        "files": {"src/x.py": _result("src/x.py").to_dict() | {"subsystem": ""}},
    }
    (cfg / config.STATE_FILE).write_text(json.dumps(legacy), encoding="utf-8")

    # load_state transparently reads the legacy JSON...
    loaded = store.load_state(tmp_path)
    assert loaded.get("src/x.py") is not None

    # ...and `cache --migrate` converts + removes it.
    report = store.migrate_json_to_sqlite(tmp_path)
    assert report["migrated"] is True
    assert report["files"] == 1
    # save_state now removes the orphan json itself; removed_json must still reflect that truthfully.
    assert report["removed_json"] is True
    assert not (cfg / config.STATE_FILE).exists()
    assert (cfg / config.CACHE_FILE).is_file()
    # A second migrate is a no-op.
    assert store.migrate_json_to_sqlite(tmp_path)["migrated"] is False


def test_save_state_removes_orphan_legacy_json(tmp_path):
    """save_state must self-complete the migration: after writing cache.db it removes a sibling
    state.json so the shadowed, version-gated orphan never lingers as dead weight."""
    _init_bounds(tmp_path)
    cfg = tmp_path / config.BOUNDS_DIR
    legacy = cfg / config.STATE_FILE
    legacy.write_text(json.dumps({"version": config.STATE_VERSION, "files": {}}), encoding="utf-8")

    state = store.State()
    state.put(_result("src/auth/login.py"), subsystem="auth")
    store.save_state(tmp_path, state)

    # cache.db is now authoritative; the orphan state.json is gone.
    assert (cfg / config.CACHE_FILE).is_file()
    assert not legacy.exists()


def test_save_state_no_legacy_json_is_noop(tmp_path):
    """When there is no sibling state.json, the cleanup is a fail-soft no-op (missing file never raises)."""
    _init_bounds(tmp_path)
    cfg = tmp_path / config.BOUNDS_DIR
    assert not (cfg / config.STATE_FILE).exists()

    state = store.State()
    state.put(_result("src/auth/login.py"), subsystem="auth")
    store.save_state(tmp_path, state)  # must not raise

    assert (cfg / config.CACHE_FILE).is_file()
    assert not (cfg / config.STATE_FILE).exists()


def test_prune_drops_missing_files(tmp_path):
    """prune_missing must drop cache records whose source file no longer exists on disk and keep the live ones, preventing stale entries."""
    _init_bounds(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "live.py").write_text("x = 1", encoding="utf-8")
    state = store.State()
    state.put(_result("src/live.py"), subsystem="auth")
    state.put(_result("src/gone.py"), subsystem="auth")
    store.save_state(tmp_path, state)

    report = store.prune_missing(tmp_path)
    assert report["pruned"] == 1
    assert report["remaining"] == 1
    assert store.load_state(tmp_path).get("src/gone.py") is None


def test_inspect_is_counts_only(tmp_path):
    """inspect must report counts only (backend/files/by_subsystem) and never leak symbols/imports, keeping the summary token-lean."""
    _init_bounds(tmp_path)
    state = store.State()
    state.put(_result("src/auth/a.py"), subsystem="auth")
    store.save_state(tmp_path, state)

    summary = store.inspect(tmp_path)
    assert summary["backend"] == "sqlite"
    assert summary["files"] == 1
    assert summary["by_subsystem"] == {"auth": 1}
    # token-lean: no symbols/imports leaked into the summary
    assert "symbols" not in json.dumps(summary)


def test_corrupt_cache_yields_empty_state(tmp_path):
    """Fail soft: a corrupt cache.db must load as an empty state, never crash, so a bad cache degrades to a cold re-extract."""
    _init_bounds(tmp_path)
    (tmp_path / config.BOUNDS_DIR / config.CACHE_FILE).write_bytes(b"not a database")
    assert store.load_state(tmp_path).files == {}


def test_partial_read_rejects_stale_schema_version(tmp_path):
    """Partial and full reads must apply the same schema-version gate, so a stale-version cache reads empty on both paths, never disagreeing."""
    # The per-subsystem partial read must apply the same schema-version gate as the full
    # read, so the two paths never disagree on whether a stale cache is valid.
    import sqlite3

    _init_bounds(tmp_path)
    state = store.State()
    state.put(_result("src/auth/a.py"), subsystem="auth")
    store.save_state(tmp_path, state)

    # Sanity: a current-schema cache reads back.
    assert [r.path for r in store.load_subsystem_records(tmp_path, "auth")] == ["src/auth/a.py"]

    # Bump the on-disk schema version past what this build accepts.
    db = tmp_path / config.BOUNDS_DIR / config.CACHE_FILE
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {store._schema_version() + 1}")
    conn.commit()
    conn.close()

    assert store.load_subsystem_records(tmp_path, "auth") == []
    assert store.load_state(tmp_path).files == {}  # full read agrees
