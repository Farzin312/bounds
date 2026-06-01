"""TypeScript ``tsconfig.json`` path-alias resolution (best-effort, deterministic).

TS/JS projects rarely import by raw relative path; they lean on ``compilerOptions.paths``
aliases (``@/common`` → ``src/common``) and ``baseUrl``-relative bare imports
(``common/types`` with ``baseUrl: "src"``). The import resolver in :mod:`bounds.validate.checks`
only understands relative (``./x``) and exact stems, so on an alias-heavy repo (a typical NestJS
or monorepo backend) most cross-subsystem edges silently vanish — the same class of undercount
as the dotted-filename bug, from a different cause.

This module loads a project's tsconfig (tolerant of JSONC comments + trailing commas, following a
chain of ``extends``) and precompiles its ``baseUrl`` + ``paths`` into a small structure that
expands a bare specifier into candidate extension-less, project-relative path stems. Those stems
feed straight into the existing resolver, so alias resolution composes with index/suffix lookup.

Constraints honored: pure stdlib, no new deps; fail-soft (a missing/garbled tsconfig yields
``None``, never an exception); deterministic (patterns and candidates emitted in a stable order).
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path

# Extensions a tsconfig target or alias may carry; stripped so candidates match the resolver's
# extension-less stems. ``.d.ts`` first so it wins over the bare ``.ts`` suffix.
_TS_EXTS = (".d.ts", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")

# Bound the `extends` chain so a cyclic/pathological config can't loop forever.
_MAX_EXTENDS_DEPTH = 16

_TSCONFIG_NAME = "tsconfig.json"


@dataclass(frozen=True)
class TsAliases:
    """Precompiled ``baseUrl`` + ``paths`` for one project, ready to expand bare specifiers.

    ``base_url`` is project-relative POSIX ("" = project root). ``patterns`` is a sorted tuple of
    ``(pattern, targets)`` where each target is project-relative POSIX (already joined to baseUrl),
    so expansion is a pure string substitution.
    """

    base_url: str
    patterns: tuple[tuple[str, tuple[str, ...]], ...]

    def candidate_stems(self, module: str) -> list[str]:
        """Extension-less, project-relative path stems ``module`` could alias to (in order).

        Only bare (non-relative) specifiers alias; a ``./x`` relative import is handled by the
        resolver directly. ``paths`` matches are tried first (in sorted pattern order), then the
        ``baseUrl``-relative fallback, deduped while preserving order for determinism.
        """
        if not module or module.startswith("."):
            return []
        out: list[str] = []
        seen: set[str] = set()

        def add(stem: str) -> None:
            s = posixpath.normpath(stem).strip("/")
            # Drop empties and any stem that escapes the project root — it can never match an
            # in-repo file anyway, and keeping candidates in-tree avoids surprising lookups.
            if s and s not in (".", "/") and not s.startswith("..") and s not in seen:
                seen.add(s)
                out.append(s)

        for pattern, targets in self.patterns:
            capture = _match_pattern(pattern, module)
            if capture is None:
                continue
            for target in targets:
                substituted = target.replace("*", capture) if "*" in target else target
                add(_strip_ts_ext(substituted))
        # baseUrl-relative bare import (e.g. `common/types` with baseUrl=src) — TS resolves bare
        # specifiers against baseUrl before falling through to node_modules.
        add(_strip_ts_ext(posixpath.join(self.base_url, module)))
        return out


def load(project_root: Path) -> TsAliases | None:
    """Load + precompile ``project_root``'s tsconfig aliases, or ``None`` when there's nothing usable.

    Looks for ``tsconfig.json`` then ``jsconfig.json`` at the project root, follows ``extends``,
    and returns ``None`` if neither exists, the JSON can't be recovered, or no ``paths``/``baseUrl``
    are declared. Never raises — a broken tsconfig must not break extraction/validation.
    """
    for name in (_TSCONFIG_NAME, "jsconfig.json"):
        path = project_root / name
        if path.is_file():
            try:
                return _compile(project_root, path)
            except Exception:  # noqa: BLE001 - fail soft; a bad tsconfig is not fatal
                return None
    return None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _compile(project_root: Path, tsconfig_path: Path) -> TsAliases | None:
    """Merge the ``extends`` chain and project ``baseUrl`` + ``paths`` into a :class:`TsAliases`."""
    base_url_rel = ""  # project-relative dir baseUrl points at ("" = root)
    have_base_url = False
    # pattern -> targets, each target project-relative (joined to the baseUrl in force at its decl).
    patterns: dict[str, tuple[str, ...]] = {}

    # Walk extends from base to leaf so the leaf (closest) wins on a key collision.
    for cfg_path, opts in _config_chain(tsconfig_path):
        decl_dir = _rel_dir(project_root, cfg_path)  # dir of the file declaring these options
        raw_base = opts.get("baseUrl")
        if isinstance(raw_base, str):
            base_url_rel = _norm_rel(posixpath.join(decl_dir, raw_base))
            have_base_url = True
        raw_paths = opts.get("paths")
        if isinstance(raw_paths, dict):
            # paths are relative to baseUrl if set, else to the declaring file's dir.
            anchor = base_url_rel if have_base_url else decl_dir
            for pattern, targets in raw_paths.items():
                if not isinstance(pattern, str) or not isinstance(targets, list):
                    continue
                joined = tuple(
                    _norm_rel(posixpath.join(anchor, t))
                    for t in targets
                    if isinstance(t, str)
                )
                if joined:
                    patterns[pattern] = joined

    if not patterns and not have_base_url:
        return None
    return TsAliases(
        base_url=base_url_rel,
        patterns=tuple(sorted(patterns.items())),
    )


def _config_chain(tsconfig_path: Path) -> list[tuple[Path, dict]]:
    """Return ``(path, compilerOptions)`` from the furthest ``extends`` base down to ``tsconfig_path``.

    Base-first so a later (closer) config overrides an earlier one. Cycles and unreadable bases
    are skipped; depth is bounded.
    """
    chain: list[tuple[Path, dict]] = []
    seen: set[str] = set()
    current: Path | None = tsconfig_path
    depth = 0
    while current is not None and depth < _MAX_EXTENDS_DEPTH:
        key = str(current.resolve()) if current.exists() else str(current)
        if key in seen:
            break
        seen.add(key)
        data = _read_jsonc(current)
        if data is None:
            break
        opts = data.get("compilerOptions")
        chain.append((current, opts if isinstance(opts, dict) else {}))
        extends = data.get("extends")
        current = _resolve_extends(current, extends) if isinstance(extends, str) else None
        depth += 1
    chain.reverse()  # base-first
    return chain


def _resolve_extends(from_cfg: Path, extends: str) -> Path | None:
    """Resolve a tsconfig ``extends`` reference to a file path (relative paths only; bare package
    references like ``@tsconfig/node20`` live in node_modules and are out of scope)."""
    if extends.startswith("."):
        target = (from_cfg.parent / extends).resolve()
        if target.suffix != ".json":
            # `extends: "./base"` may mean base.json or a directory's tsconfig.json.
            with_json = target.with_name(target.name + ".json")
            if with_json.is_file():
                target = with_json
            elif (target / _TSCONFIG_NAME).is_file():
                target = target / _TSCONFIG_NAME
        return target if target.is_file() else None
    return None


def _read_jsonc(path: Path) -> dict | None:
    """Parse a JSONC file (comments + trailing commas tolerated) into a dict, or ``None``."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(_strip_jsonc(text))
    except ValueError:  # JSONDecodeError is a ValueError subclass
        return None
    return data if isinstance(data, dict) else None


