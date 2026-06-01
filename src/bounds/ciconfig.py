"""CI gate generation: scaffold CI config so Bounds runs as a merge gate.

This module writes (or idempotently augments) the config files that wire Bounds
into common CI/CD systems, so structural drift is caught at the boundary where it
matters: before a change lands on the main branch.

Supported targets
------------------
- ``action``    -> ``.github/workflows/bounds.yml`` (GitHub Actions)
- ``precommit`` -> ``.pre-commit-config.yaml``      (pre-commit framework)
- ``gitlab``    -> ``.gitlab-ci.yml``               (GitLab CI)

Two deliberate design choices, both about *which* Bounds command runs where:

``bounds preflight --ci`` in remote CI (action + gitlab)
    Preflight is the strict gate. It does a full structural validation and blocks
    the pipeline on any blocking issue. Remote CI is allowed to be slow and
    thorough — it is the last line of defense before merge — so it pays for a full
    pass rather than a diff-based one.

``bounds validate --quick --ci`` in the local pre-commit hook
    The pre-commit hook fires on every local commit and must stay fast (the repo's
    performance budget keeps ``--quick`` sub-200ms): it diffs git, re-extracts only
    changed files, and propagates by manifest graph. A heavy full pass on every
    commit would be abandoned by developers; the quick path keeps the loop tight
    while the remote ``preflight`` provides the authoritative gate.

The GitHub Actions cache key
----------------------------
The ``actions/cache`` key is hashed over ``.bounds/root.yaml`` and
``.bounds/manifests/**`` — the *manifests*, deliberately NOT the branch ref or the
source tree. The Bounds extraction cache (``.bounds/cache.db``) is keyed by content
hash internally, so it stays valid across branches as long as the declared
structural surface (root + manifests) is unchanged. Hashing over the manifests
means a freshly-pushed feature branch reuses the cache main warmed up, instead of
extracting the whole tree from cold on every new branch. The cache only invalidates
when the manifests themselves change — exactly when re-extraction is actually needed.

The ``[skip bounds]`` convention
--------------------------------
By convention, a commit/PR whose message contains ``[skip bounds]`` should skip the
Bounds gate (e.g. an emergency hotfix). This is a *CI-side* convention: the generated
configs document it, but enforcing it is left to the CI provider's own commit-message
filtering. This module does not implement runner-side skip logic.

Determinism: this module emits no timestamps, randomness, or wall-clock data, sorts
all reported collections, and reports paths in POSIX form so output is byte-stable.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from bounds import errors

# Recognized targets. An empty request set means "all of these".
_ALL_TARGETS = ("action", "precommit", "gitlab")

# Relative paths (POSIX) each target writes to, under the project root.
_TARGET_PATHS = {
    "action": ".github/workflows/bounds.yml",
    "precommit": ".pre-commit-config.yaml",
    "gitlab": ".gitlab-ci.yml",
}

# ---- Generated file/templates -------------------------------------------------

# GitHub Actions workflow. The cache key hashes root.yaml + manifests (not the
# branch) so a new branch reuses main's warmed cache. Runs the strict preflight gate.
_ACTION_YML = """\
name: bounds
# Skip convention: include "[skip bounds]" in the PR/commit message to bypass this
# gate (enforced via CI-side commit-message filtering, not by bounds itself).
on:
  pull_request:
    paths: ['src/**', '.bounds/**']
  push:
    branches: [main]
    paths: ['src/**', '.bounds/**']
jobs:
  bounds:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/cache@v4
        with:
          path: .bounds/cache.db
          key: bounds-${{ hashFiles('.bounds/root.yaml', '.bounds/manifests/**') }}
      - run: pipx install bounds-cli
      # Freshness gate: flag NEW manifest-vs-source drift above the committed baseline.
      # Non-blocking by default (|| true) until you commit a baseline and trust the signal;
      # drop the `|| true` to make new drift fail the build.
      - run: bounds calibrate --check || true
      - run: bounds preflight --ci
