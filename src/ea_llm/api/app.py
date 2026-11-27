"""EA-LLM HTTP service: the contract for the Talkwise backend, learners' browsers and Pipecat.

Production (Railway): ``uvicorn --factory ea_llm.api.app:production_app``. Local development:
``uv run ea-llm serve`` (in-memory stores, no authentication).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import TypeAdapter
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from ea_llm.adapters.memory import MemoryScenarioStore
from ea_llm.adapters.sql import (
    SqlHistoryStore,
    SqlLearnerStore,
    SqlPersonaStore,
    SqlScenarioStore,
    create_engine,
    ping,
)
from ea_llm.api.auth import API_KEY_HEADER, Authenticator, Caller
from ea_llm.api.errors import ApiError, error_responses, install_error_handlers
from ea_llm.api.logs import AccessLogMiddleware, configure_service_logging
from ea_llm.api.ratelimit import SlidingWindowLimiter
from ea_llm.api.schemas import (
    CreateSession,
    CreateTurn,
    Delivered,
    Health,
    MessageList,
    PersonaList,
    PersonaOut,
    ScenarioList,
    SessionEnded,
    SessionOut,
)
from ea_llm.api.settings import ApiSettings
from ea_llm.api.turns import TurnBusy, TurnStreams
from ea_llm.bootstrap import build_service
from ea_llm.models import LearnerProfile, SessionSnapshot
from ea_llm.ports import ScenarioStore
from ea_llm.teacher.context import LearnerInput, TurnContext
from ea_llm.teacher.display import to_display
from ea_llm.teacher.events import TeacherEvent
from ea_llm.teacher.runner import TeacherService

logger = logging.getLogger(__name__)

# A cold Neon compute in another region needs about 5 s for the first connection.
HEALTH_DB_TIMEOUT_S = 8.0
MINUTE_S = 60.0
HOUR_S = 3600.0

DESCRIPTION = """
Teacher service of Talkwise: text in, text out (audio stays in the voice bot).

**Authentication.** Backend services send `X-API-Key`. A learner's browser sends
`Authorization: Bearer <token>`, a short-lived HS256 JWT minted by the Talkwise backend
(claims `sub` = user id, `aud` = `ea-llm`, `iat`, `exp`, optional `sid` = the only session the
token may use). A learner can only reach their own sessions.

**Turn stream.** `POST /sessions/{session_id}/turns` answers with Server-Sent Events. Each
message is `event: <kind>` + `data: <json>`, with `kind` one of `segment`, `board`, `mistake`,
`terminal`, `turn_end`, `turn_failed`; the stream ends after `turn_end` or `turn_failed`. The
JSON shape of every event is published at `GET /schema/events`. Closing the stream cancels the
turn: report the last segment shown with `.../delivered` *before* closing, so only that part
is kept in the history.

