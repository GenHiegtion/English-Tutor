---
name: pre-commit-check
description: Run lint, tests, and a secrets-leak check before committing changes to a Python project.
---

# /pre-commit-check

Use this workflow right before committing changes, or when the user asks "is this ready to commit" / "check before I commit."

## Steps

1. **Lint.** Run the project's configured linter/formatter (`ruff check .` and `black --check .` if the project uses them; otherwise whatever is configured in `pyproject.toml`/`.pre-commit-config.yaml`). Fix any issues found rather than just reporting them, unless the user asked for a read-only check.
2. **Run tests.** Run the test suite (`pytest`), excluding slow/integration-marked tests by default (`pytest -m "not integration"`) unless the user asks for the full suite. Report failures clearly: which test, what assertion failed, likely cause.
3. **Check for leaked secrets.** Scan the staged diff (`git diff --cached` or the changed files) for patterns that look like hard-coded credentials: API keys, tokens, passwords, connection strings with embedded credentials, private key blocks (`BEGIN...PRIVATE KEY`). If a plausible secret is found, flag it and do not proceed to suggest a commit until it's resolved (moved to `.env`/config or confirmed to be a placeholder).
4. **Check `.gitignore` coverage.** Confirm that `.env`, `__pycache__/`, `.pytest_cache/`, and any local virtualenv/build artifacts are ignored and not staged for commit.
5. **Summarize the result**: lint status, test pass/fail count, whether any secrets were flagged, and whether it's safe to commit. If everything passes, this workflow does not run `git commit` itself unless the user explicitly asks it to — it reports readiness and lets the user (or a separate step) do the commit.
