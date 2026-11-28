import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, insert, select, update
from sqlalchemy.exc import DBAPIError

from english_tutor.adapters.memory import load_seed_personas, load_seed_scenarios
from english_tutor.adapters.sql import (
    METADATA,
    SqlHistoryStore,
    SqlLearnerStore,
    SqlPersonaStore,
    SqlScenarioStore,
    _retrying,
    ai_usage_logs,
    async_database_url,
    create_engine,
    messages,
    ping,
    scenarios,
    sessions,
    teacher_personas,
    users,
)
from english_tutor.models import TurnRecord
from english_tutor.teacher.context import LearnerInput, TurnContext
from english_tutor.teacher.runner import TeacherService
from english_tutor.testing import FakeProvider, reply

USER = uuid.UUID("3f2b8c1e-0000-4000-8000-000000000001")
SUSPENDED = uuid.UUID("3f2b8c1e-0000-4000-8000-000000000002")
SESSION = "6c1d3e2a-0000-4000-8000-0000000000aa"
NOW = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)


@pytest.fixture
async def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(METADATA.create_all)
        await conn.execute(
            insert(users),
            [
                {
                    "id": USER,
                    "name": " Hà ",
                    "level": "B1",
                    "age_band": "senior",
                    "disabled_at": None,
                },
                {"id": SUSPENDED, "name": "X", "level": "A2", "age_band": None, "disabled_at": NOW},
            ],
        )
        await conn.execute(
            insert(teacher_personas),
            [
                {
                    **p.model_dump(include={"id", "name", "gender", "age_band", "personality"}),
                    "switch_greeting": p.switch_greeting,
                    "address": {k: v.model_dump() for k, v in p.address.items()},
                    "fillers": p.fillers,
                    "status": "published",
                }
                for p in load_seed_personas()
            ],
        )
        await conn.execute(
            insert(scenarios),
            [{**s.model_dump(), "active": True} for s in load_seed_scenarios()],
        )
    yield engine
    await engine.dispose()


@pytest.fixture
def stores(engine):
    return (
        SqlHistoryStore(engine, clock=lambda: NOW),
        SqlPersonaStore(engine),
        SqlLearnerStore(engine),
    )


def service_for(stores, *replies):
    history, personas, learners = stores
    return TeacherService(
        primary=FakeProvider(list(replies)),
        history=history,
        personas=personas,
        learners=learners,
    )


def ctx(turn_id, text, scenario=None, mode="scenario"):
    return TurnContext(
        session_id=SESSION,
        user_id=str(USER),
        turn_id=turn_id,
        channel="text",
        mode=mode,
        persona_id="lan",
        scenario=scenario,
        learner_input=LearnerInput(text=text, source="typed") if text else None,
        received_at=0.0,
    )


async def rows(engine, table):
    async with engine.connect() as conn:
        return (await conn.execute(select(table))).all()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgres://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql://u:p@h/db?sslmode=require", "postgresql+psycopg://u:p@h/db?sslmode=require"),
        ("postgresql+psycopg2://u@h/db", "postgresql+psycopg://u@h/db"),
        ("postgresql+psycopg://u@h/db", "postgresql+psycopg://u@h/db"),
        ("sqlite:///x.db", "sqlite+aiosqlite:///x.db"),
    ],
)
def test_async_database_url(url, expected):
    assert async_database_url(url) == expected


def test_database_url_needs_a_scheme():
    with pytest.raises(ValueError):
        async_database_url("not a url")


async def test_ping(engine):
    await ping(engine)


async def test_learner_profile_comes_from_users(stores):
    learners = stores[2]
    profile = await learners.get_profile(str(USER))
    assert profile is not None
    assert (profile.display_name, profile.cefr, profile.age_band) == ("Hà", "B1", "senior")
    assert await learners.get_profile(str(SUSPENDED)) is None
    assert await learners.get_profile(str(uuid.uuid4())) is None
    assert await learners.get_profile("not-a-uuid") is None


async def test_catalogs_come_from_app_tables(engine):
    personas = SqlPersonaStore(engine)
    assert {p.id for p in await personas.list_personas()} == {p.id for p in load_seed_personas()}
    scenario_store = SqlScenarioStore(engine)
    assert (await scenario_store.get_scenario("cafe-order")).goal_vi
    assert await scenario_store.get_scenario("nope") is None


async def test_catalogs_skip_unpublished_and_invalid_rows(engine):
    async with engine.begin() as conn:
        await conn.execute(
            update(teacher_personas).where(teacher_personas.c.id == "minh").values(status="review")
        )
        await conn.execute(
            update(teacher_personas)
            .where(teacher_personas.c.id == "hung")
            .values(switch_greeting="Chào {nobody}")
        )
        await conn.execute(
            update(scenarios).where(scenarios.c.id == "job-interview").values(active=False)
        )
    personas = SqlPersonaStore(engine)
    published = {p.id for p in load_seed_personas()} - {"minh", "hung"}
    assert {p.id for p in await personas.list_personas()} == published
    assert (await personas.get_persona("minh")).status == "draft"
    assert await personas.get_persona("hung") is None
    ids = {s.id for s in await SqlScenarioStore(engine).list_scenarios()}
    assert "job-interview" not in ids and "cafe-order" in ids


