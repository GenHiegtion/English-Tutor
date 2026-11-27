"""Postgres (Neon) implementations of the ports, on the app database schema.

The app's Alembic migrations own every table here (migration ``0030`` added the English Tutor
columns); this module never creates or migrates them. ``METADATA`` mirrors only the columns
read or written below, so tests can create the tables on SQLite. Keep it in sync with
``api/app/models.py`` of English-Teacher.

Every method takes a pooled connection for one short step, so no connection is held across an
LLM call. The database may be far away (one round trip can take 0.2 s), so each method keeps
its round trips low: reads run in autocommit (no BEGIN/ROLLBACK), single statements commit on
their own, and the pool does not ping connections before use; a connection the server closed
in the meantime is replaced and the step retried once, which is safe because every write is
idempotent.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, get_args

from pydantic import ValidationError
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Executable,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    Uuid,
    exists,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Row
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from english_tutor.models import (
    AgeBand,
    CefrLevel,
    LearnerProfile,
    Persona,
    SessionSnapshot,
    SessionState,
    StoredMessage,
    TurnRecord,
    UsageRecord,
)
from english_tutor.providers.base import Message
from english_tutor.teacher.context import ScenarioRef
from english_tutor.teacher.display import display_text

logger = logging.getLogger(__name__)

CATALOG_TTL_S = 60.0  # personas and scenarios change rarely; reload them at most once a minute
POOL_SIZE = 5
POOL_MAX_OVERFLOW = 5
POOL_RECYCLE_S = 300
CONNECT_TIMEOUT_S = 10
SESSION_ACTIVE = "active"
SESSION_DONE = "done"
CEFR_LEVELS: frozenset[str] = frozenset(get_args(CefrLevel))
AGE_BANDS: frozenset[str] = frozenset(get_args(AgeBand))

TS = DateTime(timezone=True)
METADATA = MetaData()

users = Table(
    "users",
    METADATA,
    Column("id", Uuid, primary_key=True),
    Column("name", String(80), nullable=False, default=""),
    Column("level", String(2), nullable=False, default="B1"),
    Column("age_band", String(10)),
    Column("disabled_at", TS),
)

scenarios = Table(
    "scenarios",
    METADATA,
    Column("id", String(60), primary_key=True),
    Column("title", String(120), nullable=False),
    Column("level", String(2), nullable=False),
    Column("goal", Text, nullable=False),
    Column("goal_vi", Text),
    Column("opener", Text, nullable=False),
    Column("targets", JSON, nullable=False),
    Column("active", Boolean, nullable=False, default=True),
)

teacher_personas = Table(
    "teacher_personas",
    METADATA,
    Column("id", String(40), primary_key=True),
    Column("name", String(80), nullable=False),
    Column("gender", String(10), nullable=False),
    Column("age_band", String(10), nullable=False),
    Column("personality", Text, nullable=False),
    Column("switch_greeting", Text, nullable=False),
    Column("address", JSON, nullable=False),
    Column("fillers", JSON, nullable=False),
    Column("status", String(12), nullable=False),
)

sessions = Table(
    "sessions",
    METADATA,
    Column("id", Uuid, primary_key=True),
    Column("user_id", Uuid, nullable=False),
    Column("scenario_id", String(60)),
    Column("status", String(10), nullable=False),
    Column("channel", String(10), nullable=False),
    Column("mode", String(12), nullable=False),
    Column("persona_id", String(40)),
    Column("prompt_version", String(160)),
    Column("snapshot", JSON),
    Column("started_at", TS, nullable=False),
    Column("ended_at", TS),
    Column("seconds", Integer, nullable=False),
)

messages = Table(
    "messages",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("session_id", Uuid, nullable=False, index=True),
    Column("idx", Integer, nullable=False),
    Column("role", String(10), nullable=False),
    Column("text", Text, nullable=False),
    Column("content", JSON),
    Column("turn_id", String(40), index=True),
    Column("guard_flags", JSON),
    Column("tool_statuses", JSON),
    Column("created_at", TS),
    Column("persona_id", String(40)),
)

ai_usage_logs = Table(
    "ai_usage_logs",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Uuid),
    Column("session_id", Uuid),
    Column("turn_id", String(40)),
    Column("role", String(12), nullable=False),
    Column("provider", String(40), nullable=False),
    Column("model", String(80), nullable=False),
    Column("prompt_version", String(160)),
    Column("channel", String(10)),
    Column("mode", String(12)),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cache_read_tokens", Integer, nullable=False),
    Column("cache_write_tokens", Integer, nullable=False),
    Column("cost_usd", Float),
    Column("ttft_ms", Integer),
    Column("fallback_used", Boolean, nullable=False),
    Column("cancelled", Boolean, nullable=False),
    Column("created_at", TS, nullable=False),
)


def async_database_url(url: str) -> str:
    """Point a plain Postgres/SQLite URL at its async driver (psycopg 3, aiosqlite)."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        raise ValueError("database URL must look like 'postgresql://...'")
    drivers = {
        "postgres": "postgresql+psycopg",
        "postgresql": "postgresql+psycopg",
        "postgresql+psycopg2": "postgresql+psycopg",
        "sqlite": "sqlite+aiosqlite",
    }
    return f"{drivers.get(scheme, scheme)}://{rest}"


