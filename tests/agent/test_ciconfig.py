"""Tests for the CI gate generator, bounds.ciconfig."""

from __future__ import annotations

import pytest
import yaml

from bounds.core import ciconfig
from bounds.shared import errors


def _read(path):
    return path.read_text(encoding="utf-8")


def test_all_targets_creates_three_files(tmp_path):
    """An explicit all-three target set must install all CI gates (action/gitlab/precommit) with sorted `created`/`targets` lists — byte-stable output across runs (determinism). This is the `--all` escape hatch, never an empty-set default."""
    result = ciconfig.run_ci_install(tmp_path, targets=set(ciconfig.ALL_TARGETS))

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


def test_empty_targets_raises_never_dumps_all(tmp_path):
    """An empty target set must raise BoundsError E_USAGE and write nothing — `run_ci_install` never falls back to "install all three", so a missing selection can't silently drop a stray host config (the old GitHub-gets-a-GitLab-file footgun)."""
    with pytest.raises(errors.BoundsError) as exc:
        ciconfig.run_ci_install(tmp_path, targets=set())
    assert exc.value.code == errors.E_USAGE
    assert not (tmp_path / ".github/workflows/bounds.yml").exists()
    assert not (tmp_path / ".gitlab-ci.yml").exists()
    assert not (tmp_path / ".pre-commit-config.yaml").exists()


def test_detect_ci_provider_github(tmp_path):
    """A `.github/` directory marks the repo as GitHub Actions — detection returns only {action}, never gitlab and never the orthogonal precommit hook."""
    (tmp_path / ".github").mkdir()
    assert ciconfig.detect_ci_provider(tmp_path) == {"action"}


def test_detect_ci_provider_gitlab(tmp_path):
    """A root `.gitlab-ci.yml` (or `.gitlab/` dir) marks the repo as GitLab CI — detection returns only {gitlab}."""
    (tmp_path / ".gitlab-ci.yml").write_text("stages: [test]\n", encoding="utf-8")
    assert ciconfig.detect_ci_provider(tmp_path) == {"gitlab"}
    other = tmp_path / "viadir"
    other.mkdir()
    (other / ".gitlab").mkdir()
    assert ciconfig.detect_ci_provider(other) == {"gitlab"}


def test_detect_ci_provider_none_and_both(tmp_path):
    """No markers → empty set; both markers → {action, gitlab} (ambiguous). detect never guesses or includes precommit — it just reports the markers it found."""
    assert ciconfig.detect_ci_provider(tmp_path) == set()
    (tmp_path / ".github").mkdir()
    (tmp_path / ".gitlab-ci.yml").write_text("stages: [test]\n", encoding="utf-8")
    assert ciconfig.detect_ci_provider(tmp_path) == {"action", "gitlab"}


def test_github_workflows_dir_created_when_absent(tmp_path):
    """Installing the action target into a repo with no `.github/workflows/` must create the directory tree (parents=True) and write the workflow — folder creation is part of the install, not a precondition."""
    assert not (tmp_path / ".github").exists()
    result = ciconfig.run_ci_install(tmp_path, targets={"action"})
    assert result["created"] == [".github/workflows/bounds.yml"]
    assert (tmp_path / ".github" / "workflows" / "bounds.yml").is_file()


def test_action_cache_key_and_preflight(tmp_path):
    """The GitHub Action must key its cache on manifests/root, run both gates, and install Bounds without PyPI."""
    ciconfig.run_ci_install(tmp_path, targets={"action"})
    text = _read(tmp_path / ".github/workflows/bounds.yml")

    # Cache key is hashed over manifests/root, not the branch.
    assert "hashFiles('.bounds/root.yaml', '.bounds/manifests/**')" in text
    assert "path: .bounds/cache.db" in text
    # Remote CI runs the freshness gate then the strict gate.
    assert "bounds calibrate --check" in text
    assert "bounds preflight --ci" in text
    # External repos install from the git ref; the Bounds repo itself installs the checked-out PR.
    assert 'pipx install "git+https://github.com/Farzin312/bounds.git"' in text
    assert "pipx install ." in text
    assert "pipx install bounds\n" not in text and "pipx install bounds " not in text
    assert "pipx install bounds-cli" not in text  # PyPI name not used until published
    # Intent to switch once published is recorded.
    assert "published on PyPI" in text
    # Skip convention is documented.
    assert "[skip bounds]" in text


def test_existing_action_with_pypi_install_is_migrated(tmp_path):
    """Existing generated Bounds workflows with the unpublished PyPI install line must be migrated in place."""
    path = tmp_path / ".github" / "workflows" / "bounds.yml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "name: bounds\n"
        "jobs:\n"
        "  bounds:\n"
        "    steps:\n"
        "      - run: pipx install bounds-cli\n"
        "      - run: bounds preflight --ci\n",
        encoding="utf-8",
    )

    result = ciconfig.run_ci_install(tmp_path, targets={"action"})
    text = _read(path)

    assert result["created"] == [".github/workflows/bounds.yml"]
    assert 'pipx install "git+https://github.com/Farzin312/bounds.git"' in text
    assert "pipx install ." in text
    assert "pipx install bounds-cli" not in text