**Errors.** Every non-2xx response has the body `{"error": {"code": ..., "message": ...}}`.
"""

api_key_scheme = APIKeyHeader(
    name=API_KEY_HEADER, auto_error=False, description="Backend services (Talkwise, Pipecat)."
)
bearer_scheme = HTTPBearer(auto_error=False, description="Learner token minted by Talkwise.")


@dataclass
class ServiceState:
    """Everything the routes need, built once per app."""

    service: TeacherService
    scenarios: ScenarioStore
    settings: ApiSettings
    auth: Authenticator
    turns: TurnStreams
    turn_limits: SlidingWindowLimiter
    session_limits: SlidingWindowLimiter
    database: DatabaseProbe | None
    require_known_learner: bool  # with the Talkwise database a session needs a real user row


class DatabaseProbe:
    """Pings the database for /health; a ping keeps running after the request stops waiting.

    Cancelling a slow first connection would throw it away, so every later check would start
    over and time out again. Letting it finish puts the connection in the pool, and a single
    ping at a time is shared by concurrent checks.
    """

    def __init__(self, engine: AsyncEngine, timeout_s: float = HEALTH_DB_TIMEOUT_S) -> None:
        self._engine = engine
        self._timeout_s = timeout_s
        self._ping: asyncio.Task[None] | None = None

    async def check(self) -> None:
        """Raise TimeoutError or the database error when the database does not answer in time."""
        if self._ping is None or self._ping.done():
            self._ping = asyncio.create_task(ping(self._engine))
            self._ping.add_done_callback(_mark_retrieved)
        async with asyncio.timeout(self._timeout_s):
            await asyncio.shield(self._ping)

    async def close(self) -> None:
        """Cancel a ping still running at shutdown."""
        if self._ping is not None and not self._ping.done():
            self._ping.cancel()
            await asyncio.gather(self._ping, return_exceptions=True)


def _mark_retrieved(task: asyncio.Task[None]) -> None:
    # A ping nobody waits for any more must not log "exception was never retrieved"; the
    # request that gave up on it already logged the timeout.
    if not task.cancelled():
        task.exception()


def get_state(request: Request) -> ServiceState:
    """The ServiceState of the running app."""
    return request.app.state.ea


StateDep = Annotated[ServiceState, Depends(get_state)]


async def get_caller(
    request: Request,
    state: StateDep,
    api_key: Annotated[str | None, Security(api_key_scheme)] = None,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Security(bearer_scheme)] = None,
) -> Caller:
    """Authenticate the request (401 otherwise) and tag the access log with the caller."""
    caller = state.auth.authenticate(api_key, bearer.credentials if bearer else None)
    request.state.caller = caller.name
    return caller


CallerDep = Annotated[Caller, Depends(get_caller)]
router = APIRouter()


@router.get("/health", tags=["meta"], responses=error_responses(503))
async def health(state: StateDep) -> Health:
    """Liveness and readiness for Railway: also checks the database when there is one."""
    if state.database is None:
        return Health(database="not_configured")
    try:
        await state.database.check()
    except (SQLAlchemyError, OSError, TimeoutError):
        logger.warning("health check: database unavailable", exc_info=True)
        raise ApiError(503, "database_unavailable", "database unavailable") from None
    return Health(database="ok")


@router.get("/schema/events", tags=["meta"])
async def event_schema() -> dict:
    """JSON Schema of every event the turn stream can emit."""
    return TypeAdapter(TeacherEvent).json_schema()


@router.get("/personas", tags=["catalog"], responses=error_responses(401))
async def list_personas(state: StateDep, _: CallerDep) -> PersonaList:
    """Published teachers a learner can choose."""
    personas = await state.service.personas.list_personas()
    return PersonaList(items=[PersonaOut.model_validate(p, from_attributes=True) for p in personas])


@router.get("/scenarios", tags=["catalog"], responses=error_responses(401))
async def list_scenarios(state: StateDep, _: CallerDep) -> ScenarioList:
    """Active role-play scenarios."""
    return ScenarioList(items=await state.scenarios.list_scenarios())


@router.post(
    "/sessions",
    tags=["sessions"],
    status_code=201,
    responses=error_responses(401, 403, 404, 422, 429),
)
async def create_session(body: CreateSession, state: StateDep, caller: CallerDep) -> SessionOut:
    """Open a session: teacher, mode and learner profile are frozen for its whole length."""
    user_id = _session_owner(body, caller)
    _limit(state.session_limits, user_id)
    service = state.service
    scenario = None
    if body.scenario_id is not None:
        scenario = await state.scenarios.get_scenario(body.scenario_id)
        if scenario is None:
            raise ApiError(404, "scenario_not_found", f"unknown scenario: {body.scenario_id}")
    persona = await service.personas.get_persona(body.persona_id)
    if persona is None or persona.status != "published":
        raise ApiError(404, "persona_not_found", f"unknown teacher: {body.persona_id}")
    profile = await service.learners.get_profile(user_id)
    if profile is None:
        if state.require_known_learner:
            raise ApiError(404, "user_not_found", "unknown or suspended user")
        profile = LearnerProfile(user_id=user_id)
    if body.learner is not None:
        profile = body.learner.apply(profile)
    snapshot = await service.open_session(
        session_id=str(uuid.uuid4()),
        user_id=user_id,
        channel="text",
        mode=body.mode,
        persona_id=persona.id,
        scenario=scenario,
        learner=profile,
    )
    return SessionOut.of(snapshot)


@router.post(
    "/sessions/{session_id}/turns",
    tags=["turns"],
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "SSE event stream"},
        **error_responses(401, 404, 409, 422, 429),
    },
)
async def run_turn(
    session_id: str, body: CreateTurn, state: StateDep, caller: CallerDep
) -> StreamingResponse:
    """Run one teacher turn and stream its events (409 while another turn of it runs)."""
    snapshot = await _session_for(state, caller, session_id)
    if await state.service.history.is_closed(session_id):
        raise ApiError(409, "session_closed", "the session has ended")
    _limit(state.turn_limits, snapshot.user_id)
    ctx = TurnContext(
        session_id=session_id,
        user_id=snapshot.user_id,
        turn_id=body.turn_id or uuid.uuid4().hex,
        channel=snapshot.channel,
        mode=snapshot.mode,
        persona_id=body.persona_id or snapshot.persona_id,
        scenario=snapshot.scenario,
        learner_input=(
            LearnerInput(text=body.text, source="typed") if body.text is not None else None
        ),
        received_at=time.time(),
    )
    try:
        events = await state.turns.start(ctx)
    except TurnBusy:
        raise ApiError(409, "turn_in_progress", "another turn of this session is running") from None
    return StreamingResponse(
        _sse(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/sessions/{session_id}/turns/{turn_id}/delivered",
    tags=["turns"],
    status_code=204,
    response_class=Response,
    responses=error_responses(401, 404, 422),
)
async def delivered(
    session_id: str, turn_id: str, body: Delivered, state: StateDep, caller: CallerDep
) -> Response:
    """Report how far the learner got, so a cancelled turn keeps only what was received.

    Ignored once the turn has finished (nothing left to trim).
    """
    await _session_for(state, caller, session_id)
    if state.turns.is_running(session_id, turn_id):
        state.service.mark_delivered(turn_id, body.seq)
    return Response(status_code=204)


@router.get(
    "/sessions/{session_id}/messages",
    tags=["sessions"],
    responses=error_responses(401, 404),
)
async def list_messages(session_id: str, state: StateDep, caller: CallerDep) -> MessageList:
    """The conversation as the learner saw it, oldest first."""
    await _session_for(state, caller, session_id)
    stored = await state.service.history.load_messages(session_id)
    return MessageList(items=[shown for m in stored if (shown := to_display(m)) is not None])


@router.post(
    "/sessions/{session_id}/end",
    tags=["sessions"],
    responses=error_responses(401, 404),
)
async def end_session(session_id: str, state: StateDep, caller: CallerDep) -> SessionEnded:
    """Close the session (idempotent); no more turns are accepted."""
    await _session_for(state, caller, session_id)
    await state.service.history.close_session(session_id)
    return SessionEnded(session_id=session_id)


def create_app(
    service: TeacherService,
    scenarios: ScenarioStore,
    settings: ApiSettings,
    *,
    engine: AsyncEngine | None = None,
    require_known_learner: bool = False,
) -> FastAPI:
    """The FastAPI app over a ready TeacherService; ``engine`` is disposed on shutdown."""
    turns = TurnStreams(service, settings.turn_lock_wait_s)
    database = DatabaseProbe(engine) if engine is not None else None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await turns.shutdown()
        if database is not None:
            await database.close()
        if engine is not None:
            await engine.dispose()

    app = FastAPI(
        title="EA-LLM",
        version="0.1.0",
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.ea = ServiceState(
        service=service,
        scenarios=scenarios,
        settings=settings,
        auth=Authenticator(settings),
        turns=turns,
        turn_limits=SlidingWindowLimiter(settings.turns_per_minute, MINUTE_S),
        session_limits=SlidingWindowLimiter(settings.sessions_per_hour, HOUR_S),
        database=database,
        require_known_learner=require_known_learner,
    )
    install_error_handlers(app)
    app.include_router(router)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )
    app.add_middleware(AccessLogMiddleware)
    return app


def production_app() -> FastAPI:
    """Entry point for ``uvicorn --factory``: Talkwise database, real models, authentication."""
    load_dotenv()
    configure_service_logging()
    settings = ApiSettings.from_env()
    settings.validate_for_production()
    assert settings.database_url is not None  # checked by validate_for_production
    engine = create_engine(settings.database_url)
    service = build_service(
        history=SqlHistoryStore(engine),
        personas=SqlPersonaStore(engine),
        learners=SqlLearnerStore(engine),
    )
    return create_app(
        service, SqlScenarioStore(engine), settings, engine=engine, require_known_learner=True
    )


def dev_app(*, offline: bool = False) -> FastAPI:
    """Local server: in-memory stores and seed data, no authentication. Never deploy this."""
    service = build_service(offline=offline)
    settings = ApiSettings(auth_disabled=True)
    return create_app(service, MemoryScenarioStore.from_seed(), settings)


def _session_owner(body: CreateSession, caller: Caller) -> str:
    """The user the new session belongs to; learners can only open sessions for themselves."""
    if caller.kind == "service":
        if body.user_id is None:
            raise ApiError(422, "invalid_request", "user_id is required with an API key")
        return body.user_id
    assert caller.user_id is not None
    if body.learner is not None:
        raise ApiError(403, "forbidden", "only backend services may override the profile")
    if caller.session_id is not None:
        raise ApiError(403, "forbidden", "this token is limited to one session")
    if body.user_id not in (None, caller.user_id):
        raise ApiError(403, "forbidden", "a learner can only open their own sessions")
    return caller.user_id


async def _session_for(state: ServiceState, caller: Caller, session_id: str) -> SessionSnapshot:
    """The session if the caller may use it; 404 otherwise (other users' ids are not revealed)."""
    snapshot = await state.service.history.load_session(session_id)
    if snapshot is None or not caller.can_access(snapshot):
        raise ApiError(404, "session_not_found", "unknown session")
    return snapshot


def _limit(limiter: SlidingWindowLimiter, key: str) -> None:
    if (wait := limiter.hit(key)) is not None:
        raise ApiError(429, "rate_limited", "too many requests", headers={"Retry-After": str(wait)})


async def _sse(events: AsyncGenerator[TeacherEvent]) -> AsyncIterator[str]:
    async with aclosing(events) as stream:  # closing it cancels the turn if it is still running
        async for event in stream:
            yield f"event: {event.kind}\ndata: {event.model_dump_json()}\n\n"
