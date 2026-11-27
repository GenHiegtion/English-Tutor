import pytest

from english_tutor.adapters.memory import (
    MemoryHistoryStore,
    MemoryLearnerStore,
    MemoryPersonaStore,
    load_seed_scenarios,
)
from english_tutor.models import LearnerProfile
from english_tutor.teacher.context import LearnerInput, ScenarioRef, TurnContext
from english_tutor.teacher.runner import TeacherConfig, TeacherService


@pytest.fixture
def scenario() -> ScenarioRef:
    return load_seed_scenarios()[0]


@pytest.fixture
def stores():
    return MemoryHistoryStore(), MemoryPersonaStore.from_seed(), MemoryLearnerStore()


def make_service(stores, primary, fallback=None, **cfg):
    history, personas, learners = stores
    return TeacherService(
        primary=primary,
        fallback=fallback,
        history=history,
        personas=personas,
        learners=learners,
        config=TeacherConfig(**cfg) if cfg else None,
    )


def turn_ctx(
    turn_id,
    text,
    *,
    session_id="s1",
    user_id="u1",
    persona_id="lan",
    scenario=None,
    mode="scenario",
):
    return TurnContext(
        session_id=session_id,
        user_id=user_id,
        turn_id=turn_id,
        channel="text",
        mode=mode,
        persona_id=persona_id,
        scenario=scenario,
        learner_input=LearnerInput(text=text, source="typed") if text is not None else None,
        received_at=0.0,
    )


async def collect(service, ctx):
    return [e async for e in service.run_turn(ctx)]


__all__ = ["LearnerProfile", "collect", "make_service", "turn_ctx"]
