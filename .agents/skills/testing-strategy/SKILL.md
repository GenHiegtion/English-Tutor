---
name: testing-strategy
description: Testing standard for Python projects — how much coverage is enough, when to write unit vs integration tests, test naming conventions, and how to use pytest fixtures. Use when writing new tests, deciding what to test, or reviewing test coverage for a Python project.
---

# Testing Strategy

## How much coverage is enough

- Aim for meaningful coverage of `core/` (business logic), not a raw percentage target. Every non-trivial branch (if/else, loop edge cases, error paths) in `core/` should have at least one test.
- `io/` (external calls, DB, file access) needs fewer unit tests and more integration/contract tests — mock the external dependency for unit tests, and have a small number of real integration tests behind a flag or marker (e.g. `@pytest.mark.integration`) that hit the real thing.
- Don't chase 100% coverage on trivial code (simple getters, `__repr__`, straightforward data classes). Spend the effort where logic can actually break.
- A new feature/module is not "done" until it has at least: one happy-path test, one test for the most likely failure mode, and one test for an edge case (empty input, None, boundary value).

## Unit vs integration tests

- **Unit test**: tests one function/class in isolation, no real network/DB/filesystem access, fast (milliseconds). Lives in `tests/<mirror-of-src-path>/test_<module>.py`.
- **Integration test**: exercises multiple components together, or talks to a real (or realistic, e.g. containerized) external dependency. Slower, and may need setup/teardown. Mark it explicitly (`@pytest.mark.integration`) so it can be skipped in fast local runs and run separately in CI.
- Default to unit tests. Only write an integration test when the thing being verified is the *integration itself* (e.g. "does our SQL actually run against real Postgres," "does our HTTP client correctly parse the real API's response shape").

## Naming conventions

- Test files: `test_<module_name>.py`, placed under `tests/` mirroring the `src/` path (e.g. `src/<package>/core/parser.py` → `tests/core/test_parser.py`).
- Test functions: `test_<unit_under_test>_<scenario>_<expected_outcome>`, e.g. `test_parse_config_missing_key_raises_value_error`.
- Group related tests for one function/class in one file; don't spread tests for the same unit across multiple files.

## Using pytest fixtures

- Put shared fixtures in `tests/conftest.py` (project-wide) or a `conftest.py` in the relevant subdirectory (scoped to that area), not duplicated in every test file.
- Prefer fixtures over `setUp`/`tearDown`-style classes; keep fixtures small and composable rather than one giant fixture that sets up everything.
- Use fixture scope deliberately: `function` (default) for anything mutable or test-specific, `module`/`session` only for expensive, read-only setup (e.g. a loaded ML model, a DB connection pool) that is safe to share.
- Use `pytest.mark.parametrize` for testing the same logic across multiple input/output pairs instead of copy-pasting near-identical test functions.
- Don't reach into real external services from a fixture used by unit tests — use fakes/mocks (`unittest.mock`, `pytest-mock`) or in-memory equivalents (e.g. an in-memory SQLite DB) instead.
