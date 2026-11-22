---
trigger: model_decision
description: Coding style and lint standard for Python files — type hints, docstrings, formatting, line length, no global mutable state.
---

# Coding Style & Lint

- Every function/method signature must have type hints for parameters and return value (use `typing`/`collections.abc` types, not bare `Any` unless truly dynamic).
- Every public function, class, and module must have a short docstring (1-3 lines): what it does, not how. Private helpers (`_name`) may skip it if the name is self-explanatory.
- Format code as `black` would (line length 88 unless the project's `pyproject.toml` says otherwise) and keep it `ruff`-clean (no unused imports, no unused variables, no bare `except:`).
- Max line length: follow the project's configured formatter; if none is configured, default to 88 characters.
- Avoid module-level mutable global state (mutable lists/dicts/sets at module scope). Pass state explicitly through function arguments, class instances, or a config/context object instead.
- Prefer explicit over implicit: no `import *`, no magic numbers without a named constant, no deeply nested conditionals (>3 levels) without extracting a helper function.
- When generating a new `.py` file, check whether the project already has a formatter/linter config (`pyproject.toml`, `.ruff.toml`, `setup.cfg`) and follow its settings instead of these defaults if they conflict.