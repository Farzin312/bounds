"""Stable error-code registry and the CompactError exception.

Codes are part of the public contract — never renumber or repurpose. Severities:
  fatal   -> raised as CompactError, aborts the command (exit 2)
  error   -> blocking Issue (blocks preflight always; full only when enforce=on)
  warning -> non-blocking Issue
"""

from __future__ import annotations

# ---- Validation codes (emitted as Issues) ----
E_STRUCTURAL_DRIFT = "E_STRUCTURAL_DRIFT"
E_BOUNDARY_VIOLATION = "E_BOUNDARY_VIOLATION"
E_CONTRACT_MISSING_EXPORT = "E_CONTRACT_MISSING_EXPORT"
E_STALE_INTERFACE = "E_STALE_INTERFACE"
E_CYCLE_DETECTED = "E_CYCLE_DETECTED"
E_ORPHAN_EXPORT = "E_ORPHAN_EXPORT"
E_UNRESOLVED_REFERENCE = "E_UNRESOLVED_REFERENCE"

# ---- Schema / extraction codes ----
E_SCHEMA_INVALID = "E_SCHEMA_INVALID"
E_UNSUPPORTED_LANGUAGE = "E_UNSUPPORTED_LANGUAGE"
E_EXTRACTION_FAILED = "E_EXTRACTION_FAILED"

# ---- Fatal codes (raised) ----
E_MANIFEST_NOT_FOUND = "E_MANIFEST_NOT_FOUND"
E_MANIFEST_PARSE_ERROR = "E_MANIFEST_PARSE_ERROR"
E_SUBSYSTEM_NOT_FOUND = "E_SUBSYSTEM_NOT_FOUND"
E_USAGE = "E_USAGE"

# Severity lookup for emitted Issue codes (advisory; engine may override per context).
SEVERITY = {
    E_STRUCTURAL_DRIFT: "error",
    E_BOUNDARY_VIOLATION: "error",
    E_CONTRACT_MISSING_EXPORT: "error",
    E_STALE_INTERFACE: "error",
    E_CYCLE_DETECTED: "error",
    E_ORPHAN_EXPORT: "warning",
    E_UNRESOLVED_REFERENCE: "warning",
    E_SCHEMA_INVALID: "error",
    E_UNSUPPORTED_LANGUAGE: "warning",
    E_EXTRACTION_FAILED: "warning",
}


class CompactError(Exception):
    """A fatal, user-facing error. Carries a stable code and an actionable fix."""

    def __init__(self, code: str, message: str, fix: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "fix": self.fix}}
