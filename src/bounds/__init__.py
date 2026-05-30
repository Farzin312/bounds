"""Bounds — AI-native codebase understanding via subsystem boundary manifests."""

try:
    from importlib.metadata import version as _version
    __version__ = _version("bounds")
except Exception:
    __version__ = "unknown"
