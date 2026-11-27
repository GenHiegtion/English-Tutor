# English Tutor

AI teacher service of an English-learning app (currently Talkwise). A Vietnamese-speaking
teacher persona teaches English **in Vietnamese**. English Tutor is the only AI core: a FastAPI
service (deployed on Railway) that the app backend and browsers call for text chat, and the
Pipecat voice bot calls for voice. The app itself holds no AI code.

- **Text in, text out.** Audio, STT and TTS stay in the voice bot.
- **Channels:** `text` (implemented), `voice` (planned: parser and tool schemas are ready, the
  voice prompt is not). **Modes:** `scenario` and `free_chat` (implemented), `lesson` (planned).
- **Teachers:** several personas (age, gender, personality); pronouns adapt to the learner's age.
- **Models:** all calls go through [OpenRouter](https://openrouter.ai) with the Anthropic SDK
  (Haiku 5.5 primary, GPT-6 Luna as hedged fallback).
- **Data:** the app's Postgres database (Neon). The app's Alembic migrations own the schema;
  English Tutor never creates or migrates tables.

## Quick start

```bash
uv sync                                # install everything
cp .env.example .env                   # then set OPENROUTER_API_KEY
uv run english-tutor chat              # scenario lesson in the terminal (default: cafe-order)
uv run english-tutor chat --mode free_chat --interests football,music
uv run english-tutor chat --list       # teachers and scenarios
uv run english-tutor chat --offline    # no network, canned replies
uv run english-tutor serve             # local server, docs at http://127.0.0.1:8000/docs
```

`uv run english-tutor chat --help` lists every option (teacher, CEFR level, age band, name, ...).
`english-tutor serve` uses in-memory stores and seed data and needs no credentials; it is for local
development only (`--offline` also skips OpenRouter).

### Environment

| Variable | Required | Meaning |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | yes, except `--offline` | OpenRouter key for every LLM call |
| `ENGLISH_TUTOR_<ROLE>_MODEL` | no | override the primary model of a role (`TEACHER`, `EXAMINER`, `REPORTER`, `WRITER`) |
| `ENGLISH_TUTOR_<ROLE>_FALLBACK_MODEL` | no | override the hedged fallback; `none` disables it |
| `ENGLISH_TUTOR_LOG_LEVEL` | no | level of the `english_tutor` loggers (CLI default `WARNING`, service `INFO`) |
| `DATABASE_URL` | service, eval export | app Postgres URL (see "Database role" below) |
| `ENGLISH_TUTOR_BACKEND_API_KEY` | service, one credential at least | key of the app backend |
| `ENGLISH_TUTOR_PIPECAT_API_KEY` | service, one credential at least | key of the voice bot |
| `ENGLISH_TUTOR_TOKEN_SECRET` | service, one credential at least | HS256 secret shared with the app backend for learner tokens |
| `ENGLISH_TUTOR_CORS_ORIGINS` | no | comma-separated web origins allowed to call the service |
| `ENGLISH_TUTOR_DOCS` | no | `off` hides `/docs` and `/openapi.json` |
| `ENGLISH_TUTOR_TURNS_PER_MINUTE` / `ENGLISH_TUTOR_SESSIONS_PER_HOUR` | no | per-learner limits (20 / 30) |

Model ids are OpenRouter ids (`vendor/model`); unset or empty keeps the defaults in
`src/english_tutor/config.py`. Token limits, effort and timeouts stay in code because they are tuned
with the prompts. API keys and the token secret need at least 32 characters
(`openssl rand -hex 32`), and each caller gets its own key. `.env` is loaded when present;
on Railway the variables come from the service settings.

Run `scripts/check.sh` after changes: it lints, checks formatting, runs the tests (extra
arguments go to pytest) and then removes generated caches (`.pytest_cache`, `.hypothesis`,
`__pycache__`, ...), even when a check fails. `scripts/check.sh clean` only cleans (`--all` also
removes `.venv`).

## HTTP API

The OpenAPI docs at `/docs` (and `/openapi.json`) are the contract for both callers.

| Endpoint | What it does | Callers |
| --- | --- | --- |
| `GET /health` | liveness and readiness (checks the database) | Railway |
| `GET /personas` | published teachers | web, bot |
| `GET /scenarios` | active role-play scenarios | web, bot |
| `POST /sessions` | open a session (teacher, mode, scenario); the learner profile comes from `users` | web, bot |
| `POST /sessions/{id}/turns` | one teacher turn, streamed as Server-Sent Events | web, bot |
| `POST /sessions/{id}/turns/{turn_id}/delivered` | last segment the learner received | bot, web |
| `GET /sessions/{id}/messages` | the conversation as the learner saw it | web |
| `POST /sessions/{id}/end` | close the session | web, bot |
| `GET /schema/events` | JSON Schema of the stream events | developers |

**Authentication.** Backend services send `X-API-Key` and name the `user_id` themselves; they
may also override profile fields for a session (`learner`). Browsers send
`Authorization: Bearer <token>`: an HS256 JWT minted by the app backend with
`ENGLISH_TUTOR_TOKEN_SECRET`, claims `sub` (user id), `aud` = `english-tutor`, `iat`, `exp` (at most one hour
after `iat`) and optionally `sid` (limits the token to one session). A learner only reaches
their own sessions; other ids answer 404.

**Turn stream.** Each SSE message is `event: <kind>` + `data: <json>`, with `kind` one of
`segment`, `board`, `mistake`, `terminal`, `turn_end`, `turn_failed`. Closing the stream
cancels the turn; call `delivered` first so only what the learner received is kept. One turn
runs at a time per session: a new turn waits up to 5 s for the previous one (a barge-in), then
gets 409 `turn_in_progress`.

**Errors.** Every non-2xx response is `{"error": {"code": "...", "message": "..."}}` (validation
errors add `details` with field locations, never the rejected input).

### Deploying on Railway

`railway.json` sets the start command (`python -m english_tutor.api`, listening on `$PORT`), the
`/health` check and **one replica**: turn locks, `delivered` positions and rate limits live in
the process. Set the service variables above, then generate a domain; the docs are at
`https://<name>.up.railway.app/docs`. Logs are JSON lines on stdout, one access line per
request (method, route, status, duration, caller), with no query strings or bodies.

### Database role

The service reads `users`, `teacher_personas` and `scenarios`, and writes `sessions`,
`messages` and `ai_usage_logs` (columns from app migration `0030`). Give it its own Neon
role with only those rights rather than the app's owner role. Neon's pooled endpoint works:
psycopg runs without server-side prepared statements.

```bash
uv run python scripts/check_db.py                 # read-only: role, columns, rights, teachers
uv run python scripts/seed_personas.py            # dry run: what would change in teacher_personas
uv run python scripts/seed_personas.py --apply    # copy src/english_tutor/data/personas.yaml into it
```

`check_db.py` exits with 1 when the service could not run on that database (a missing column or
right, no published teacher). `seed_personas.py` inserts new teachers and updates changed ones
(raising `version`); it never touches `voice`, `avatar_key` or teachers missing from the YAML.
The service role cannot write `teacher_personas`, so pass `--url-env NAME` with an owner URL.

## Teacher core

The HTTP layer is a thin shell around `TeacherService`, which stays free of web and database
code so tests need no network:

```python
from english_tutor.providers.openrouter_provider import OpenRouterProvider
from english_tutor.teacher.runner import TeacherService

service = TeacherService(
    primary=OpenRouterProvider(),
    fallback=OpenRouterProvider().with_model("openai/gpt-6-luna"),
    history=...,
    personas=...,
    learners=...,  # port implementations (see below)
)
await service.open_session(
    session_id=...,
    user_id=...,
    channel="text",
    mode="scenario",
    persona_id="lan",
    scenario=scenario,
)
async for event in service.run_turn(turn_context):  # OutputSegment, BoardUpdate, ...
    ...
```

Events are `OutputSegment` (a `<vi>`/`<en>` piece of speech), `BoardUpdate`, `MistakeNoted`,
`TerminalAction`, `TurnEnd` and `TurnFailed`. To cancel a turn, close the async generator; call
`service.mark_delivered(turn_id, seq)` first so only segments the learner actually received are
kept in the history.

### Ports

The core reads and writes data only through `english_tutor.ports` (`HistoryStore`, `PersonaStore`,
`LearnerStore`, `ScenarioStore`). `english_tutor.adapters.sql` implements them on the app database
(SQLAlchemy async, psycopg 3); `english_tutor.adapters.memory` implements them in memory for the CLI,
the local server, evals and tests. Rules for every implementation:

1. All methods are `async`; run blocking drivers in a thread.
2. Never hold a DB connection while waiting for the LLM; open a short session per call.
3. Writes are idempotent on their natural key (`turn_id`), so retries are safe.

## Evals

Evals send hand-written cases through the real teacher (`TeacherService` with the configured
model on OpenRouter, no fallback) and grade the replies in two layers:

1. **Rule checks** (free, deterministic): clean output, pronouns, the question the learner saw,
   required corrections, no correction of correct sentences, Vietnamese share, length, forbidden
   phrases. Defined per case in `src/english_tutor/evals/cases/teacher/*.yaml`.
2. **LLM judge** (rubric 1-5): `run` writes `judge_prompt.md`, a self-contained file with fixed
   instructions (`src/english_tutor/evals/judge_instructions.md`) and every case. Any capable model reads
   it and writes `judge_scores.jsonl` next to it; `report` validates and merges the scores.

```bash
uv run python -m english_tutor.evals.run_eval run                       # 50 cases, about $0.02
uv run python -m english_tutor.evals.run_eval run --filter fc-          # a subset
# let a judge write eval_runs/<run>/judge_scores.jsonl, then:
uv run python -m english_tutor.evals.run_eval report eval_runs/<run> --baseline eval_runs/<older run>
```

Runs land in `eval_runs/` (gitignored). `report` re-applies the current rule checks, so an older
baseline stays comparable; with `--baseline` it lists cases that newly fail or lost a judge point.
Each case runs once, so a single run can move by a case or two from model randomness.

New cases can be drafted from real sessions with a read-only export (reads
`DATABASE_URL`, sends only SELECTs):

```bash
uv run python -m english_tutor.evals.db_export --limit 200
```

It writes `exports/<time>/` (gitignored): masked conversations, the public scenarios and one draft
case per learner line. Only mode, channel, scenario, teacher, CEFR level, age band, message text
and the old engine's corrections leave the database; names, contacts and account data do not,
learners under 18 and suspended accounts are skipped, emails, links, long numbers and the
learner's name are masked, and session ids become hashes salted per export. Drafts never run as
cases until someone writes their `expect` block and moves them into `evals/cases/teacher/`.

## Layout

```
src/english_tutor/
  providers/      LLM contracts (base.py) and the OpenRouter adapter
  prompts/        versioned prompt templates, registry (prompt_version) and builder
  teacher/        parser, tools, guards, history, hedged streaming, runner, personas
  api/            FastAPI service: routes, auth, settings, turn streams, rate limits, JSON logs
  adapters/       port implementations: app database (sql.py) and in-memory
  data/           seed teachers and scenarios (mirror the DB tables), prices, blocked terms
  pricing.py      price table and cost estimates for calls without a reported cost
  evals/          eval cases, rule checks + judge handoff (run_eval), latency/cost benchmark
  cli.py          terminal chat and `serve` (local server)
  testing.py      FakeProvider for tests and offline mode
tests/            unit and integration tests (no network; the SQL adapter runs on SQLite)
scripts/          check.sh (lint, format, tests, clean), build_wordlist.py
```

## Design notes

- **Prompt caching:** tools + static rules (1 h) are shared by all users of a channel/mode; the
  per-session block (1 h) is frozen at session open; the newest user message carries a 5 min
  breakpoint. History is trimmed in blocks so the cached prefix stays stable.
- **Streaming parser:** `LangTagParser` is chunk-invariant (property-tested): the output never
  depends on where the stream was split.
- **Latency:** if the primary has no token after 2.5 s (or fails), the fallback starts in
  parallel and the first provider with content wins; give up after 6 s.
- **Cost:** every LLM call is a usage row (`TurnRecord.usage`). OpenRouter's reported cost wins;
  otherwise `data/pricing.yaml` prices it. A call cut off before its usage arrived (the losing
  side of a hedge, or a turn the learner interrupted) gets an estimated row with `cancelled`.
- **Guards:** blocking per-segment checks (blocked terms, markdown, emoji, spoken scores) and
  flag-only checks after the turn (pronouns, number of corrections, Vietnamese ratio, CEFR
  overshoot, numbers without a source, ...). A blocked term drops the rest of the turn and sends
  a fixed "back to the lesson" line. The CEFR guard also checks English words against
  `data/cefr_wordlist.yaml` (more than 15% above the learner's level + 1 raises the flag).
- **CEFR word list:** generated by `scripts/build_wordlist.py` from the CEFR-J Vocabulary
  Profile 1.5 (A1-B2; free for commercial use with citation) and the Octanove Vocabulary Profile
  C1/C2 (CC BY-SA 4.0, so the derived list is shared under the same licence), via
  [Open Language Profiles](https://github.com/openlanguageprofiles/olp-en-cefrj). The script adds
  irregular forms and contractions; change the script, not the YAML.
