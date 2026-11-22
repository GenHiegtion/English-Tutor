"""Run lint, format check and tests, then remove generated caches.

Usage: uv run python scripts/check.py [pytest args...]
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(*cmd: str) -> int:
    print(f"$ {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=ROOT)


def main() -> int:
    code = (
        run("ruff", "check", "src", "tests", "scripts")
        or run("ruff", "format", "--check", "src", "tests", "scripts")
        or run("pytest", "-q", *sys.argv[1:])
    )
    run(sys.executable, "scripts/clean.py")
    return code


if __name__ == "__main__":
    sys.exit(main())
