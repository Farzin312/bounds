"""Language-adapter base class and deterministic hashing helpers.

An adapter turns the bytes of one source file into an :class:`ExtractResult`:
the file's top-level exported symbols and its import references. Extraction is
deterministic and body-insensitive at the structure-hash level, so comment- or
body-only edits do not invalidate downstream consumers.

Two hashes are produced per file:
  * ``content_hash``   -- sha256 of the raw bytes; decides cache validity.
  * ``structure_hash`` -- sha256 of the canonical interface surface (sorted
                          symbol kind:name lines + sorted import modules);
                          decides whether change must propagate to consumers.

Both hashes avoid timestamps, randomness and wall-clock so output is byte-stable.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Iterable

import json

from .. import errors
from ..models import ExtractResult, ImportRef, Issue, Symbol


class LanguageAdapter(ABC):
    """Extracts exported symbols and imports from one language's source files."""

    language_name: str
    extensions: tuple[str, ...]
    # Human-readable statement of the self-consistency invariant an adapter's output
    # must satisfy, surfaced in docs/help. Empty when the adapter declares no contract.
    contract_description: str = ""

    @abstractmethod
    def extract(self, rel_path: str, source: bytes) -> ExtractResult:
        """Parse ``source`` (raw bytes) and return its :class:`ExtractResult`.

        ``rel_path`` is a repo-relative POSIX path, stored verbatim on the result.
        Implementations must fail soft: a parse failure becomes an ``error`` on
        the result, never a raised exception.
        """
        raise NotImplementedError

    def check_contract(self, result: ExtractResult) -> list[Issue]:
        """Return Issues when this adapter's *own output* violates its declared
        self-consistency contract — a deterministic, zero-LLM regression guard.

        The check reads only an already-built :class:`ExtractResult` (never the
        filesystem or tree-sitter), so it is a pure function of the adapter's output
        and safe to run inside the validation checks. Base implementation declares no
        contract and returns no issues; adapters with invariants override this and
        build issues via :meth:`_contract_issue`.
        """
        return []

    def _contract_issue(self, message: str, file: str | None, fix: str | None = None) -> Issue:
        """Build an ``E_ADAPTER_CONTRACT`` Issue with the code's canonical severity.

        Centralises construction so every adapter's contract violation carries the
        same code/severity (``errors.SEVERITY`` is the single home for the latter).
        """
        return Issue(
            errors.E_ADAPTER_CONTRACT,
            errors.SEVERITY[errors.E_ADAPTER_CONTRACT],
            message,
            file=file,
            fix=fix,
        )


def content_hash(source: bytes) -> str:
    """Return the sha256 hex digest of the raw file bytes."""
    return hashlib.sha256(source).hexdigest()


def canonical_columns(names: Iterable[str]) -> list[str]:
    """Deduplicated, sorted column/field names for a schema symbol's ``metadata["columns"]``.

    Single home so every schema-bearing adapter (SQL, Prisma, …) canonicalizes a table's
    columns identically — same order, same dedup — which keeps the structure hash stable.
    """
    return sorted(set(names))


def structure_hash(symbols: list[Symbol], imports: list[ImportRef]) -> str:
    """Hash the canonical interface surface of a file.

    The canonical string is the newline-join of, in order:
      * sorted ``"sym:{kind}:{name}"`` lines, one per symbol, and
      * sorted ``"imp:{module}"`` lines, one per import module.

    Deterministic and body-insensitive: reordering declarations or editing
    function bodies does not change the digest.
    """
    sym_lines = sorted(
        f"sym:{s.kind}:{s.name}:{json.dumps(s.metadata, sort_keys=True, separators=(',', ':'))}"
        for s in symbols
    )
    imp_lines = sorted(f"imp:{i.module}" for i in imports)
    canonical = "\n".join(sym_lines + imp_lines)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def make_result(
    rel_path: str,
    language: str,
    symbols: list[Symbol],
    imports: list[ImportRef],
    source: bytes,
    error: str | None = None,
) -> ExtractResult:
    """Build an :class:`ExtractResult` with both hashes filled in.

    ``rel_path`` is stored as-is (callers pass POSIX form). On error the symbol
    and import lists are typically empty, but the hashes are still computed so
    cache identity remains well-defined.
    """
    return ExtractResult(
        path=rel_path,
        language=language,
        symbols=symbols,
        imports=imports,
        content_hash=content_hash(source),
        structure_hash=structure_hash(symbols, imports),
        error=error,
    )
