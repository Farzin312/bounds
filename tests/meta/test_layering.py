"""Static guards for the package layering introduced by the modular refactor."""

from __future__ import annotations

import ast
from pathlib import Path


_PACKAGE = Path(__file__).parents[2] / "src" / "bounds"
_TIER = {"shared": 0, "core": 1, "agents": 2, "maintenance": 2, "cli": 3}


def _resolved_import(path: Path, node: ast.ImportFrom) -> str | None:
    if not node.level:
        return node.module
    relative = path.relative_to(_PACKAGE)
    package = ["bounds", *relative.parts[:-1]]
    base = package[: len(package) - node.level + 1]
    return ".".join(base + ((node.module or "").split(".") if node.module else []))


def test_internal_dependencies_only_flow_downward():
    """Lower tiers must not import higher-tier integration or CLI packages."""
    violations: list[str] = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        relative = path.relative_to(_PACKAGE)
        if len(relative.parts) < 2 or relative.parts[0] not in _TIER:
            continue
        source_tier = relative.parts[0]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        targets: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                target = _resolved_import(path, node)
                if target:
                    targets.append((node.lineno, target))
            elif isinstance(node, ast.Import):
                targets.extend((node.lineno, alias.name) for alias in node.names)
        for line, target in targets:
            parts = target.split(".")
            if len(parts) < 2 or parts[0] != "bounds" or parts[1] not in _TIER:
                continue
            destination_tier = parts[1]
            if _TIER[destination_tier] > _TIER[source_tier]:
                violations.append(
                    f"{relative}:{line}: {source_tier} imports higher tier {destination_tier}"
                )

    assert violations == []


def test_orchestration_modules_stay_reviewable():
    """CLI and agent orchestration files stay below the refactor's 500-line review limit."""
    oversized = []
    for package in ("agents", "cli"):
        for path in sorted((_PACKAGE / package).glob("*.py")):
            lines = len(path.read_text(encoding="utf-8").splitlines())
            if lines > 500:
                oversized.append(f"{path.relative_to(_PACKAGE)}: {lines}")

    assert oversized == []
