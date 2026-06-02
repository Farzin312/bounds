"""Content-addressable extraction cache — binary SQLite (context armor).

The cache stores one :class:`FileRecord` per scanned source file, keyed by its
repo-relative POSIX path. Records carry both the content hash (sha256 of raw
bytes) and the structure hash (sha256 of the symbol/import shape) so the engine
can cheaply decide whether a file's *structure* changed without re-extracting.

**Why SQLite, not JSON ("context armor"):** the cache is the one Bounds
artifact that can grow large. Stored as a binary ``.bounds/cache.db``, an agent
that naively ``cat``s it gets gibberish instead of a parseable token blob dumped
into its context — the structural defense against accidental context burn. The
human-editable manifests stay YAML; only the *derived* cache is binary.

**Partial reads (cache scalability):** the ``subsystem`` column + index let
callers load just one subsystem's records (``SELECT ... WHERE subsystem = ?``)
instead of the whole cache — see :func:`load_subsystem_records`.

**Migration:** a legacy ``state.json`` is read transparently on load and rewritten
as ``cache.db`` on the next save (or eagerly via ``bounds cache --migrate``).
Detection is by magic bytes (``SQLite format 3\\0``), matching the spec.

Loading is tolerant: a missing, corrupt, or version-mismatched cache yields an
empty :class:`State` rather than an error — the engine simply re-extracts.
Determinism note: the ``updated_at`` column exists for forward-compatibility but
is written empty — Bounds never puts wall-clock into any persisted artifact.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ..models import ExtractResult, ImportRef, Symbol

_SQLITE_MAGIC = b"SQLite format 3\x00"

__all__ = [
    "FileRecord",
    "State",
    "inspect",
    "load_state",
    "migrate_json_to_sqlite",
    "prune_missing",
    "save_state",
]

# Milliseconds a cache connection waits on a locked database before erroring. Two `bounds
# validate` runs racing on the same `.bounds/cache.db` should queue briefly, not immediately raise
# "database is locked"; the writer is a single short transaction, so a few seconds is ample.
_BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    path TEXT PRIMARY KEY,
    subsystem TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    structure_hash TEXT NOT NULL,
    language TEXT NOT NULL,
    symbols TEXT,
    imports TEXT,
    generated INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cache_subsystem ON cache(subsystem);
"""