async def test_catalog_is_cached_until_ttl(engine):
    personas = SqlPersonaStore(engine, ttl_s=3600)
    assert await personas.get_persona("lan")
    async with engine.begin() as conn:
        await conn.execute(teacher_personas.delete())
    assert await personas.get_persona("lan")  # still the cached copy
    assert await SqlPersonaStore(engine, ttl_s=0).get_persona("lan") is None


async def test_full_turn_is_stored_in_app_tables(engine, stores):
    scenario = await SqlScenarioStore(engine).get_scenario("cafe-order")
    service = service_for(stores, reply("<vi>Chào bác.</vi><en>What would you like?</en>"))
    snapshot = await service.open_session(
        session_id=SESSION,
        user_id=str(USER),
        channel="text",
        mode="scenario",
        persona_id="lan",
        scenario=scenario,
    )
    assert snapshot.address.learner == "bác"  # senior learner, from the users row

    opener = [e async for e in service.run_turn(ctx("t0", None, scenario))]
    turn = [e async for e in service.run_turn(ctx("t1", "A coffee please", scenario))]
    assert opener[-1].kind == turn[-1].kind == "turn_end"

    (session_row,) = await rows(engine, sessions)
    assert session_row.user_id == USER and session_row.scenario_id == "cafe-order"
    assert (session_row.status, session_row.channel, session_row.mode) == (
        "active",
        "text",
        "scenario",
    )
    assert session_row.snapshot["learner"]["age_band"] == "senior"

    stored = sorted(await rows(engine, messages), key=lambda r: r.idx)
    assert [(r.idx, r.role, r.turn_id) for r in stored] == [
        (0, "user", "t0"),
        (1, "assistant", "t0"),
        (2, "user", "t1"),
        (3, "assistant", "t1"),
    ]
    assert stored[0].text == ""  # the session-start note is not shown
    assert stored[2].text == "A coffee please"
    assert stored[3].text == "Chào bác. What would you like?"
    assert stored[3].persona_id == "lan" and stored[3].created_at is not None
    assert stored[3].content[0]["type"] == "text"

    (usage,) = await rows(engine, ai_usage_logs)
    assert (usage.user_id, usage.turn_id, usage.role) == (USER, "t1", "teacher")
    assert usage.prompt_version == snapshot.prompt_version
    assert (usage.channel, usage.mode) == ("text", "scenario")
    assert usage.cost_usd == 0.0001 and not usage.cancelled

    history = stores[0]
    reloaded = await history.load_messages(SESSION)
    assert [m.turn_id for m in reloaded] == ["t0", "t0", "t1", "t1"]
    assert reloaded[3].message.content[0].text.startswith("<vi>Chào bác.</vi>")


async def test_append_turn_is_idempotent(engine, stores):
    service = service_for(stores, reply("<vi>Ừ.</vi>"))
    await service.open_session(
        session_id=SESSION, user_id=str(USER), channel="text", mode="free_chat", persona_id="lan"
    )
    [e async for e in service.run_turn(ctx("t1", "hi", mode="free_chat"))]
    stored = await stores[0].load_messages(SESSION)
    await stores[0].append_turn(SESSION, TurnRecord(turn_id="t1", messages=stored[:1]))
    assert len(await rows(engine, messages)) == 2
    assert len(await rows(engine, ai_usage_logs)) == 1


async def test_persona_switch_updates_the_session_row(engine, stores):
    service = service_for(stores, reply("<vi>Ừ.</vi>"))
    await service.open_session(
        session_id=SESSION, user_id=str(USER), channel="text", mode="free_chat", persona_id="lan"
    )
    switched = ctx("t1", "hi", mode="free_chat").model_copy(update={"persona_id": "minh"})
    [e async for e in service.run_turn(switched)]
    (session_row,) = await rows(engine, sessions)
    assert session_row.persona_id == "minh"
    assert session_row.snapshot["persona_id"] == "minh"


async def test_close_session(engine, stores):
    history = stores[0]
    service = service_for(stores)
    await service.open_session(
        session_id=SESSION, user_id=str(USER), channel="text", mode="free_chat", persona_id="lan"
    )
    assert not (await history.load_session_state(SESSION)).closed
    assert await history.close_session(SESSION)
    assert await history.close_session(SESSION)  # idempotent
    assert (await history.load_session_state(SESSION)).closed
    (session_row,) = await rows(engine, sessions)
    assert session_row.status == "done" and session_row.ended_at is not None
    assert not await history.close_session(str(uuid.uuid4()))
    assert not await history.close_session("not-a-uuid")


