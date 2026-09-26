#!/usr/bin/env python3
"""Validate the GitHub workflow files before GitHub has to.

GitHub's workflow parser is stricter than YAML: a duplicated job key is silently
collapsed by most YAML loaders (last one wins) and then makes GitHub refuse the
whole file with "Invalid workflow file: ... is already defined", producing a run
with zero jobs.  That is a confusing failure to debug from the outside, so this
script checks for it locally, plus that every expected job is present.

Usage:
    python tools/check-workflows.py            # exit 0 when everything is fine
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

#: Job names each workflow must define, so a job cannot vanish silently.
EXPECTED_JOBS: dict[str, set[str]] = {
    ".github/workflows/ci.yml": {"test", "lint", "installer", "installer-system", "fallback"},
}


def _strict_loader() -> Any:
    """A SafeLoader that rejects duplicate mapping keys."""
    import yaml

    class StrictLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict:
        mapping: dict = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in mapping:
                raise yaml.YAMLError(f"duplicate key {key!r} (line {key_node.start_mark.line + 1})")
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    return StrictLoader


def check(path: Path, loader: Any) -> list[str]:
    import yaml

    try:
        relative = path.relative_to(REPO_ROOT).as_posix()
    except ValueError:  # a path outside the checkout (used by the tests)
        relative = str(path)
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    except yaml.YAMLError as exc:
        return [f"{relative}: invalid workflow: {exc}"]

    problems: list[str] = []
    if not isinstance(data, dict) or "jobs" not in data:
        return [f"{relative}: no jobs section"]

    actual = set(data["jobs"])
    for name, job in data["jobs"].items():
        if not isinstance(job, dict):
            problems.append(f"{relative}: job {name!r} is not a mapping")
            continue
        if "runs-on" not in job:
            problems.append(f"{relative}: job {name!r} has no runs-on")
        if not job.get("steps"):
            problems.append(f"{relative}: job {name!r} has no steps")

    expected = EXPECTED_JOBS.get(relative)
    if expected is not None and actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        detail = []
        if missing:
            detail.append(f"missing {missing}")
        if extra:
            detail.append(f"unexpected {extra}")
        problems.append(f"{relative}: job set differs: {', '.join(detail)}")

    if not problems:
        print(f"{relative}: ok ({len(actual)} jobs: {', '.join(sorted(actual))})")
    return problems


def main(argv: list[str]) -> int:
    try:
        loader = _strict_loader()
    except ImportError:
        print("pyyaml is required: python -m pip install pyyaml", file=sys.stderr)
        return 2

    paths = [Path(arg) for arg in argv] or sorted([*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")])
    if not paths:
        print(f"no workflow files found in {WORKFLOW_DIR}", file=sys.stderr)
        return 1

    problems: list[str] = []
    for path in paths:
        problems.extend(check(path, loader))
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
