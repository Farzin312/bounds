"""Tests for the CI gate generator, bounds.ciconfig."""

from __future__ import annotations

import pytest
import yaml

from bounds import ciconfig, errors


def _read(path):
    return path.read_text(encoding="utf-8")


def test_all_targets_creates_three_files(tmp_path):
    result = ciconfig.run_ci_install(tmp_path, targets=set())

    action = tmp_path / ".github/workflows/bounds.yml"
    precommit = tmp_path / ".pre-commit-config.yaml"
    gitlab = tmp_path / ".gitlab-ci.yml"
    assert action.exists()
    assert precommit.exists()
    assert gitlab.exists()

    assert result["created"] == sorted(
        [".github/workflows/bounds.yml", ".gitlab-ci.yml", ".pre-commit-config.yaml"]
    )
    assert result["skipped"] == []
    assert result["targets"] == ["action", "gitlab", "precommit"]


def test_action_cache_key_and_preflight(tmp_path):
    ciconfig.run_ci_install(tmp_path, targets={"action"})
    text = _read(tmp_path / ".github/workflows/bounds.yml")

    # Cache key is hashed over manifests/root, not the branch.
    assert "hashFiles('.bounds/root.yaml', '.bounds/manifests/**')" in text
    assert "path: .bounds/cache.db" in text
    # Remote CI runs the freshness gate then the strict gate.
    assert "bounds calibrate --check" in text
    assert "bounds preflight --ci" in text
    # Installs the correctly-named package via pipx (GitHub runners ship pipx); never the
    # squatted bare `bounds` PyPI name.
    assert "pipx install bounds-cli" in text
    assert "pipx install bounds\n" not in text and "pipx install bounds " not in text
    # Skip convention is documented.
    assert "[skip bounds]" in text


def test_precommit_uses_quick_gate(tmp_path):
    ciconfig.run_ci_install(tmp_path, targets={"precommit"})
    data = yaml.safe_load(_read(tmp_path / ".pre-commit-config.yaml"))

    hooks = data["repos"][0]["hooks"]
    assert hooks[0]["id"] == "bounds"
    assert hooks[0]["entry"] == "bounds validate --quick --ci"
    assert hooks[0]["pass_filenames"] is False
    assert hooks[0]["stages"] == ["pre-commit"]


def test_gitlab_job_uses_preflight(tmp_path):
    ciconfig.run_ci_install(tmp_path, targets={"gitlab"})
    data = yaml.safe_load(_read(tmp_path / ".gitlab-ci.yml"))

    assert data["bounds"]["stage"] == "test"
    assert data["bounds"]["image"] == "python:3.12-slim"
    assert "bounds preflight --ci" in data["bounds"]["script"]
    assert data["bounds"]["rules"] == [{"changes": ["src/**/*", ".bounds/**/*"]}]
    # The slim image has pip but NOT pipx; use pip with the correct package name so the
    # `bounds` console script lands on PATH before the gate steps run.
    assert "pip install bounds-cli" in data["bounds"]["script"]
    assert not any("pipx" in step for step in data["bounds"]["script"])
    # Never the bare squatted `bounds` package as its own step (only `bounds-cli`).
    assert not any(step == "pip install bounds" for step in data["bounds"]["script"])


def test_idempotent_rerun_reports_skipped(tmp_path):
    first = ciconfig.run_ci_install(tmp_path, targets=set())
    assert len(first["created"]) == 3

    second = ciconfig.run_ci_install(tmp_path, targets=set())
    assert second["created"] == []
    assert second["skipped"] == sorted(
        [".github/workflows/bounds.yml", ".gitlab-ci.yml", ".pre-commit-config.yaml"]
    )


def test_precommit_append_preserves_existing_hook(tmp_path):
    existing = {
        "repos": [
            {
                "repo": "https://github.com/psf/black",
                "rev": "24.0.0",
                "hooks": [{"id": "black"}],
            }
        ]
    }
    path = tmp_path / ".pre-commit-config.yaml"
    path.write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")

    result = ciconfig.run_ci_install(tmp_path, targets={"precommit"})
    assert result["created"] == [".pre-commit-config.yaml"]

    data = yaml.safe_load(_read(path))
    repo_ids = {repo.get("repo") for repo in data["repos"]}
    # Prior hook preserved.
    assert "https://github.com/psf/black" in repo_ids
    # Bounds hook added.
    hook_ids = {
        hook["id"] for repo in data["repos"] for hook in repo.get("hooks", [])
    }
    assert "black" in hook_ids
    assert "bounds" in hook_ids


def test_precommit_append_idempotent(tmp_path):
    ciconfig.run_ci_install(tmp_path, targets={"precommit"})
    second = ciconfig.run_ci_install(tmp_path, targets={"precommit"})
    assert second["skipped"] == [".pre-commit-config.yaml"]

    data = yaml.safe_load(_read(tmp_path / ".pre-commit-config.yaml"))
    bounds_hooks = [
        hook
        for repo in data["repos"]
        for hook in repo.get("hooks", [])
        if hook["id"] == "bounds"
    ]
    assert len(bounds_hooks) == 1


def test_precommit_missing_repos_list_raises(tmp_path):
    path = tmp_path / ".pre-commit-config.yaml"
    path.write_text("default_language_version:\n  python: python3.12\n", encoding="utf-8")
    with pytest.raises(errors.BoundsError) as exc:
        ciconfig.run_ci_install(tmp_path, targets={"precommit"})
    assert exc.value.code == errors.E_USAGE


def test_gitlab_append_preserves_existing_jobs(tmp_path):
    existing = {
        "stages": ["build", "test"],
        "build": {"stage": "build", "script": ["make"]},
    }
    path = tmp_path / ".gitlab-ci.yml"
    path.write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")

    result = ciconfig.run_ci_install(tmp_path, targets={"gitlab"})
    assert result["created"] == [".gitlab-ci.yml"]

    data = yaml.safe_load(_read(path))
    # Existing jobs/keys preserved.
    assert data["stages"] == ["build", "test"]
    assert data["build"] == {"stage": "build", "script": ["make"]}
    # Bounds job added.
    assert "bounds preflight --ci" in data["bounds"]["script"]


def test_gitlab_existing_bounds_job_skipped(tmp_path):
    path = tmp_path / ".gitlab-ci.yml"
    path.write_text(
        yaml.safe_dump({"bounds": {"script": ["custom"]}}, sort_keys=False),
        encoding="utf-8",
    )
    result = ciconfig.run_ci_install(tmp_path, targets={"gitlab"})
    assert result["skipped"] == [".gitlab-ci.yml"]
    # Existing job untouched.
    data = yaml.safe_load(_read(path))
    assert data["bounds"]["script"] == ["custom"]


def test_single_target_writes_only_that_file(tmp_path):
    result = ciconfig.run_ci_install(tmp_path, targets={"action"})
    assert result["created"] == [".github/workflows/bounds.yml"]
    assert result["targets"] == ["action"]
    assert (tmp_path / ".github/workflows/bounds.yml").exists()
    assert not (tmp_path / ".pre-commit-config.yaml").exists()
    assert not (tmp_path / ".gitlab-ci.yml").exists()


def test_unknown_target_raises(tmp_path):
    with pytest.raises(errors.BoundsError) as exc:
        ciconfig.run_ci_install(tmp_path, targets={"jenkins"})
    assert exc.value.code == errors.E_USAGE
