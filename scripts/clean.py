"""Remove generated caches (pytest, hypothesis, ruff, __pycache__).

Usage: uv run python scripts/clean.py [--all]   (--all also removes .venv)
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIRS = (".pytest_cache", ".hypothesis", ".ruff_cache", ".mypy_cache", "build", "dist")
SKIP_PARTS = {".venv", ".git"}


def main() -> None:
    targets = [ROOT / name for name in CACHE_DIRS]
    targets += [p for p in ROOT.rglob("__pycache__") if not SKIP_PARTS & set(p.parts)]
    targets += [p for p in ROOT.rglob("*.egg-info") if not SKIP_PARTS & set(p.parts)]
    if "--all" in sys.argv:
        targets.append(ROOT / ".venv")
    removed = 0
    for path in targets:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    print(f"Removed {removed} generated directories.")


if __name__ == "__main__":
    main()
