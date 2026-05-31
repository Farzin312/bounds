"""Binary SQLite cache + context armor + migration."""

from __future__ import annotations

import json

from bounds import config
from bounds.cache import store
from bounds.models import ExtractResult, ImportRef, Symbol


def _result(path: str) -> ExtractResult:
    return ExtractResult(
        path=path,
        language="python",
        symbols=[Symbol(name="login", kind="function", line=1, exported=True)],
        imports=[ImportRef(module="os", names=["getpid"], line=1)],
        content_hash="c" * 64,
        structure_hash="s" * 64,
    )


def _init_bounds(tmp_path):
    (tmp_path / config.BOUNDS_DIR).mkdir()


def test_save_creates_binary_sqlite_not_json(tmp_path):
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


def test_partial_read_by_subsystem(tmp_path):
    _init_bounds(tmp_path)
    state = store.State()
    state.put(_result("src/auth/a.py"), subsystem="auth")
    state.put(_result("src/billing/b.py"), subsystem="billing")
    store.save_state(tmp_path, state)

    auth = store.load_subsystem_records(tmp_path, "auth")
    assert [r.path for r in auth] == ["src/auth/a.py"]
    assert store.load_subsystem_records(tmp_path, "missing") == []


def test_migration_from_legacy_json(tmp_path):
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
    assert not (cfg / config.STATE_FILE).exists()
    assert (cfg / config.CACHE_FILE).is_file()
    # A second migrate is a no-op.
    assert store.migrate_json_to_sqlite(tmp_path)["migrated"] is False


def test_prune_drops_missing_files(tmp_path):
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
    _init_bounds(tmp_path)
    (tmp_path / config.BOUNDS_DIR / config.CACHE_FILE).write_bytes(b"not a database")
    assert store.load_state(tmp_path).files == {}
