#!/usr/bin/env bash
# Lint, format check and tests, then remove generated caches.
#
#   scripts/check.sh [pytest args...]   ruff check + ruff format --check + pytest, then clean
#   scripts/check.sh clean [--all]      only remove caches (--all also removes .venv)
#
# Caches are removed even when a check fails. Eval runs (eval_runs/) are never touched.
set -euo pipefail

cd "$(dirname "$0")/.."

clean() {
    local targets=(.pytest_cache .hypothesis .ruff_cache .mypy_cache build dist)
    if [[ "${1:-}" == "--all" ]]; then
        targets+=(.venv)
    fi
    local removed=0 path
    for path in "${targets[@]}"; do
        if [[ -e "$path" ]]; then
            rm -rf "$path"
            removed=$((removed + 1))
        fi
    done
    while IFS= read -r -d '' path; do
        rm -rf "$path"
        removed=$((removed + 1))
    done < <(find . \( -path ./.venv -o -path ./.git \) -prune -o -type d \
        \( -name __pycache__ -o -name '*.egg-info' \) -print0)
    echo "Removed $removed generated directories."
}

run() {
    echo "\$ $*"
    "$@"
}

if [[ "${1:-}" == "clean" ]]; then
    shift
    clean "$@"
    exit 0
fi

trap clean EXIT
run uv run ruff check src tests
run uv run ruff format --check src tests
run uv run pytest -q "$@"
