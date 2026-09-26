"""Guard the CI workflow files.

GitHub rejects a workflow whose YAML has a duplicated job key, but PyYAML and
most editors silently keep the last definition -- which is exactly how a broken
`.github/workflows/ci.yml` reached `main` once.  `tools/check-workflows.py`
performs the strict parse, and this test makes the mistake fail in the normal
test run instead of on GitHub.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER = REPO_ROOT / "tools" / "check-workflows.py"


def _run_checker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_workflow_files_are_valid() -> None:
    result = _run_checker()
    if result.returncode == 2 and "pyyaml" in result.stderr:
        pytest.skip("pyyaml is not installed (pip install pyyaml)")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ci.yml: ok" in result.stdout


def test_duplicate_job_keys_are_detected(tmp_path: Path) -> None:
    """The checker must actually catch the failure it exists for."""
    broken = tmp_path / "broken.yml"
    broken.write_text(
        "name: broken\n"
        "on: push\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo one\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo two\n",
        encoding="utf-8",
    )
    result = _run_checker(str(broken))
    if result.returncode == 2 and "pyyaml" in result.stderr:
        pytest.skip("pyyaml is not installed (pip install pyyaml)")
    assert result.returncode == 1
    assert "duplicate key" in result.stderr
