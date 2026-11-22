# EA-LLM

LLM module for the Talkwise English-learning app. A Vietnamese-speaking teacher persona teaches
English **in Vietnamese**; the module is a Python library (not a server) that the web backend and
the voice bot import.

- **Text in, text out.** The module never touches audio, a database driver or a web framework.
- **Channels:** `text` (implemented), `voice` (planned: parser and tool schemas are ready, the
  voice prompt is not). **Modes:** `scenario` and `free_chat` (implemented), `lesson` (planned).
- **Teachers:** several personas (age, gender, personality); pronouns adapt to the learner's age.
- **Models:** all calls go through [OpenRouter](https://openrouter.ai) with the Anthropic SDK
  (Haiku 5.5 primary, GPT-6 Luna as hedged fallback).

## Quick start

```bash
uv sync --extra server          # install everything, including the dev server
cp .env.example .env            # then set OPENROUTER_API_KEY
uv run ea-llm chat              # scenario lesson in the terminal (default: cafe-order)
uv run ea-llm chat --mode free_chat --interests football,music
uv run ea-llm chat --list       # teachers and scenarios
uv run ea-llm chat --offline    # no network, canned replies
uv run ea-llm serve             # HTTP dev server, interactive docs at http://127.0.0.1:8000/docs
```

`uv run ea-llm chat --help` lists every option (teacher, CEFR level, age band, name, ...).

### Environment

| Variable | Required | Used by | Meaning |
| --- | --- | --- | --- |
| `OPENROUTER_API_KEY` | yes, except `--offline` | library | OpenRouter key for every LLM call |
| `EA_LLM_<ROLE>_MODEL` | no | library | override the primary model of a role (`TEACHER`, `EXAMINER`, `REPORTER`, `WRITER`) |
| `EA_LLM_<ROLE>_FALLBACK_MODEL` | no | library | override the hedged fallback; `none` disables it |
| `EA_LLM_LOG_LEVEL` | no (default `WARNING`) | CLI, dev server | level of the `ea_llm` loggers |

Model ids are OpenRouter ids (`vendor/model`); unset or empty keeps the defaults in
`src/ea_llm/config.py`. Token limits, effort and timeouts stay in code because they are tuned
with the prompts. The library reads these variables from the process environment; only the CLI
and dev server load `.env`, so a host application sets them in its own environment (e.g. Render).

The library never configures logging; a host application sets up its own handlers for the
`ea_llm` logger. Database settings do not belong here: the module reaches data only through the
ports below, and the host application owns the connection.

Run `uv run python scripts/check.py` after changes: it lints, runs the tests and then removes
generated caches (`.pytest_cache`, `.hypothesis`, `__pycache__`, ...). Use
`uv run python scripts/clean.py` to only clean.

## How it is used

```python
from ea_llm.providers.openrouter_provider import OpenRouterProvider
from ea_llm.teacher.runner import TeacherService

service = TeacherService(
    primary=OpenRouterProvider(),
    fallback=OpenRouterProvider().with_model("openai/gpt-6-luna"),
    history=..., personas=..., learners=...,        # port implementations (see below)
)
await service.open_session(session_id=..., user_id=..., channel="text", mode="scenario",
                           persona_id="lan", scenario=scenario)
async for event in service.run_turn(turn_context):   # OutputSegment, BoardUpdate, ...
    ...
```

Events are `OutputSegment` (a `<vi>`/`<en>` piece of speech), `BoardUpdate`, `MistakeNoted`,
`TerminalAction`, `TurnEnd` and `TurnFailed`. Their JSON schemas are served at `/schema/events`
by the dev server. To cancel a turn, close the async generator; call
`service.mark_delivered(turn_id, seq)` first so only segments the learner actually received are
kept in the history.

### Ports

The module reads and writes data only through `ea_llm.ports` (`HistoryStore`, `PersonaStore`,
`LearnerStore`). `ea_llm.adapters.memory` implements them in memory for development and tests;
the backend provides real implementations (SQLAlchemy) when merging. Rules for every
implementation:

1. All methods are `async`; run blocking drivers in a thread.
2. Never hold a DB connection while waiting for the LLM; open a short session per call.
3. Writes are idempotent on their natural key (`turn_id`), so retries are safe.

### HTTP API

`ea_llm.devserver` (FastAPI, `serve` extra) exposes the service for manual testing and for
front-end developers: sessions, SSE turn streams, catalog and event schemas, with Swagger UI at
`/docs`. It is a development tool. In production the Talkwise backend owns the public API,
authentication and persistence.

## Layout

```
src/ea_llm/
  providers/      LLM contracts (base.py) and the OpenRouter adapter
  prompts/        versioned prompt templates, registry (prompt_version) and builder
  teacher/        parser, tools, guards, history, hedged streaming, runner, personas
  adapters/       in-memory port implementations
  data/           seed teachers and scenarios (mirror the DB tables)
  evals/          latency benchmark
  cli.py          terminal chat and `serve`
  devserver.py    FastAPI dev server
  testing.py      FakeProvider for tests and offline mode
tests/            unit and integration tests (no network)
scripts/          clean.py, check.py
```

## Design notes

- **Prompt caching:** tools + static rules (1 h) are shared by all users of a channel/mode; the
  per-session block (1 h) is frozen at session open; the newest user message carries a 5 min
  breakpoint. History is trimmed in blocks so the cached prefix stays stable.
- **Streaming parser:** `LangTagParser` is chunk-invariant (property-tested): the output never
  depends on where the stream was split.
- **Latency:** if the primary has no token after 2.5 s (or fails), the fallback starts in
  parallel and the first provider with content wins; give up after 6 s.
- **Guards:** blocking per-segment checks (markdown, emoji, spoken scores) and flag-only checks
  after the turn (pronouns, number of corrections, Vietnamese ratio, CEFR overshoot, ...).
