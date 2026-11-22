"""Development HTTP server with interactive API docs (Swagger UI at /docs).

This is a thin wrapper around TeacherService for manual testing and for front-end developers
who want to see the event stream. It is NOT the production path: in production the Talkwise
backend (and the voice bot) import ``ea_llm`` as a library and own authentication, persistence
and rate limits.

Run: ``uv run ea-llm serve`` then open http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, TypeAdapter

from ea_llm.adapters.memory import MemoryLearnerStore, load_seed_scenarios
from ea_llm.models import (
    AgeBand,
    CefrLevel,
    LearnerProfile,
    Persona,
    SessionSnapshot,
    StoredMessage,
)
from ea_llm.teacher.context import LearnerInput, ScenarioRef, TurnContext
from ea_llm.teacher.events import TeacherEvent
from ea_llm.teacher.runner import TeacherService

DESCRIPTION = """
Event stream of a teacher turn (`POST /sessions/{session_id}/turns`) uses Server-Sent Events.
Each message is `event: <kind>` + `data: <json>` where `kind` is one of
`segment`, `board`, `mistake`, `terminal`, `turn_end`, `turn_failed`.
The JSON shape of every event is published at `GET /schema/events`.
"""


class CreateSession(BaseModel):
    user_id: str = "dev-user"
    persona_id: str = "lan"
    mode: Literal["scenario", "free_chat"] = "scenario"
    scenario_id: str = Field(default="cafe-order", description="Used only in 'scenario' mode.")
    cefr: CefrLevel = "A2"
    age_band: AgeBand = "adult"
    display_name: str | None = None
    interests: list[str] = Field(default_factory=list)


class CreateTurn(BaseModel):
    text: str | None = Field(
        default=None,
        description="Learner message. Omit only for the opening turn of a 'scenario' session.",
    )
    turn_id: str | None = Field(default=None, description="Idempotency key; generated if omitted.")
    persona_id: str | None = Field(
        default=None, description="Set to switch teacher from this turn on."
    )


class Delivered(BaseModel):
    seq: int = Field(description="Highest segment seq the learner actually received.")


def create_app(service: TeacherService, learners: MemoryLearnerStore) -> FastAPI:
    app = FastAPI(title="EA-LLM dev server", version="0.1.0", description=DESCRIPTION)
    scenarios: dict[str, ScenarioRef] = {s.id: s for s in load_seed_scenarios()}
    event_adapter = TypeAdapter(TeacherEvent)

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/personas", tags=["catalog"])
    async def personas() -> list[Persona]:
        return await service.personas.list_personas()

    @app.get("/scenarios", tags=["catalog"])
    async def list_scenarios() -> list[ScenarioRef]:
        return list(scenarios.values())

    @app.get("/schema/events", tags=["meta"])
    async def event_schema() -> dict:
        """JSON Schema of every event the turn stream can emit."""
        return event_adapter.json_schema()

    @app.post("/sessions", tags=["sessions"], status_code=201)
    async def create_session(body: CreateSession) -> SessionSnapshot:
        scenario = None
        if body.mode == "scenario":
            scenario = scenarios.get(body.scenario_id)
            if scenario is None:
                raise HTTPException(404, f"unknown scenario: {body.scenario_id}")
        learners.put(
            LearnerProfile(
                user_id=body.user_id,
                display_name=body.display_name,
                cefr=body.cefr,
                age_band=body.age_band,
                interests=body.interests,
            )
        )
        try:
            return await service.open_session(
                session_id=uuid.uuid4().hex[:12],
                user_id=body.user_id,
                channel="text",
                mode=body.mode,
                persona_id=body.persona_id,
                scenario=scenario,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/sessions/{session_id}/messages", tags=["sessions"])
    async def messages(session_id: str) -> list[StoredMessage]:
        if await service.history.load_session(session_id) is None:
            raise HTTPException(404, "unknown session")
        return await service.history.load_messages(session_id)

    @app.post(
        "/sessions/{session_id}/turns",
        tags=["turns"],
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}, "description": "SSE event stream"}},
    )
    async def run_turn(session_id: str, body: CreateTurn) -> StreamingResponse:
        snapshot = await service.history.load_session(session_id)
        if snapshot is None:
            raise HTTPException(404, "unknown session")
        ctx = TurnContext(
            session_id=session_id,
            user_id=snapshot.user_id,
            turn_id=body.turn_id or uuid.uuid4().hex[:12],
            channel=snapshot.channel,
            mode=snapshot.mode,
            persona_id=body.persona_id or snapshot.persona_id,
            scenario=snapshot.scenario,
            learner_input=(
                LearnerInput(text=body.text, source="typed") if body.text is not None else None
            ),
            received_at=0.0,
        )

        async def stream() -> AsyncIterator[str]:
            async for event in service.run_turn(ctx):
                yield f"event: {event.kind}\ndata: {event.model_dump_json()}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/sessions/{session_id}/turns/{turn_id}/delivered", tags=["turns"], status_code=204)
    async def delivered(session_id: str, turn_id: str, body: Delivered) -> None:
        """Report how far the learner got, so a cancelled turn keeps only what was received."""
        service.mark_delivered(turn_id, body.seq)

    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn --factory``."""
    from ea_llm.bootstrap import build_service, configure_logging

    configure_logging()
    learners = MemoryLearnerStore()
    return create_app(build_service(learners=learners), learners)
