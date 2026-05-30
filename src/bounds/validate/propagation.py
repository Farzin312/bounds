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


def propagate(dirty: set[str], subsystems: dict[str, SubsystemCompact]) -> set[str]:
    """Return the set of consumer subsystems affected by a change to any subsystem in ``dirty``.

    The originally-dirty subsystems are excluded from the result (they're the cause, not the impact).
    """
    index = build_consumer_index(subsystems)
    affected: set[str] = set()

    for provider in sorted(dirty):
        sub = subsystems.get(provider)
        criticality = sub.criticality if sub else "leaf"
        max_depth = config.PROPAGATION_DEPTH.get(criticality, 0)
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