"""

# The pre-commit hook block (a single `repo: local` entry). Fast quick-mode gate.
_PRECOMMIT_REPO = {
    "repo": "local",
    "hooks": [
        {
            "id": "bounds",
            "name": "bounds",
            "entry": "bounds validate --quick --ci",
            "language": "system",
            "pass_filenames": False,
            "stages": ["pre-commit"],
        }
    ],
}

# The GitLab CI job. Strict preflight gate, gated on relevant path changes.
_GITLAB_JOB = {
    "bounds": {
        "stage": "test",
        "image": "python:3.12-slim",
        "rules": [{"changes": ["src/**/*", ".bounds/**/*"]}],
        # The python:3.12-slim image has pip but NOT pipx; running as root, `pip install`
        # puts the `bounds` console script on PATH (/usr/local/bin), so use pip here (the
        # GitHub Actions job uses pipx, which its runners preinstall). Always `bounds-cli` —
        # the bare `bounds` name on PyPI is an unrelated package.
        "script": [
            "pip install bounds-cli",
            "bounds calibrate --check || true",  # freshness gate; drop `|| true` to enforce
            "bounds preflight --ci",
        ],
    }
}


def _dump_yaml(data: object) -> str:
    """Serialize ``data`` to deterministic YAML (no key sorting, block style)."""
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def _install_action(project_root: Path) -> bool:
    """Write the GitHub Actions workflow. Return True if created, False if skipped."""
    path = project_root / _TARGET_PATHS["action"]
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_ACTION_YML, encoding="utf-8")
    return True


def _has_bounds_precommit_hook(data: object) -> bool:
    """True when a parsed pre-commit config already declares a hook with id ``bounds``."""
    if not isinstance(data, dict):
        return False
    repos = data.get("repos")
    if not isinstance(repos, list):
        return False
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        hooks = repo.get("hooks")
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            if isinstance(hook, dict) and hook.get("id") == "bounds":
                return True
    return False


def _install_precommit(project_root: Path) -> bool:
    """Create or append the bounds hook to ``.pre-commit-config.yaml`` idempotently."""
    path = project_root / _TARGET_PATHS["precommit"]
    if not path.exists():
        path.write_text(_dump_yaml({"repos": [_PRECOMMIT_REPO]}), encoding="utf-8")
        return True

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if _has_bounds_precommit_hook(data):
        return False

    # Append to an existing top-level `repos:` list, preserving all prior content.
    if isinstance(data, dict) and isinstance(data.get("repos"), list):
        data["repos"].append(_PRECOMMIT_REPO)
    elif data is None:
        data = {"repos": [_PRECOMMIT_REPO]}
    else:
        # File exists but has no usable `repos:` list; fail loudly rather than
        # silently clobbering the developer's config.
        raise errors.BoundsError(
            errors.E_USAGE,
            f"{_TARGET_PATHS['precommit']} exists but has no top-level 'repos:' list.",
            fix="Add a 'repos:' list to the file, or remove it and re-run.",
        )
    path.write_text(_dump_yaml(data), encoding="utf-8")
    return True


def _install_gitlab(project_root: Path) -> bool:
    """Create or append the bounds job to ``.gitlab-ci.yml`` idempotently."""
    path = project_root / _TARGET_PATHS["gitlab"]
    if not path.exists():
        path.write_text(_dump_yaml(dict(_GITLAB_JOB)), encoding="utf-8")
        return True

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "bounds" in data:
        return False

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise errors.BoundsError(
            errors.E_USAGE,
            f"{_TARGET_PATHS['gitlab']} exists but is not a top-level mapping.",
            fix="Make the file a YAML mapping of jobs, or remove it and re-run.",
        )
    # Append our job, preserving all existing jobs/keys.
    data["bounds"] = _GITLAB_JOB["bounds"]
    path.write_text(_dump_yaml(data), encoding="utf-8")
    return True


_INSTALLERS = {
    "action": _install_action,
    "precommit": _install_precommit,
    "gitlab": _install_gitlab,
}


def run_ci_install(project_root: Path, *, targets: set[str]) -> dict:
    """Scaffold CI gate config for the requested ``targets`` under ``project_root``.

    ``targets`` is a subset of ``{"action", "precommit", "gitlab"}``; an empty set
    means "all three". For each target, the relevant file is created (or, where the
    format allows, appended to) unless it is already configured for Bounds — in which
    case the target is reported as skipped (the operation is idempotent).

    Returns a JSON-serializable dict::

        {"created": [...posix paths...], "skipped": [...], "targets": [...]}

    All lists are sorted; paths are POSIX-form and repo-relative.
    """
    if not targets:
        requested = list(_ALL_TARGETS)
    else:
        unknown = sorted(targets - set(_ALL_TARGETS))
        if unknown:
            raise errors.BoundsError(
                errors.E_USAGE,
                f"Unknown CI target(s): {', '.join(unknown)}.",
                fix=f"Choose from: {', '.join(_ALL_TARGETS)}.",
            )
        requested = sorted(targets)

    root = Path(project_root)
    created: list[str] = []
    skipped: list[str] = []

    for target in requested:
        rel = _TARGET_PATHS[target]
        if _INSTALLERS[target](root):
            created.append(rel)
        else:
            skipped.append(rel)

    return {
        "created": sorted(created),
        "skipped": sorted(skipped),
        "targets": sorted(requested),
    }
