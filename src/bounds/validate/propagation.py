"""Reference propagation: which consumers are affected when a provider's interface surface changes.

Zero tree-sitter, zero LLM — pure graph traversal over the declared `consumes` edges. Propagation
depth is bounded by the *changed provider's* criticality (config.PROPAGATION_DEPTH):
  core      -> unbounded transitive closure (-1)
  connector -> direct consumers only (1)
  leaf      -> no propagation (0)
"""

from __future__ import annotations

from collections import deque

from .. import config
from ..models import SubsystemCompact


def build_consumer_index(subsystems: dict[str, SubsystemCompact]) -> dict[str, list[str]]:
    """Map each provider subsystem name -> sorted list of subsystems that consume it."""
    index: dict[str, list[str]] = {name: [] for name in subsystems}
    for name in sorted(subsystems):
        for c in subsystems[name].consumes:
            index.setdefault(c.subsystem, [])
            index[c.subsystem].append(name)
    return {provider: sorted(set(consumers)) for provider, consumers in index.items()}


def transitive_consumers(target: str, subsystems: dict[str, SubsystemCompact]) -> list[str]:
    """Full transitive closure of subsystems that (in)directly consume ``target``.

    Unlike :func:`propagate` (criticality-bounded, for drift), this is unbounded — it answers
    the ``bounds impact`` question "everything that could break if I change ``target``'s
    surface", which a developer asks deliberately for a risky change. Excludes ``target``
    itself. Returned sorted for stable output.
    """
    index = build_consumer_index(subsystems)
    seen: set[str] = set()
    queue: deque[str] = deque([target])
    while queue:
        node = queue.popleft()
        for consumer in index.get(node, []):
            if consumer not in seen:
                seen.add(consumer)
                queue.append(consumer)
    seen.discard(target)
    return sorted(seen)


def propagate(
    dirty: set[str],
    subsystems: dict[str, SubsystemCompact],
    depth_map: dict[str, int] | None = None,
) -> set[str]:
    """Return the set of consumer subsystems affected by a change to any subsystem in ``dirty``.

    The originally-dirty subsystems are excluded from the result (they're the cause, not the impact).
    ``depth_map`` (criticality label -> hop depth) comes from the resolved root registry so custom
    criticality labels (s-17) drive propagation; it defaults to the built-in depths.
    """
    depth_map = depth_map if depth_map is not None else config.PROPAGATION_DEPTH
    index = build_consumer_index(subsystems)
    affected: set[str] = set()

    for provider in sorted(dirty):
        sub = subsystems.get(provider)
        criticality = sub.criticality if sub else "leaf"
        max_depth = depth_map.get(criticality, 0)
        if max_depth == 0:
            continue

        visited = {provider}
        queue: deque[tuple[str, int]] = deque([(provider, 0)])
        while queue:
            node, depth = queue.popleft()
            if max_depth != -1 and depth >= max_depth:
                continue
            for consumer in index.get(node, []):
                if consumer not in visited:
                    visited.add(consumer)
                    affected.add(consumer)
                    queue.append((consumer, depth + 1))

    return affected - set(dirty)
