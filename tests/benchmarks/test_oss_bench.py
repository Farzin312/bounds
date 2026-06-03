from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_oss_bench():
    path = Path(__file__).resolve().parents[2] / "benchmarks" / "oss_bench.py"
    spec = importlib.util.spec_from_file_location("oss_bench_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_oss_bench_records_presetup_recovery_and_fresh_health(monkeypatch, tmp_path):
    """The OSS harness must measure both sides: pre-setup (before init) recovery UX and fresh-Bounds health."""
    oss_bench = _load_oss_bench()
    (tmp_path / "app.py").write_text("def run():\n    return True\n", encoding="utf-8")
    state = {"initialized": False}

    def fake_run(args, cwd):
        assert cwd == tmp_path
        if args == ["list"] and not state["initialized"]:
            return (
                json.dumps(
                    {
                        "error": {
                            "code": "E_MANIFEST_NOT_FOUND",
                            "message": "No .bounds directory found.",
                            "fix": "run 'bounds init --root'",
                        }
                    }
                ),
                "",
                2,
                0.01,
            )
        if args == ["init", "--root"]:
            state["initialized"] = True
            return "", "", 0, 0.02
        if args == ["discover", "--apply"]:
            return "discover: wrote 1 subsystem\n", "", 0, 0.03
        if args == ["list"]:
            return json.dumps({"subsystems": [{"name": "app", "exposes": 1, "consumed_by": []}]}), "", 0, 0.01
        if args == ["describe", "app"]:
            return json.dumps({"subsystem": "app", "exposes": [{"name": "run"}]}), "", 0, 0.01
        if args == ["impact", "app"]:
            return json.dumps({"subsystem": "app", "blast_radius": 0}), "", 0, 0.01
        if args == ["overview"]:
            return (
                json.dumps(
                    {
                        "health": {
                            "ok": False,
                            "cycles": 0,
                            "validation": {
                                "errors": 1,
                                "warnings": 2,
                                "structural_drift": 3,
                                "boundary_violations": 4,
                                "ownership_overlaps": 5,
                                "mapped_pct": 90.0,
                            },
                        },
                        "schema_issues": [],
                    }
                ),
                "",
                0,
                0.01,
            )
        if args == ["validate"]:
            return (
                json.dumps(
                    {
                        "issues": [
                            {"code": "E_CONTRACT_MISSING_EXPORT", "severity": "error"},
                            {"code": "E_SUBSYSTEM_OVERLAP", "severity": "warning"},
                        ]
                    }
                ),
                "",
                1,
                0.01,
            )
        raise AssertionError(f"unexpected bounds command: {args}")

    monkeypatch.setattr(oss_bench, "_run", fake_run)
    monkeypatch.setattr(oss_bench, "manifest_source_files", lambda _root, _name: [tmp_path / "app.py"])

    result = oss_bench.measure(tmp_path, "demo", "python", lambda text: len(text.split()))

    assert result["presetup_list_rc"] == 2
    assert result["presetup_error_code"] == "E_MANIFEST_NOT_FOUND"
    assert result["presetup_error_fix"] == "run 'bounds init --root'"
    assert result["init_rc"] == 0
    assert result["overview_validation_errors"] == 1
    assert result["overview_boundary_violations"] == 4
    assert result["overview_ownership_overlaps"] == 5
    assert result["validate_error_count"] == 1
    assert result["validate_warning_count"] == 1
    assert result["validate_error_clean_on_fresh_discover"] is False
