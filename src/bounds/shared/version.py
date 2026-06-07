"""Installed package version resolution.

Version metadata is foundational state used by the CLI, maintenance commands, and
generated-file stamps. Keeping it in ``shared`` prevents lower layers from importing
the top-level compatibility facade solely to read ``bounds.__version__``.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version


def installed_version() -> str:
    """Return the installed Bounds distribution version, or ``unknown``."""
    for distribution in ("bounds-cli", "bounds"):
        try:
            return distribution_version(distribution)
        except PackageNotFoundError:
            continue
    return "unknown"


VERSION = installed_version()
__version__ = VERSION

__all__ = ["VERSION", "__version__", "installed_version"]
