"""Content-addressable extraction cache.

The cache stores one :class:`FileRecord` per scanned source file, keyed by its
repo-relative POSIX path. Records carry both the content hash (sha256 of raw
bytes) and the structure hash (sha256 of the symbol/import shape) so the engine
can cheaply decide whether a file's *structure* changed without re-extracting.

Persistence is a single ``.bounds/state.json`` (or legacy ``.compact/state.json``)
written atomically with sorted keys and no timestamps, so the on-disk bytes are
deterministic across runs.
Loading is tolerant: a missing, corrupt, or version-mismatched file yields an
empty :class:`State` rather than an error — the engine simply re-extracts.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ..models import ExtractResult, ImportRef, Symbol


@dataclass
class FileRecord:
    """One cached file's structural fingerprint and extracted shape.

    ``symbols`` and ``imports`` are stored as plain dicts (the ``to_dict()``
    form of :class:`Symbol` / :class:`ImportRef`) so the record round-trips to
    JSON without further conversion.
    """

    path: str
    content_hash: str
    structure_hash: str
    language: str
    symbols: list[dict] = field(default_factory=list)
    imports: list[dict] = field(default_factory=list)

    def to_result(self) -> ExtractResult:
        """Rebuild the :class:`ExtractResult` this record was derived from.

        Both hashes are carried back through so the result is byte-equivalent
        to the original extraction (minus any transient ``error``, which the
        cache never persists).
        """
        return ExtractResult(
            path=self.path,
            language=self.language,
            symbols=[Symbol.from_dict(s) for s in self.symbols],
            imports=[ImportRef.from_dict(i) for i in self.imports],
            content_hash=self.content_hash,
            structure_hash=self.structure_hash,
        )

    @classmethod
    def from_result(cls, r: ExtractResult) -> "FileRecord":
        """Serialize an :class:`ExtractResult` into a cacheable record."""
        return cls(
            path=r.path,
            content_hash=r.content_hash,
            structure_hash=r.structure_hash,
            language=r.language,
            symbols=[s.to_dict() for s in r.symbols],
            imports=[i.to_dict() for i in r.imports],
        )

    def to_dict(self) -> dict:
        """Plain-dict form for JSON serialization."""
        return {
            "path": self.path,
            "content_hash": self.content_hash,
            "structure_hash": self.structure_hash,
            "language": self.language,
            "symbols": [dict(s) for s in self.symbols],
            "imports": [dict(i) for i in self.imports],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FileRecord":
        """Reconstruct a record from its JSON form, tolerating missing keys."""
        return cls(
            path=str(d.get("path", "")),
            content_hash=str(d.get("content_hash", "")),
            structure_hash=str(d.get("structure_hash", "")),
            language=str(d.get("language", "")),
            symbols=[dict(s) for s in (d.get("symbols") or [])],
            imports=[dict(i) for i in (d.get("imports") or [])],
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

    def put(self, r: ExtractResult) -> None:
        """Insert/replace the cache entry for an extraction result."""
        self.files[r.path] = FileRecord.from_result(r)

    def prune(self, live_paths: set[str]) -> None:
        """Drop cached entries whose path is no longer in ``live_paths``."""
        for path in [p for p in self.files if p not in live_paths]:
            del self.files[path]

    def to_dict(self) -> dict:
        """Stable JSON form; file keys sorted for deterministic output."""
        return {
            "version": self.version,
            "files": {
                path: self.files[path].to_dict() for path in sorted(self.files)
            },
        }

    @classmethod
    def from_dict(cls, d: dict) -> "State":
        """Reconstruct a State from its JSON form, tolerating missing keys."""
        files_data = d.get("files") or {}
        files = {
            str(path): FileRecord.from_dict(rec) for path, rec in files_data.items()
        }
        return cls(version=str(d.get("version", config.STATE_VERSION)), files=files)


def load_state(project_root: Path) -> State:
    """Read ``<project_root>/.bounds/state.json`` into a :class:`State`.

    Reads the legacy ``.compact/state.json`` instead when only that layout is present.
    Tolerant by design: a missing file, unreadable file, malformed JSON, or a
    version that does not match :data:`config.STATE_VERSION` all yield a fresh
    empty State. This never raises — a stale or broken cache simply forces a
    full re-extraction downstream.
    """
    state_path = config.config_dir(project_root) / config.STATE_FILE
    try:
        raw = state_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return State()

    if not isinstance(data, dict) or str(data.get("version", "")) != config.STATE_VERSION:
        return State()

    return State.from_dict(data)


def save_state(project_root: Path, state: State) -> None:
    """Atomically persist ``state`` to the project's config dir as ``state.json``.

    Writes into the active config directory (``.bounds/``, or legacy ``.compact/`` when
    that is the layout in use). The JSON is written with ``indent=2`` and
    ``sort_keys=True`` for byte-stable output (no timestamps anywhere). Writing goes to a
    sibling ``.tmp`` file which is then ``os.replace``d over the target, so a crash
    mid-write can never leave a half-written cache.
    """
    cfg_dir = config.config_dir(project_root)
    cfg_dir.mkdir(parents=True, exist_ok=True)

    final_path = cfg_dir / config.STATE_FILE
    tmp_path = cfg_dir / (config.STATE_FILE + ".tmp")

    tmp_path.write_text(
        json.dumps(state.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp_path, final_path)
