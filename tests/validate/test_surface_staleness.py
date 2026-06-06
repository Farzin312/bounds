"""Deterministic staleness detection for hand-authored unsupported-language exposes.

Bounds has no adapter for Go/Rust/Java, so it cannot verify a hand-authored ``exposes`` against its
source. These tests lock in ``E_UNSUPPORTED_SURFACE_STALE``: once the surface is confirmed (committed
baseline via ``calibrate --dump-baseline``), a later change to the file is flagged so an agent knows
to re-verify — the deterministic "re-run" trigger, no LLM. See docs/coverage.md.

(Go, not shell, is used as the unsupported language here: shell has an adapter as of Stage 5, so it
is verified directly and never goes through the surface-baseline path.)
"""
from __future__ import annotations

import subprocess

from bounds.shared import config, errors, surface
from bounds.core.calibrate import dump_baseline
from bounds.core.validate import engine


def _git_init(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


def _go_repo(tmp_path):
    """A subsystem ``payments`` owning one Go file (unsupported language) with hand-authored exposes."""
    svc = tmp_path / "payments"
    svc.mkdir()
    (svc / "charge.go").write_text("package payments\nfunc Charge() error { return nil }\n")
    cfg = tmp_path / config.BOUNDS_DIR
    (cfg / config.MANIFESTS_DIR).mkdir(parents=True)
    (cfg / config.ROOT_FILE).write_text(
        'version: "1"\nproject: st\nlanguages: [go]\nsubsystems: [payments]\n'
    )
    (cfg / config.MANIFESTS_DIR / "payments.yaml").write_text(
        "name: payments\nrole: library\ncriticality: leaf\npaths:\n  - payments\n"
        "exposes:\n  - {name: Charge, kind: function}\n"
    )
    _git_init(tmp_path)
    return tmp_path


def _stale(report):
    return [i for i in report.issues if i.code == errors.E_UNSUPPORTED_SURFACE_STALE]


def test_no_staleness_signal_without_a_baseline(tmp_path):
    """Opt-in like the drift baseline: with no committed surface-baseline.json, validate emits no
    staleness signal (nothing to compare against)."""
    _go_repo(tmp_path)
    report = engine.run(tmp_path, mode="full", include_gitignored=True)
    assert _stale(report) == []
    assert not (tmp_path / config.BOUNDS_DIR / config.SURFACE_BASELINE_FILE).is_file()


def test_unchanged_surface_after_baseline_is_clean(tmp_path):
    """Dumping the baseline records the file's hash; an unchanged file is not stale."""
    _go_repo(tmp_path)
    dump_baseline(tmp_path)
    assert (tmp_path / config.BOUNDS_DIR / config.SURFACE_BASELINE_FILE).is_file()
    report = engine.run(tmp_path, mode="full", include_gitignored=True)
    assert _stale(report) == []


def test_changed_unsupported_file_flags_stale_then_reconfirm_clears(tmp_path):
    """The core loop: confirm → change the file → E_UNSUPPORTED_SURFACE_STALE (names the file,
    advisory, never blocks) → re-confirm → clean."""
    _go_repo(tmp_path)
    dump_baseline(tmp_path)
    (tmp_path / "payments" / "charge.go").write_text(
        "package payments\nfunc Charge() error { return nil }\nfunc Refund() error { return nil }\n"
    )
    report = engine.run(tmp_path, mode="full", include_gitignored=True)
    stale = _stale(report)
    assert len(stale) == 1
    assert stale[0].severity == "warning"
    assert stale[0].subsystem == "payments"
    assert "payments/charge.go" in stale[0].message
    assert stale[0].count == 1
    assert "dump-baseline" in stale[0].fix
    assert not report.errors()  # advisory only — never blocks CI on its own
    # re-confirm and it clears
    dump_baseline(tmp_path)
    report2 = engine.run(tmp_path, mode="full", include_gitignored=True)
    assert _stale(report2) == []


def test_quick_mode_skips_staleness(tmp_path):
    """--quick stays on the hot path: no per-file source reads, no staleness scan."""
    _go_repo(tmp_path)
    dump_baseline(tmp_path)
    (tmp_path / "payments" / "charge.go").write_text("package payments\nfunc Changed() {}\n")
    report = engine.run(tmp_path, mode="quick", include_gitignored=True)
    assert _stale(report) == []


def test_stale_subsystems_flags_changed_and_removed_ignores_additions():
    """Unit: a changed hash OR a removed baseline file is stale; a brand-new file is not (that's a
    coverage concern); a subsystem absent from the baseline is never stale."""
    base = {"a": {"a/x.go": "h1", "a/y.go": "h2"}}
    cur = {"a": {"a/x.go": "h1NEW", "a/z.go": "h3"}, "b": {"b/q.go": "h9"}}
    out = surface.stale_subsystems(base, cur)
    assert out == {"a": ["a/x.go", "a/y.go"]}  # x changed + y removed (sorted); z added → ignored; b → not baselined


def test_surface_baseline_roundtrip_and_failsoft(tmp_path):
    """write_baseline → load_baseline round-trips; a missing/garbage file loads as {} (no raise)."""
    surfaces = {"s": {"s/a.go": "deadbeef", "s/b.go": "cafe"}}
    surface.write_baseline(tmp_path, surfaces)
    assert surface.load_baseline(tmp_path) == surfaces
    (tmp_path / config.BOUNDS_DIR / config.SURFACE_BASELINE_FILE).write_text("{not json", encoding="utf-8")
    assert surface.load_baseline(tmp_path) == {}
