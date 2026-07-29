#!/usr/bin/env python3
"""Run the single local validation contract used by CI and GitHub Releases."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
GATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "generated public Skills",
        (
            sys.executable,
            "tools/sync_public_skills.py",
            "--check",
        ),
    ),
    (
        "high-signal static safety rules",
        (
            sys.executable,
            "-m",
            "ruff",
            "check",
            "skills",
            "tests",
            "tools",
        ),
    ),
    (
        "Python source compilation",
        (
            sys.executable,
            "-m",
            "compileall",
            "-q",
            "skills",
        ),
    ),
    (
        "regression tests",
        (
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-v",
        ),
    ),
    ("patch formatting", ("git", "diff", "--check")),
)


def _run_gate(label: str, command: Sequence[str]) -> int:
    print(f"\n==> Checking {label}", flush=True)
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            check=False,
        )
    except OSError as exc:
        print(f"Could not run {label}: {exc}", file=sys.stderr)
        return 2
    if completed.returncode != 0:
        print(
            f"Release gate failed at {label} (exit {completed.returncode}).",
            file=sys.stderr,
        )
    return completed.returncode


def main() -> int:
    for label, command in GATES:
        returncode = _run_gate(label, command)
        if returncode != 0:
            return returncode
    print("\nAll release gates passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