async def test_legacy_and_unknown_sessions_are_not_found(engine, stores):
    history = stores[0]
    legacy = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            insert(sessions).values(
                id=legacy,
                user_id=USER,
                status="active",
                channel="text",
                mode="scenario",
                started_at=NOW,
                seconds=0,
            )
        )
    assert await history.load_session(str(legacy)) is None
    assert not await history.close_session(str(legacy))
    assert await history.load_session("not-a-uuid") is None
    assert await history.load_messages("not-a-uuid") == []


async def test_session_ids_must_be_uuids(stores):
    service = service_for(stores)
    with pytest.raises(ValueError, match="session_id must be a UUID"):
        await service.open_session(
            session_id="abc", user_id=str(USER), channel="text", mode="free_chat", persona_id="lan"
        )


async def test_append_turn_for_unknown_session_fails(stores):
    with pytest.raises(LookupError):
        await stores[0].append_turn(SESSION, TurnRecord(turn_id="t1", messages=[]))


class StatementCounter:
    """Counts the SQL statements an engine sends (each one is a round trip to the database)."""

    def __init__(self, engine) -> None:
        self.statements: list[str] = []
        event.listen(engine.sync_engine, "before_cursor_execute", self._seen)

    def _seen(self, conn, cursor, statement, *args) -> None:
        self.statements.append(statement.split()[0].upper())

    def take(self) -> list[str]:
        taken, self.statements = self.statements, []
        return taken


async def test_a_turn_reads_with_few_statements(engine, stores):
    history = stores[0]
    service = service_for(stores, reply("<vi>Ừ.</vi>"))
    await service.open_session(
        session_id=SESSION, user_id=str(USER), channel="text", mode="free_chat", persona_id="lan"
    )
    [e async for e in service.run_turn(ctx("t1", "hi", mode="free_chat"))]
    counter = StatementCounter(engine)

    state = await history.load_session_state(SESSION)
    assert counter.take() == ["SELECT"]  # auth and "ended?" in one query
    assert state is not None and not state.closed and state.snapshot.persona_id == "lan"

    snapshot, stored = await history.load_conversation(SESSION)
    assert counter.take() == ["SELECT", "SELECT"]  # everything a turn needs before the LLM
    assert snapshot == state.snapshot and [m.turn_id for m in stored] == ["t1", "t1"]

    await history.save_session(snapshot)
    assert counter.take() == ["INSERT"]  # one upsert

    again = stored[0].model_copy(update={"turn_id": "t2"})
    record = TurnRecord(turn_id="t2", messages=[again], usage=[])
    await history.append_turn(SESSION, record)
    assert counter.take() == ["SELECT", "SELECT", "INSERT"]  # lock, duplicate+index, insert
    await history.append_turn(SESSION, record)
    assert counter.take() == ["SELECT", "SELECT"]  # already stored: nothing written


async def test_conversation_of_unknown_or_legacy_sessions_is_empty(engine, stores):
    history = stores[0]
    assert await history.load_conversation("not-a-uuid") == (None, [])
    assert await history.load_conversation(str(uuid.uuid4())) == (None, [])
    assert await history.load_session_state(str(uuid.uuid4())) is None


async def test_upsert_keeps_the_row_started_and_ended_state(engine, stores):
    history = stores[0]
    service = service_for(stores)
    snapshot = await service.open_session(
        session_id=SESSION, user_id=str(USER), channel="text", mode="free_chat", persona_id="lan"
    )
    await history.close_session(SESSION)
    later = SqlHistoryStore(engine, clock=lambda: datetime(2030, 1, 1, tzinfo=UTC))
    await later.save_session(snapshot.model_copy(update={"persona_id": "minh"}))
    (row,) = await rows(engine, sessions)
    assert row.persona_id == "minh" and row.status == "done"
    assert row.started_at.year == 2026  # the first insert's time stays


class _Dropped(Exception):
    pass


def _dropped(invalidated: bool) -> DBAPIError:
    return DBAPIError("SELECT 1", {}, _Dropped(), connection_invalidated=invalidated)


async def test_a_step_is_retried_once_when_the_server_closed_the_connection():
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise _dropped(invalidated=True)
        return "ok"

    assert await _retrying(flaky) == "ok"
    assert len(calls) == 2


async def test_other_database_errors_are_not_retried():
    calls = []

    async def broken():
        calls.append(1)
        raise _dropped(invalidated=False)

    with pytest.raises(DBAPIError):
        await _retrying(broken)
    assert len(calls) == 1


async def test_a_second_drop_is_raised():
    async def always_dropped():
        raise _dropped(invalidated=True)

    with pytest.raises(DBAPIError):
        await _retrying(always_dropped)
