---
name: project-structure
description: Directory and code organization standard for ANY Python project (chatbot, STT/TTS, data script, service, internal tool, etc. — not tied to a specific domain). Use when scaffolding a new Python project, or when adding a new module/feature/file to an existing Python project, so the structure stays consistent regardless of which model is doing the generation.
---

# Project Structure Standard

Goal: regardless of which model is generating code (Gemini, Claude, ...), the project always ends up with a clear, maintainable, extensible structure — instead of depending on the model to "invent" a structure every time.

Applies to **any kind of Python project**: data-processing scripts, services/APIs, CLI tools, AI projects (models, pipelines, agents, etc.). Do not assume a specific domain.

## Core principles

1. **Separate by responsibility, not by ad-hoc file names.** Each subdirectory under `src/` holds modules that share one responsibility (e.g. input/output, business logic, external integrations, configuration). Don't dump everything into one or two large files.
2. **Split by responsibility, not by line count.** Treat ~500 lines as a signal to check whether a file has taken on more than one responsibility, not as a hard limit. A cohesive file that does one thing (e.g. a model architecture definition, a large but single-purpose parser) can legitimately run 1000+ lines. Never split a file mechanically just to get under a number.
3. **Business logic is separated from I/O and from entrypoints.** Pure logic functions should not directly call `print`, read files, or hit external APIs — leave that to the layer that calls them. This keeps logic testable and reusable.
4. **Configuration and secrets are never hard-coded inside logic.** Put them in `config/` or environment variables (`.env`), read through a single central config module, instead of scattering `os.environ.get(...)` everywhere.
5. **Tests mirror the source structure.** The `tests/` directory mirrors `src/` (e.g. `src/core/parser.py` ↔ `tests/core/test_parser.py`).
6. **Non-Python files (Docker, shell scripts, markdown, CI config, etc.) have a fixed home**, never mixed into `src/`. See "Where non-Python files go" below.
7. **Don't create directories/files "just in case."** Only create a directory once there is real content to put in it. A small project (a one- or two-file script) doesn't need the full structure below — see "When to simplify."

## Reference directory structure

```
project-root/
├── src/
│   └── <package_name>/          # package name = project name, snake_case
│       ├── __init__.py
│       ├── core/                # core business logic, pure Python, no I/O dependencies
│       │   └── __init__.py
│       ├── io/                  # file read/write, API calls, DB, message queues, etc.
│       │   └── __init__.py
│       ├── config/               # load & validate configuration (e.g. pydantic BaseSettings)
│       │   └── __init__.py
│       ├── cli.py                 # CLI entrypoint (if applicable)
│       └── app.py                 # service/API entrypoint (if applicable, e.g. a FastAPI app)
├── tests/
│   ├── core/
│   ├── io/
│   └── conftest.py
├── scripts/                      # *.sh or standalone Python scripts run manually, not imported by the package
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml
├── docs/                         # supplementary markdown docs (design notes, internal guides, etc.)
├── .github/workflows/            # CI/CD if using GitHub Actions
├── pyproject.toml                # dependency & metadata management (preferred over requirements.txt)
├── .env.example
├── .gitignore
└── README.md
```

This is a **reference layout**, not a rigid mold — adjust the subdirectory names under `src/<package_name>/` to fit the project (e.g. a project with several distinct kinds of processing might add `pipelines/`, `models/`, `handlers/`, etc.), as long as the underlying principle holds: **separate by responsibility, don't split arbitrarily.**

## Where non-Python files go

| File type | Location |
|---|---|
| `Dockerfile`, `docker-compose.yml` | `docker/` (or project root if the project is very small and has a single Dockerfile) |
| Utility shell scripts (`*.sh`) | `scripts/` |
| Markdown docs (design notes, internal guides) | `docs/` — except `README.md`, which always stays at the project root |
| CI/CD config | `.github/workflows/` (or the equivalent directory for whichever CI platform is in use) |
| Environment config file | `.env.example` at the project root (never commit the real `.env`) |
| Notebooks (`.ipynb`) used for exploration/experiments | a dedicated `notebooks/` directory, never mixed into `src/` |

General rule: **the project root only holds project-level configuration files** (`pyproject.toml`, `README.md`, `.gitignore`, etc.); everything else gets its own directory by responsibility.

## Naming & import conventions

- Package, module, directory names: `snake_case`.
- Class names: `PascalCase`. Function/variable names: `snake_case`. Constants: `UPPER_SNAKE_CASE`.
- Use absolute imports within the package (`from <package_name>.core import parser`), avoid deep relative imports (`from ...x import y`).
- Never use `import *`.
- Every public module should have a short docstring stating its responsibility (1-3 lines at the top of the file is enough — no need for a long write-up).

## When to simplify

- A one-off script, used once and discarded, with no tests and no one else reading it later → this structure doesn't apply; a single `.py` file is fine.
- A small project (fewer than ~5 logic files) → `core/` and `io/` can be merged into one flat directory, but still keep `tests/`, `scripts/`, and `docker/` separate if those file types exist.
- Don't create `docker/` or `.github/workflows/` if the project doesn't use Docker or doesn't have CI — never create empty directories.

## When asked to add a feature/module to an existing project

1. Determine which responsibility group the new code belongs to (`core`, `io`, `config`, etc.) before creating any file.
2. Place the file in the matching directory; only create a new directory if that responsibility genuinely has no existing home.
3. Add the corresponding test under `tests/`, mirroring the structure.
4. If the project's current structure has already drifted from this standard (e.g. generated earlier by a different model), don't unilaterally restructure everything — apply this standard only to the new code, and suggest a refactor only if the user asks about it.