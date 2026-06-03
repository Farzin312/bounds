"""Shared pytest fixtures."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent
SAMPLE_PROJECT = TESTS_DIR / "fixtures" / "sample_project"

_GIT_ENV = ["-c", "user.email=test@bounds.dev", "-c", "user.name=Bounds Test"]


def _strip_state(root: Path) -> None:
    state = root / ".bounds" / "state.json"
    if state.exists():
        state.unlink()


@pytest.fixture
def sample_project(tmp_path: Path) -> Path:
    """A pristine copy of the TypeScript sample project in a temp dir."""
    dst = tmp_path / "shop"
    shutil.copytree(SAMPLE_PROJECT, dst)
    _strip_state(dst)
    return dst


@pytest.fixture
def git_sample_project(sample_project: Path) -> Path:
    """The sample project as a committed git repo (for quick-mode tests)."""
    subprocess.run(["git", "init", "-q"], cwd=sample_project, check=True)
    subprocess.run(["git", *_GIT_ENV, "add", "-A"], cwd=sample_project, check=True)
    subprocess.run(["git", *_GIT_ENV, "commit", "-q", "-m", "init"], cwd=sample_project, check=True)
    return sample_project


@pytest.fixture
def py_project(tmp_path: Path) -> Path:
    """A minimal two-subsystem Python project: `models` (core) consumed by `svc` (service)."""
    root = tmp_path / "proj"
    (root / ".bounds" / "manifests").mkdir(parents=True)
    (root / ".bounds" / "root.yaml").write_text(
        'version: "1"\nproject: proj\nlanguages: [python]\nenforce: "off"\n'
        "subsystems: [models, svc]\n",
        encoding="utf-8",
    )
    (root / ".bounds" / "manifests" / "models.yaml").write_text(
        "name: models\nrole: library\ncriticality: core\npaths: [src/models]\n"
        "description: Core domain models.\n"
        "exposes:\n  - { name: Thing, kind: class }\nconsumes: []\n",
        encoding="utf-8",
    )
    (root / ".bounds" / "manifests" / "svc.yaml").write_text(
        "name: svc\nrole: service\ncriticality: leaf\npaths: [src/svc]\n"
        "description: Service layer over the models.\nexposes: []\n"
        "consumes:\n  - { subsystem: models, via: models, interfaces: [Thing] }\n",
        encoding="utf-8",
    )
    (root / "src" / "models").mkdir(parents=True)
    (root / "src" / "svc").mkdir(parents=True)
    (root / "src" / "models" / "thing.py").write_text("class Thing:\n    pass\n", encoding="utf-8")
    (root / "src" / "svc" / "main.py").write_text(
        "from ..models.thing import Thing\n\n\ndef run():\n    return Thing()\n", encoding="utf-8"
    )
    return root


@pytest.fixture
def git_init():
    """Return a helper that turns a directory into a committed git repo."""

    def _init(root: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", *_GIT_ENV, "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", *_GIT_ENV, "commit", "-q", "-m", "init"], cwd=root, check=True)

    return _init
