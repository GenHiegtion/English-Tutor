---
name: add-module
description: Add a new module/feature to an existing Python project following the project's structure standard, with matching tests and docs.
---

# /add-module

Use this workflow whenever the user asks to add a new module, feature, or file to an existing Python project (rather than scaffolding a brand-new one).

## Steps

1. **Clarify responsibility.** Before creating any file, determine which responsibility group the new code belongs to: `core` (business logic), `io` (external I/O — file/DB/API/queue), `config` (settings/config loading), or something else specific to this project's existing layout. If it's ambiguous, ask the user in one short question rather than guessing.
2. **Check the existing structure first.** Look at how the project is currently organized (it may already deviate from the `python-project-structure` skill if it was generated earlier by a different model). Follow the project's existing conventions for where things go, don't impose a new structure on top of an existing one without being asked.
3. **Create the file in the right place**, following:
   - the project's `python-project-structure` skill (directory layout, naming conventions),
   - the `coding-style-lint` rule (type hints, docstrings, formatting),
   - the `error-handling-logging` rule (logging, exception handling),
   - the `secrets-security` rule (no hard-coded secrets).
4. **Add a matching test** under `tests/`, mirroring the new file's path, following the `testing-strategy` skill (unit test for the happy path, one failure-mode test, one edge case).
5. **Update docs if needed.** If the project has a `docs/` folder or a README section describing its modules/architecture, add a short note about the new module. Don't create new doc files speculatively — only touch docs that already exist and are relevant.
6. **Summarize what was added**: which file(s) were created/changed, where the tests are, and anything the user should decide (e.g. a config value that needs setting, a dependency that needs installing).
