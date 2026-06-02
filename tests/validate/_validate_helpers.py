"""Shared fixtures/helpers for the split validate test suite.

Holds the ``_ctx`` CheckContext builder (and the dataclass aliases the checks tests lean on) in one
place so test_loader/test_schema/test_resolve/test_checks/test_engine all import it instead of each
re-declaring a divergent copy.
"""

from __future__ import annotations

from pathlib import Path

from bounds.models import RootManifest
from bounds.validate.checks import CheckContext


def _ctx(subsystems, extracts=None, file_owner=None, dirty=None, propagated=None):
    return CheckContext(
        project_root=Path("."),
        root=RootManifest(),
        subsystems=subsystems,
        extracts=extracts or {},
        file_owner=file_owner or {},
        dirty=dirty or set(),
        propagated=propagated or set(),
    )