def _strip_jsonc(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments + trailing commas, respecting string literals."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = esc = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _rel_dir(project_root: Path, cfg_path: Path) -> str:
    """Project-relative POSIX dir of ``cfg_path`` ("" when at the root)."""
    try:
        rel = cfg_path.resolve().parent.relative_to(project_root.resolve())
    except ValueError:
        return ""
    return _norm_rel(rel.as_posix())


def _norm_rel(p: str) -> str:
    """Normalize a project-relative POSIX path; "." / "" collapse to ""."""
    normed = posixpath.normpath(p)
    return "" if normed in (".", "") else normed.strip("/")


def _strip_ts_ext(p: str) -> str:
    """Drop a trailing TS/JS extension so the stem matches the resolver's extension-less index."""
    for ext in _TS_EXTS:
        if p.endswith(ext):
            return p[: -len(ext)]
    return p


def _match_pattern(pattern: str, module: str) -> str | None:
    """Return the ``*`` capture for a wildcard ``pattern`` matching ``module`` ("" for an exact,
    wildcard-free pattern), or ``None`` if it doesn't match."""
    if "*" in pattern:
        prefix, _, suffix = pattern.partition("*")
        if module.startswith(prefix) and module.endswith(suffix) and \
                len(module) >= len(prefix) + len(suffix):
            return module[len(prefix): len(module) - len(suffix) if suffix else None]
        return None
    return "" if module == pattern else None
