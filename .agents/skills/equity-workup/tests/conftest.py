import shutil
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = TESTS_DIR.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
for path in (str(TESTS_DIR), str(SCRIPTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import fixtures  # noqa: E402


@pytest.fixture(scope="session")
def template() -> Path:
    assert fixtures.TEMPLATE.exists(), "original Word template is missing"
    return fixtures.TEMPLATE


@pytest.fixture(scope="session")
def built_run(tmp_path_factory) -> Path:
    run_dir = tmp_path_factory.mktemp("run")
    report = fixtures.build_run(run_dir)
    assert report["passed"], report["errors"]
    return run_dir


@pytest.fixture()
def run_copy(built_run, tmp_path) -> Path:
    target = tmp_path / "run"
    shutil.copytree(built_run, target)
    return target
