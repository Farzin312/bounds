"""Managed agent-file stamping, merging, and status helpers."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..shared.version import VERSION
from .content import (
    _MARKDOWN,
    _MD_END,
    _MD_START,
    _MD_STAMP_RE,
    _YAML,
    _YAML_END,
    _YAML_START,
    _YAML_STAMP_RE,
)


def _version() -> str:
    """Return the installed Bounds version for generated-file stamps."""
    return VERSION


def body_hash(body: str) -> str:
    """Return the stable short hash used in generated-file stamps."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]


def build_block(fmt: str, body: str) -> str:
    """Wrap a managed body in markers and a version/hash stamp."""
    start, end = (_YAML_START, _YAML_END) if fmt == _YAML else (_MD_START, _MD_END)
    inner = body.rstrip("\n")
    marker = "#" if fmt == _YAML else "<!--"
    suffix = "" if fmt == _YAML else " -->"
    stamp = f"{marker} BOUNDS:GENERATED v={_version()} h={body_hash(inner)}{suffix}"
    return f"{start}\n{stamp}\n{inner}\n{end}"


class Buckets:
    """Accumulate per-file update outcomes for a sync report."""

    __slots__ = ("created", "updated", "unchanged", "skipped", "reasons")

    def __init__(self) -> None:
        self.created: set[str] = set()
        self.updated: set[str] = set()
        self.unchanged: set[str] = set()
        self.skipped: set[str] = set()
        self.reasons: dict[str, str] = {}

    def record(self, outcome: str, rel: str) -> None:
        """Record one managed-file outcome."""
        if outcome == "created":
            self.created.add(rel)
        elif outcome == "updated":
            self.updated.add(rel)
        elif outcome == "unchanged":
            self.unchanged.add(rel)
        elif outcome == "skipped_authored":
            self.skipped.add(rel)
            self.reasons[rel] = "authored"
        elif outcome == "skipped_hand_edited":
            self.skipped.add(rel)
            self.reasons[rel] = "hand-edited"
        elif outcome == "skipped_malformed":
            self.skipped.add(rel)
            self.reasons[rel] = "malformed-settings"


def upsert_block(
    target: Path,
    fmt: str,
    body: str,
    prefix: str = "",
    dedicated: bool = False,
) -> str:
    """Insert or refresh one managed block while preserving user-owned content."""
    start, end = (_YAML_START, _YAML_END) if fmt == _YAML else (_MD_START, _MD_END)
    block = build_block(fmt, body)

    if not target.exists():
        write_text(target, (prefix + "\n" if prefix else "") + block + "\n")
        return "created"

    existing = read_text(target)
    if start in existing and end in existing:
        current_inner = extract_inner(existing, start, end)
        if current_inner is not None and is_hand_edited(current_inner, fmt):
            return "skipped_hand_edited"
        new = replace_block(existing, start, end, block)
        if dedicated:
            new = ensure_front_matter(new, prefix)
        current_body = split_stamp(current_inner, fmt)[2] if current_inner is not None else None
        front_ok = not dedicated or not prefix or has_front_matter(existing)
        if current_body == body and front_ok:
            return "unchanged"
        write_text(target, new)
        return "updated"

    if dedicated:
        front = leading_front_matter(existing) or prefix
        write_text(target, (front + "\n" if front else "") + block + "\n")
        return "updated"

    if looks_bounds_authored(existing):
        return "skipped_authored"

    sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    write_text(target, existing + sep + block + "\n")
    return "updated"


def target_status(
    root: Path,
    rel: str,
    fmt: str,
    expected_body: str,
    dedicated: bool,
    front: str,
) -> str:
    """Classify one target as missing, stale, or configured."""
    target = root / Path(rel)
    if not target.exists():
        return "missing"
    start = _YAML_START if fmt == _YAML else _MD_START
    end = _YAML_END if fmt == _YAML else _MD_END
    text = read_text(target)
    if start not in text or end not in text:
        return "missing"
    inner = extract_inner(text, start, end)
    if inner is None:
        return "missing"
    _version_stamp, recorded, body = split_stamp(inner, fmt)
    if recorded is None or recorded != body_hash(expected_body):
        return "stale"
    if body_hash(body) != recorded:
        return "stale"
    if dedicated and front and not has_front_matter(text):
        return "stale"
    return "configured"


def has_front_matter(text: str) -> bool:
    """Return whether front matter starts at line one."""
    return text.startswith("---")


def leading_front_matter(text: str) -> str:
    """Return the leading fenced front matter, if present."""
    if not text.startswith("---"):
        return ""
    lines = text.split("\n")
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return "\n".join(lines[: idx + 1]) + "\n"
    return ""


def ensure_front_matter(text: str, prefix: str) -> str:
    """Restore required front matter without replacing an existing block."""
    if not prefix or has_front_matter(text):
        return text
    return prefix + "\n" + text


def extract_inner(text: str, start: str, end: str) -> str | None:
    """Return the first managed region's inner text."""
    i = text.find(start)
    if i == -1:
        return None
    j = text.find(end, i + len(start))
    if j == -1:
        return None
    return text[i + len(start):j].strip("\n")


def split_stamp(inner: str, fmt: str) -> tuple[str | None, str | None, str]:
    """Split a managed region into version, hash, and body."""
    pattern = _YAML_STAMP_RE if fmt == _YAML else _MD_STAMP_RE
    lines = inner.split("\n")
    if lines:
        match = pattern.fullmatch(lines[0].strip())
        if match:
            return match.group("v"), match.group("h"), "\n".join(lines[1:]).strip("\n")
    return None, None, inner


def is_hand_edited(inner: str, fmt: str) -> bool:
    """Return whether a stamped managed body no longer matches its hash."""
    _version_stamp, recorded, body = split_stamp(inner, fmt)
    return recorded is not None and body_hash(body) != recorded


def replace_block(text: str, start: str, end: str, block: str) -> str:
    """Replace the first complete managed block."""
    i = text.index(start)
    j = text.index(end, i) + len(end)
    return text[:i] + block + text[j:]


_BOUNDS_COMMAND_RE = re.compile(
    r"\bbounds\s+"
    r"(list|describe|validate|preflight|overview|init|impact|discover|calibrate|agent|ci|cache)\b"
)
_BOUNDS_HEADING_RE = re.compile(r"(?m)^\s*#.*\bbounds\b")


def looks_bounds_authored(text: str) -> bool:
    """Detect deliberate Bounds guidance in an unmarked shared file."""
    lowered = text.lower()
    return (
        bool(_BOUNDS_COMMAND_RE.search(lowered))
        or "`bounds`" in lowered
        or bool(_BOUNDS_HEADING_RE.search(lowered))
    )


def read_text(path: Path) -> str:
    """Read UTF-8 text."""
    return path.read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    """Write UTF-8 text, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
