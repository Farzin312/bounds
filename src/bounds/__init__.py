"""Bounds — a verified, CI-enforced contract of what your architecture is supposed to be. Architecture tooling for AI-native codebases — zero LLM inside."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

# Published on PyPI as "bounds-cli" (the bare "bounds" name belongs to an unrelated
# project); the import package and the CLI command stay "bounds". Resolve across both
# names so the version works whether installed from PyPI or as an editable checkout.
__version__ = "unknown"
for _dist in ("bounds-cli", "bounds"):
    try:
        __version__ = _version(_dist)
        break
    except PackageNotFoundError:
        continue
