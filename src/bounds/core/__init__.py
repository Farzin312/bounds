"""Core business logic package.

Submodules are intentionally loaded on demand. Eagerly importing every engine here
turns the package initializer into an upward-facing aggregator and obscures the
downward dependency graph.
"""

__all__: list[str] = []