@dataclass
class FileRecord:
    """One cached file's structural fingerprint and extracted shape.

    ``symbols`` and ``imports`` are stored as plain dicts (the ``to_dict()`` form of
    :class:`Symbol` / :class:`ImportRef`) so the record round-trips without further
    conversion. ``subsystem`` is the owning subsystem name (used for partial reads).
    """

    path: str
    content_hash: str
    structure_hash: str
    language: str
    symbols: list[dict] = field(default_factory=list)
    imports: list[dict] = field(default_factory=list)
    subsystem: str = ""
    generated: bool = False

    def to_result(self) -> ExtractResult:
        """Rebuild the :class:`ExtractResult` this record was derived from."""
        return ExtractResult(
            path=self.path,
            language=self.language,
            symbols=[Symbol.from_dict(s) for s in self.symbols],
            imports=[ImportRef.from_dict(i) for i in self.imports],
            content_hash=self.content_hash,
            structure_hash=self.structure_hash,
            generated=self.generated,
        )

    @classmethod
    def from_result(cls, r: ExtractResult, subsystem: str = "") -> "FileRecord":
        """Serialize an :class:`ExtractResult` into a cacheable record."""
        return cls(
            path=r.path,
            content_hash=r.content_hash,
            structure_hash=r.structure_hash,
            language=r.language,
            symbols=[s.to_dict() for s in r.symbols],
            imports=[i.to_dict() for i in r.imports],
            subsystem=subsystem,
            generated=r.generated,
        )

    def to_dict(self) -> dict:
        """Plain-dict form (used by the JSON migration path and ``cache --inspect``)."""
        return {
            "path": self.path,
            "subsystem": self.subsystem,
            "content_hash": self.content_hash,
            "structure_hash": self.structure_hash,
            "language": self.language,
            "symbols": [dict(s) for s in self.symbols],
            "imports": [dict(i) for i in self.imports],
            "generated": self.generated,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FileRecord":
        """Reconstruct a record from its dict form, tolerating missing keys."""
        return cls(
            path=str(d.get("path", "")),
            content_hash=str(d.get("content_hash", "")),
            structure_hash=str(d.get("structure_hash", "")),
            language=str(d.get("language", "")),
            symbols=[dict(s) for s in (d.get("symbols") or [])],
            imports=[dict(i) for i in (d.get("imports") or [])],
            subsystem=str(d.get("subsystem", "")),
            generated=bool(d.get("generated", False)),
        )


class State:
    """The full extraction cache: a versioned map of path -> :class:`FileRecord`."""

    def __init__(
        self,
        version: str = config.STATE_VERSION,
        files: dict[str, FileRecord] | None = None,
    ) -> None:
        self.version = version
        self.files: dict[str, FileRecord] = files if files is not None else {}

    def get(self, path: str) -> FileRecord | None:
        """Return the cached record for ``path`` (repo-relative POSIX), or None."""
        return self.files.get(path)

    def put(self, r: ExtractResult, subsystem: str = "") -> None:
        """Insert/replace the cache entry for an extraction result (with its owner)."""
        self.files[r.path] = FileRecord.from_result(r, subsystem)

    def prune(self, live_paths: set[str]) -> None:
        """Drop cached entries whose path is no longer in ``live_paths``."""
        for path in [p for p in self.files if p not in live_paths]:
            del self.files[path]

    def to_dict(self) -> dict:
        """Stable dict form; file keys sorted for deterministic output."""
        return {
            "version": self.version,
            "files": {path: self.files[path].to_dict() for path in sorted(self.files)},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "State":
        """Reconstruct a State from its dict form, tolerating missing keys."""
        files_data = d.get("files") or {}
        files = {str(path): FileRecord.from_dict(rec) for path, rec in files_data.items()}
        return cls(version=str(d.get("version", config.STATE_VERSION)), files=files)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def _cache_path(project_root: Path) -> Path:
    return config.config_dir(project_root) / config.CACHE_FILE


def _legacy_json_path(project_root: Path) -> Path:
    return config.config_dir(project_root) / config.STATE_FILE


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(16) == _SQLITE_MAGIC
    except OSError:
        return False


def load_state(project_root: Path) -> State:
    """Read the project's extraction cache into a :class:`State`.

    Resolution: a binary ``cache.db`` is read via SQLite; otherwise a
    legacy ``state.json`` is read (auto-migration — the next ``save_state`` writes ``cache.db``).
    Tolerant by design: a missing/unreadable/corrupt/version-mismatched cache yields a fresh
    empty State rather than raising, so a broken cache simply forces full re-extraction.
    """
    db_path = _cache_path(project_root)
    if db_path.is_file() and _is_sqlite(db_path):
        return _load_sqlite(db_path)
    json_path = _legacy_json_path(project_root)
    if json_path.is_file():
        return _load_json(json_path)
    return State()


def _schema_version() -> int:
    """The cache schema version as an int for ``PRAGMA user_version`` (0 if non-numeric)."""
    try:
        return int(config.STATE_VERSION)
    except (TypeError, ValueError):
        return 0


def _load_sqlite(db_path: Path) -> State:
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error:
        return State()
    try:
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        if conn.execute("PRAGMA user_version").fetchone()[0] != _schema_version():
            return State()
        rows = conn.execute(
            "SELECT path, subsystem, content_hash, structure_hash, language, symbols, imports, generated FROM cache"
        ).fetchall()
    except sqlite3.Error:
        return State()
    finally:
        conn.close()

    files: dict[str, FileRecord] = {}
    for path, subsystem, chash, shash, lang, symbols, imports, generated in rows:
        files[path] = FileRecord(
            path=path,
            content_hash=chash,
            structure_hash=shash,
            language=lang,
            symbols=_loads(symbols),
            imports=_loads(imports),
            subsystem=subsystem or "",
            generated=bool(generated),
        )
    return State(version=config.STATE_VERSION, files=files)


def _load_json(json_path: Path) -> State:
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return State()
    if not isinstance(data, dict) or str(data.get("version", "")) != config.STATE_VERSION:
        return State()
    return State.from_dict(data)


def _loads(blob) -> list[dict]:
    try:
        out = json.loads(blob) if blob else []
    except (TypeError, ValueError):
        return []
    return [dict(x) for x in out] if isinstance(out, list) else []


def save_state(project_root: Path, state: State) -> None:
    """Atomically persist ``state`` to ``.bounds/cache.db`` (binary SQLite).

    The whole cache is rewritten inside a single transaction, so a crash mid-write rolls
    back cleanly (no half-written cache). Rows are inserted in sorted path order for stable
    iteration. ``updated_at`` is written empty — Bounds never persists wall-clock.
    """
    cfg_dir = config.config_dir(project_root)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    db_path = cfg_dir / config.CACHE_FILE

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {_schema_version()}")
        with conn:  # transaction: commit on success, rollback on error
            conn.execute("DELETE FROM cache")
            conn.executemany(
                "INSERT INTO cache (path, subsystem, content_hash, structure_hash, language, "
                "symbols, imports, generated, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')",
                [
                    (
                        rec.path,
                        rec.subsystem,
                        rec.content_hash,
                        rec.structure_hash,
                        rec.language,
                        json.dumps(rec.symbols, sort_keys=True),
                        json.dumps(rec.imports, sort_keys=True),
                        1 if rec.generated else 0,
                    )
                    for path in sorted(state.files)
                    for rec in (state.files[path],)
                ],
            )
    finally:
        conn.close()

    # The write succeeded, so cache.db is now authoritative. Self-complete the migration by
    # removing any lingering legacy state.json: it is already shadowed (load_state prefers
    # cache.db) and version-gated out, so it is dead weight. Fail-soft — never raise from cleanup.
    legacy = _legacy_json_path(project_root)
    try:
        legacy.unlink(missing_ok=True)
    except OSError:
        pass


def load_subsystem_records(project_root: Path, subsystem: str) -> list[FileRecord]:
    """Partial read: the cached records for one subsystem, via the subsystem index.

    Returns ``[]`` when there is no SQLite cache yet. This is the per-subsystem read path
    that keeps an agent/tool from loading the whole cache to inspect one boundary.
    """
    db_path = _cache_path(project_root)
    if not (db_path.is_file() and _is_sqlite(db_path)):
        return []
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error:
        return []
    try:
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        # Same schema-version gate as the full read (_load_sqlite): a stale-schema cache is
        # discarded, not partially served, so both read paths agree on what's valid.
        if conn.execute("PRAGMA user_version").fetchone()[0] != _schema_version():
            return []
        rows = conn.execute(
            "SELECT path, subsystem, content_hash, structure_hash, language, symbols, imports, generated "
            "FROM cache WHERE subsystem = ? ORDER BY path",
            (subsystem,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [
        FileRecord(
            path=path,
            content_hash=chash,
            structure_hash=shash,
            language=lang,
            symbols=_loads(symbols),
            imports=_loads(imports),
            subsystem=subsystem or "",
            generated=bool(generated),
        )
        for path, subsystem, chash, shash, lang, symbols, imports, generated in rows
    ]


def migrate_json_to_sqlite(project_root: Path) -> dict:
    """Eagerly convert a legacy ``state.json`` to ``cache.db`` (``bounds cache --migrate``).

    Returns a small report ``{migrated, files, removed_json, note}``. A no-op (already SQLite,
    or no cache at all) reports ``migrated: false`` with an explanatory note.
    """
    db_path = _cache_path(project_root)
    json_path = _legacy_json_path(project_root)
    if db_path.is_file() and _is_sqlite(db_path):
        return {"migrated": False, "files": 0, "removed_json": False, "note": "cache is already SQLite"}
    if not json_path.is_file():
        return {"migrated": False, "files": 0, "removed_json": False, "note": "no state.json to migrate"}

    state = _load_json(json_path)
    save_state(project_root, state)
    # save_state self-completes the migration by removing the orphan state.json, so reflect the
    # real on-disk outcome rather than relying on a redundant unlink here (which would now miss).
    removed = not json_path.exists()
    return {
        "migrated": True,
        "files": len(state.files),
        "removed_json": removed,
        "note": f"migrated {len(state.files)} file record(s) to {config.CACHE_FILE}",
    }


def prune_missing(project_root: Path) -> dict:
    """Drop cache rows whose source file no longer exists (``bounds cache --prune``).

    Returns ``{pruned, remaining}``. Operates on the on-disk SQLite cache directly.
    """
    state = load_state(project_root)
    before = len(state.files)
    live = {p for p in state.files if (Path(project_root) / p).is_file()}
    state.prune(live)
    save_state(project_root, state)
    return {"pruned": before - len(state.files), "remaining": len(state.files)}


def inspect(project_root: Path) -> dict:
    """Token-lean cache summary (``bounds cache --inspect``) — counts, never symbol dumps.

    Reports the backend (sqlite/json/none), total files, and per-subsystem / per-language
    counts. Deliberately omits symbols/imports so inspecting the cache stays cheap and never
    becomes the very context blob the binary format is meant to prevent.
    """
    db_path = _cache_path(project_root)
    json_path = _legacy_json_path(project_root)
    if db_path.is_file() and _is_sqlite(db_path):
        backend = "sqlite"
    elif json_path.is_file():
        backend = "json (legacy — run `bounds cache --migrate`)"
    else:
        return {"backend": "none", "files": 0, "by_subsystem": {}, "by_language": {}}

    state = load_state(project_root)
    by_subsystem: dict[str, int] = {}
    by_language: dict[str, int] = {}
    for rec in state.files.values():
        by_subsystem[rec.subsystem or "(unowned)"] = by_subsystem.get(rec.subsystem or "(unowned)", 0) + 1
        by_language[rec.language] = by_language.get(rec.language, 0) + 1
    return {
        "backend": backend,
        "files": len(state.files),
        "by_subsystem": dict(sorted(by_subsystem.items())),
        "by_language": dict(sorted(by_language.items())),
    }