def test_existing_action_with_git_install_is_migrated_to_checkout_aware_install(tmp_path):
    """Existing git-only Bounds workflows must be migrated so Bounds' own PR CI tests the checkout."""
    ciconfig.run_ci_install(tmp_path, targets={"action"})
    path = tmp_path / ".github" / "workflows" / "bounds.yml"
    old = path.read_text(encoding="utf-8").replace(
        "      # External repos install Bounds from git until `bounds-cli` is published on PyPI.\n"
        "      # The Bounds repo itself installs the checked-out PR, not the default branch.\n"
        "      - name: Install Bounds\n"
        "        run: |\n"
        "          if [ -f pyproject.toml ] && grep -q '^name = \"bounds-cli\"' pyproject.toml; then\n"
        "            pipx install .\n"
        "          else\n"
        "            pipx install \"git+https://github.com/Farzin312/bounds.git\"\n"
        "          fi\n",
        "      # Installs from git (works today; GitHub runners preinstall pipx).\n"
        "      # switch to `bounds-cli` (pipx) once published to PyPI.\n"
        "      - run: pipx install \"git+https://github.com/Farzin312/bounds.git\"\n",
    )
    path.write_text(old, encoding="utf-8")

    result = ciconfig.run_ci_install(tmp_path, targets={"action"})
    text = _read(path)

    assert result["created"] == [".github/workflows/bounds.yml"]
    assert "pipx install ." in text
    assert "Install Bounds" in text


def test_precommit_uses_quick_gate(tmp_path):
    """The pre-commit hook must use `validate --quick --ci` (sub-200ms quick path) with pass_filenames False at the pre-commit stage — a local commit gate stays fast and repo-wide."""
    ciconfig.run_ci_install(tmp_path, targets={"precommit"})
    data = yaml.safe_load(_read(tmp_path / ".pre-commit-config.yaml"))

    hooks = data["repos"][0]["hooks"]
    assert hooks[0]["id"] == "bounds"
    assert hooks[0]["entry"] == "bounds validate --quick --ci"
    assert hooks[0]["pass_filenames"] is False
    assert hooks[0]["stages"] == ["pre-commit"]


def test_gitlab_job_uses_preflight(tmp_path):
    """The GitLab job must run preflight --ci on src/.bounds changes and install from the git ref via pip (the slim image lacks pipx; works today — PyPI publish pending) — never pipx and never the bare squatted `bounds` package."""
    ciconfig.run_ci_install(tmp_path, targets={"gitlab"})
    data = yaml.safe_load(_read(tmp_path / ".gitlab-ci.yml"))

    assert data["bounds"]["stage"] == "test"
    assert data["bounds"]["image"] == "python:3.12-slim"
    assert "bounds preflight --ci" in data["bounds"]["script"]
    assert data["bounds"]["rules"] == [{"changes": ["src/**/*", ".bounds/**/*"]}]
    # The slim image has pip but NOT pipx; install from the git ref (works today — bounds-cli
    # is not on PyPI yet) so the `bounds` console script lands on PATH before the gate steps run.
    assert (
        'pip install "git+https://github.com/Farzin312/bounds.git"'
        in data["bounds"]["script"]
    )
    assert not any("pipx" in step for step in data["bounds"]["script"])
    # Never the bare squatted `bounds` package, and not the PyPI name until published.
    assert not any(step == "pip install bounds" for step in data["bounds"]["script"])
    assert not any("bounds-cli" in step for step in data["bounds"]["script"])


def test_idempotent_rerun_reports_skipped(tmp_path):
    """A second install must create nothing and report all three files (sorted) in `skipped` — re-running the installer is idempotent and never duplicates or clobbers CI config."""
    targets = set(ciconfig.ALL_TARGETS)
    first = ciconfig.run_ci_install(tmp_path, targets=targets)
    assert len(first["created"]) == 3

    second = ciconfig.run_ci_install(tmp_path, targets=targets)
    assert second["created"] == []
    assert second["skipped"] == sorted(
        [".github/workflows/bounds.yml", ".gitlab-ci.yml", ".pre-commit-config.yaml"]
    )


def test_precommit_append_preserves_existing_hook(tmp_path):
    """Installing into an existing .pre-commit-config.yaml must append the bounds hook while preserving the user's prior hooks (e.g. black) — never overwrite a hand-maintained config."""
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
    """Re-installing the precommit target must report it skipped and leave exactly one bounds hook — appending is idempotent, never accumulating duplicate hooks across runs."""
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
    """A pre-commit config lacking a `repos:` list must raise BoundsError E_USAGE — the installer fails loudly rather than guessing where to inject the hook (fail-soft/report-hard)."""
    path = tmp_path / ".pre-commit-config.yaml"
    path.write_text("default_language_version:\n  python: python3.12\n", encoding="utf-8")
    with pytest.raises(errors.BoundsError) as exc:
        ciconfig.run_ci_install(tmp_path, targets={"precommit"})
    assert exc.value.code == errors.E_USAGE


def test_gitlab_append_preserves_existing_jobs(tmp_path):
    """Installing into an existing .gitlab-ci.yml must add the bounds job while preserving prior stages and jobs verbatim — never clobber a hand-maintained pipeline."""
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
    """A pre-existing `bounds` job in .gitlab-ci.yml must be reported skipped and left untouched — the installer never overwrites a job the user already customized."""
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
    """A single target must write only that file and leave the other two absent — `targets={action}` scopes the install precisely, not a kitchen-sink of all gates."""
    result = ciconfig.run_ci_install(tmp_path, targets={"action"})
    assert result["created"] == [".github/workflows/bounds.yml"]
    assert result["targets"] == ["action"]
    assert (tmp_path / ".github/workflows/bounds.yml").exists()
    assert not (tmp_path / ".pre-commit-config.yaml").exists()
    assert not (tmp_path / ".gitlab-ci.yml").exists()


def test_unknown_target_raises(tmp_path):
    """An unknown CI target must raise BoundsError E_USAGE — an unsupported platform fails fast with the stable usage code rather than silently installing nothing."""
    with pytest.raises(errors.BoundsError) as exc:
        ciconfig.run_ci_install(tmp_path, targets={"jenkins"})
    assert exc.value.code == errors.E_USAGE