def create_engine(url: str) -> AsyncEngine:
    """Async engine for the app database (works with Neon's pooled endpoint)."""
    url = async_database_url(url)
    if not url.startswith("postgresql+psycopg://"):
        return create_async_engine(url)
    return create_async_engine(
        url,
        # No pool_pre_ping: it costs a round trip per query. Dropped connections are retried
        # instead (_retrying), and pool_recycle replaces old ones before the server drops them.
        pool_size=POOL_SIZE,
        max_overflow=POOL_MAX_OVERFLOW,
        pool_recycle=POOL_RECYCLE_S,
        # Neon's pooler runs PgBouncer in transaction mode: server-side prepared statements
        # would land on another backend, so psycopg must never prepare.
        connect_args={"prepare_threshold": None, "connect_timeout": CONNECT_TIMEOUT_S},
    )


async def ping(engine: AsyncEngine) -> None:
    """Raise if the database cannot answer a trivial query."""
    await _read(engine, text("SELECT 1"))


@asynccontextmanager
async def _autocommit(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A pooled connection in autocommit: each read is one round trip, no BEGIN/ROLLBACK."""
    async with engine.connect() as conn:
        yield await conn.execution_options(isolation_level="AUTOCOMMIT")


async def _retrying[T](step: Callable[[], Awaitable[T]]) -> T:
    """Run ``step`` once more when the server had closed the pooled connection it got."""
    try:
        return await step()
    except DBAPIError as exc:
        if not exc.connection_invalidated:
            raise
        logger.info("database connection was closed by the server; retrying on a new one")
        return await step()


async def _read(engine: AsyncEngine, *queries: Executable) -> list[Sequence[Row[Any]]]:
    """The rows of each query, all on one connection (one round trip per query)."""

    async def step() -> list[Sequence[Row[Any]]]:
        async with _autocommit(engine) as conn:
            return [(await conn.execute(query)).all() for query in queries]

    return await _retrying(step)


async def _write_one(engine: AsyncEngine, statement: Executable) -> int:
    """Run one statement that commits on its own (atomic as a single statement); rows changed."""

    async def step() -> int:
        async with _autocommit(engine) as conn:
            return (await conn.execute(statement)).rowcount

    return await _retrying(step)


async def _transaction[T](
    engine: AsyncEngine, work: Callable[[AsyncConnection], Awaitable[T]]
) -> T:
    """Run ``work`` in one transaction (BEGIN ... COMMIT)."""

    async def step() -> T:
        async with engine.begin() as conn:
            return await work(conn)

    return await _retrying(step)


def parse_uuid(value: str) -> uuid.UUID | None:
    """UUID of an app id, or None when ``value`` is not one (an unknown id)."""
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


def _now() -> datetime:
    return datetime.now(UTC)


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]


class SqlHistoryStore:
    """Sessions, messages and usage rows in the app's ``sessions``/``messages`` tables."""

    def __init__(self, engine: AsyncEngine, clock: Callable[[], datetime] = _now) -> None:
        self._engine = engine
        self._clock = clock

    async def save_session(self, snapshot: SessionSnapshot) -> None:
        """Insert the session row, or update its teacher and snapshot: one upsert statement."""
        sid = _require_uuid(snapshot.session_id, "session_id")
        row = {
            "id": sid,
            "user_id": _require_uuid(snapshot.user_id, "user_id"),
            "scenario_id": snapshot.scenario.id if snapshot.scenario else None,
            "status": SESSION_ACTIVE,
            "channel": snapshot.channel,
            "mode": snapshot.mode,
            "persona_id": snapshot.persona_id,
            "prompt_version": snapshot.prompt_version,
            "snapshot": snapshot.model_dump(mode="json"),
            "started_at": self._clock(),
            "seconds": 0,
        }
        await _write_one(self._engine, _upsert_session(self._engine.dialect.name, row))

    async def load_session(self, session_id: str) -> SessionSnapshot | None:
        """The snapshot of an English Tutor session; None for unknown ids and legacy sessions."""
        state = await self.load_session_state(session_id)
        return state.snapshot if state else None

    async def load_session_state(self, session_id: str) -> SessionState | None:
        """Snapshot and ended flag in one query; None for unknown ids and legacy sessions."""
        sid = parse_uuid(session_id)
        if sid is None:
            return None
        (rows,) = await _read(self._engine, _session_query(sid))
        return _session_state(rows)

    async def load_messages(self, session_id: str) -> list[StoredMessage]:
        sid = parse_uuid(session_id)
        if sid is None:
            return []
        (rows,) = await _read(self._engine, _messages_query(sid))
        return [_stored_message(row) for row in rows]

    async def load_conversation(
        self, session_id: str
    ) -> tuple[SessionSnapshot | None, list[StoredMessage]]:
        """Snapshot and messages on one connection: two round trips before the LLM call."""
        sid = parse_uuid(session_id)
        if sid is None:
            return None, []
        session_rows, message_rows = await _read(
            self._engine, _session_query(sid), _messages_query(sid)
        )
        state = _session_state(session_rows)
        if state is None:
            return None, []
        return state.snapshot, [_stored_message(row) for row in message_rows]

    async def append_turn(self, session_id: str, turn: TurnRecord) -> None:
        sid = _require_uuid(session_id, "session_id")

        async def work(conn: AsyncConnection) -> None:
            # Row lock: turns of one session are written one after the other. The next query
            # runs after the lock is held, so it sees what an earlier writer committed.
            session = (
                await conn.execute(
                    select(sessions.c.prompt_version, sessions.c.channel, sessions.c.mode)
                    .where(sessions.c.id == sid)
                    .with_for_update()
                )
            ).first()
            if session is None:
                raise LookupError(f"cannot store turn {turn.turn_id}: unknown session {sid}")
            seen = (
                await conn.execute(
                    select(
                        exists()
                        .where(messages.c.session_id == sid, messages.c.turn_id == turn.turn_id)
                        .label("duplicate"),
                        select(func.max(messages.c.idx))
                        .where(messages.c.session_id == sid)
                        .scalar_subquery()
                        .label("last_idx"),
                    )
                )
            ).one()
            if seen.duplicate:
                return
            first_idx = 0 if seen.last_idx is None else seen.last_idx + 1
            created_at = self._clock()
            if turn.messages:
                await conn.execute(
                    insert(messages),
                    [
                        _message_row(sid, first_idx + i, item, created_at)
                        for i, item in enumerate(turn.messages)
                    ],
                )
            if turn.usage:
                await conn.execute(
                    insert(ai_usage_logs),
                    [_usage_row(sid, record, session, created_at) for record in turn.usage],
                )

        await _transaction(self._engine, work)

    async def close_session(self, session_id: str) -> bool:
        sid = parse_uuid(session_id)
        if sid is None:
            return False
        changed = await _write_one(
            self._engine,
            update(sessions)
            .where(sessions.c.id == sid, sessions.c.snapshot.is_not(None))
            .values(
                status=SESSION_DONE,
                ended_at=func.coalesce(sessions.c.ended_at, self._clock()),
            ),
        )
        return changed > 0


class SqlLearnerStore:
    """Learner profiles from the app's ``users`` table."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def get_profile(self, user_id: str) -> LearnerProfile | None:
        """The profile of an active account; None for unknown or suspended accounts."""
        uid = parse_uuid(user_id)
        if uid is None:
            return None
        query = select(users.c.name, users.c.level, users.c.age_band, users.c.disabled_at).where(
            users.c.id == uid
        )
        (rows,) = await _read(self._engine, query)
        row = rows[0] if rows else None
        if row is None or row.disabled_at is not None:
            return None
        return LearnerProfile(
            user_id=user_id,
            display_name=(row.name or "").strip() or None,
            cefr=row.level if row.level in CEFR_LEVELS else "A1",
            age_band=row.age_band if row.age_band in AGE_BANDS else "unknown",
        )


class _Catalog[T]:
    """Small read-through cache of a whole table, refreshed after ``ttl_s``."""

    def __init__(self, load: Callable[[], Awaitable[dict[str, T]]], ttl_s: float) -> None:
        self._load = load
        self._ttl_s = ttl_s
        self._items: dict[str, T] = {}
        self._loaded_at: float | None = None
        self._lock = asyncio.Lock()

    async def items(self) -> dict[str, T]:
        if self._fresh():
            return self._items
        async with self._lock:
            if not self._fresh():
                self._items = await self._load()
                self._loaded_at = time.monotonic()
        return self._items

    def _fresh(self) -> bool:
        return self._loaded_at is not None and time.monotonic() - self._loaded_at < self._ttl_s


class SqlPersonaStore:
    """Teachers from the app's ``teacher_personas`` table, cached for ``ttl_s``."""

    def __init__(self, engine: AsyncEngine, ttl_s: float = CATALOG_TTL_S) -> None:
        self._engine = engine
        self._catalog: _Catalog[Persona] = _Catalog(self._load, ttl_s)

    async def list_personas(self) -> list[Persona]:
        return [p for p in (await self._catalog.items()).values() if p.status == "published"]

    async def get_persona(self, persona_id: str) -> Persona | None:
        return (await self._catalog.items()).get(persona_id)

    async def _load(self) -> dict[str, Persona]:
        (rows,) = await _read(self._engine, select(teacher_personas).order_by("id"))
        loaded: dict[str, Persona] = {}
        for row in rows:
            try:
                loaded[row.id] = Persona(
                    id=row.id,
                    name=row.name,
                    gender=row.gender,
                    age_band=row.age_band,
                    personality=row.personality,
                    address=row.address,
                    switch_greeting=row.switch_greeting,
                    fillers=row.fillers or {},
                    # The app also has "review": anything not published is offered to no one.
                    status="published" if row.status == "published" else "draft",
                )
            except ValidationError:
                logger.warning("skipping invalid teacher persona %s", row.id, exc_info=True)
        return loaded


class SqlScenarioStore:
    """Active role-play scenarios from the app's ``scenarios`` table, cached for ``ttl_s``."""

    def __init__(self, engine: AsyncEngine, ttl_s: float = CATALOG_TTL_S) -> None:
        self._engine = engine
        self._catalog: _Catalog[ScenarioRef] = _Catalog(self._load, ttl_s)

    async def list_scenarios(self) -> list[ScenarioRef]:
        return list((await self._catalog.items()).values())

    async def get_scenario(self, scenario_id: str) -> ScenarioRef | None:
        return (await self._catalog.items()).get(scenario_id)

    async def _load(self) -> dict[str, ScenarioRef]:
        query = (
            select(
                scenarios.c.id,
                scenarios.c.title,
                scenarios.c.level,
                scenarios.c.goal,
                scenarios.c.goal_vi,
                scenarios.c.opener,
                scenarios.c.targets,
            )
            .where(scenarios.c.active.is_(True))
            .order_by(scenarios.c.level, scenarios.c.id)
        )
        (rows,) = await _read(self._engine, query)
        loaded: dict[str, ScenarioRef] = {}
        for row in rows:
            try:
                loaded[row.id] = ScenarioRef.model_validate(row._asdict())
            except ValidationError:
                logger.warning("skipping invalid scenario %s", row.id, exc_info=True)
        return loaded


def _require_uuid(value: str, field: str) -> uuid.UUID:
    parsed = parse_uuid(value)
    if parsed is None:
        raise ValueError(f"{field} must be a UUID for the app database")
    return parsed


def _session_query(sid: uuid.UUID) -> Executable:
    return select(sessions.c.snapshot, sessions.c.status).where(sessions.c.id == sid)


def _session_state(rows: Sequence[Row[Any]]) -> SessionState | None:
    """The session of a ``_session_query`` result; None when missing or a legacy session."""
    if not rows or not rows[0].snapshot:
        return None
    return SessionState(
        snapshot=SessionSnapshot.model_validate(rows[0].snapshot),
        closed=rows[0].status == SESSION_DONE,
    )


def _messages_query(sid: uuid.UUID) -> Executable:
    return (
        select(
            messages.c.turn_id,
            messages.c.role,
            messages.c.content,
            messages.c.persona_id,
            messages.c.guard_flags,
            messages.c.tool_statuses,
        )
        .where(
            messages.c.session_id == sid,
            messages.c.turn_id.is_not(None),
            messages.c.content.is_not(None),
        )
        .order_by(messages.c.idx)
    )


def _upsert_session(dialect: str, row: dict[str, Any]) -> Executable:
    """INSERT the session row; on a known id only its teacher and snapshot change."""
    dialects = {"postgresql": postgresql.insert, "sqlite": sqlite.insert}
    if dialect not in dialects:
        raise ValueError(f"unsupported database dialect: {dialect}")
    statement = dialects[dialect](sessions).values(**row)
    return statement.on_conflict_do_update(
        index_elements=[sessions.c.id],
        set_={
            name: statement.excluded[name] for name in ("persona_id", "prompt_version", "snapshot")
        },
    )


def _stored_message(row: Row) -> StoredMessage:
    return StoredMessage(
        turn_id=row.turn_id,
        message=Message(role=row.role, content=row.content),
        persona_id=row.persona_id,
        guard_flags=row.guard_flags or [],
        tool_statuses=row.tool_statuses or {},
    )


def _message_row(
    session_id: uuid.UUID, idx: int, item: StoredMessage, created_at: datetime
) -> dict[str, object]:
    return {
        "session_id": session_id,
        "idx": idx,
        "role": item.message.role,
        "text": display_text(item),
        "content": [part.model_dump(mode="json") for part in item.message.content],
        "turn_id": item.turn_id,
        "guard_flags": item.guard_flags,
        "tool_statuses": item.tool_statuses,
        "created_at": created_at,
        "persona_id": item.persona_id,
    }


def _usage_row(
    session_id: uuid.UUID, record: UsageRecord, session: Row, created_at: datetime
) -> dict[str, object]:
    return {
        "user_id": parse_uuid(record.user_id),
        "session_id": session_id,
        "turn_id": record.turn_id,
        "role": record.role,
        "provider": _clip(record.provider, 40),
        "model": _clip(record.model, 80),
        "prompt_version": session.prompt_version,
        "channel": session.channel,
        "mode": session.mode,
        "input_tokens": record.input_tokens,
        "output_tokens": record.output_tokens,
        "cache_read_tokens": record.cache_read_tokens,
        "cache_write_tokens": record.cache_write_tokens,
        "cost_usd": record.cost_usd,
        "ttft_ms": record.ttft_ms,
        "fallback_used": record.fallback_used,
        "cancelled": record.cancelled,
        "created_at": created_at,
    }
