"""Postgres (Neon) implementations of the ports, on the Talkwise database schema.

Talkwise's Alembic migrations own every table here (migration ``0030`` added the EA-LLM
columns); this module never creates or migrates them. ``METADATA`` mirrors only the columns
read or written below, so tests can create the tables on SQLite. Keep it in sync with
``api/app/models.py`` of English-Teacher.

Every method opens its own short transaction, so no connection is held across an LLM call.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import get_args

from pydantic import ValidationError
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    Uuid,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ea_llm.models import (
    AgeBand,
    CefrLevel,
    LearnerProfile,
    Persona,
    SessionSnapshot,
    StoredMessage,
    TurnRecord,
    UsageRecord,
)
from ea_llm.providers.base import Message
from ea_llm.teacher.context import ScenarioRef
from ea_llm.teacher.display import display_text

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
    """Async engine for the Talkwise database (works with Neon's pooled endpoint)."""
    url = async_database_url(url)
    if not url.startswith("postgresql+psycopg://"):
        return create_async_engine(url)
    return create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=POOL_SIZE,
        max_overflow=POOL_MAX_OVERFLOW,
        pool_recycle=POOL_RECYCLE_S,
        # Neon's pooler runs PgBouncer in transaction mode: server-side prepared statements
        # would land on another backend, so psycopg must never prepare.
        connect_args={"prepare_threshold": None, "connect_timeout": CONNECT_TIMEOUT_S},
    )


async def ping(engine: AsyncEngine) -> None:
    """Raise if the database cannot answer a trivial query."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


def parse_uuid(value: str) -> uuid.UUID | None:
    """UUID of a Talkwise id, or None when ``value`` is not one (an unknown id)."""
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


def _now() -> datetime:
    return datetime.now(UTC)


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]


class SqlHistoryStore:
    """Sessions, messages and usage rows in the Talkwise ``sessions``/``messages`` tables."""

    def __init__(self, engine: AsyncEngine, clock: Callable[[], datetime] = _now) -> None:
        self._engine = engine
        self._clock = clock

    async def save_session(self, snapshot: SessionSnapshot) -> None:
        sid = _require_uuid(snapshot.session_id, "session_id")
        payload = snapshot.model_dump(mode="json")
        async with self._engine.begin() as conn:
            exists = await conn.scalar(select(sessions.c.id).where(sessions.c.id == sid))
            if exists is not None:
                await conn.execute(
                    update(sessions)
                    .where(sessions.c.id == sid)
                    .values(
                        persona_id=snapshot.persona_id,
                        prompt_version=snapshot.prompt_version,
                        snapshot=payload,
                    )
                )
                return
            await conn.execute(
                insert(sessions).values(
                    id=sid,
                    user_id=_require_uuid(snapshot.user_id, "user_id"),
                    scenario_id=snapshot.scenario.id if snapshot.scenario else None,
                    status=SESSION_ACTIVE,
                    channel=snapshot.channel,
                    mode=snapshot.mode,
                    persona_id=snapshot.persona_id,
                    prompt_version=snapshot.prompt_version,
                    snapshot=payload,
                    started_at=self._clock(),
                    seconds=0,
                )
            )

    async def load_session(self, session_id: str) -> SessionSnapshot | None:
        """The snapshot of an EA-LLM session; None for unknown ids and legacy sessions."""
        sid = parse_uuid(session_id)
        if sid is None:
            return None
        async with self._engine.connect() as conn:
            raw = await conn.scalar(select(sessions.c.snapshot).where(sessions.c.id == sid))
        return SessionSnapshot.model_validate(raw) if raw else None

    async def load_messages(self, session_id: str) -> list[StoredMessage]:
        sid = parse_uuid(session_id)
        if sid is None:
            return []
        query = (
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
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [_stored_message(row) for row in rows]

    async def append_turn(self, session_id: str, turn: TurnRecord) -> None:
        sid = _require_uuid(session_id, "session_id")
        async with self._engine.begin() as conn:
            # Row lock: turns of one session are written one after the other.
            session = (
                await conn.execute(
                    select(sessions.c.prompt_version, sessions.c.channel, sessions.c.mode)
                    .where(sessions.c.id == sid)
                    .with_for_update()
                )
            ).first()
            if session is None:
                raise LookupError(f"cannot store turn {turn.turn_id}: unknown session {sid}")
            duplicate = await conn.scalar(
                select(messages.c.id)
                .where(messages.c.session_id == sid, messages.c.turn_id == turn.turn_id)
                .limit(1)
            )
            if duplicate is not None:
                return
            last_idx = await conn.scalar(
                select(func.max(messages.c.idx)).where(messages.c.session_id == sid)
            )
            first_idx = 0 if last_idx is None else last_idx + 1
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

    async def close_session(self, session_id: str) -> bool:
        sid = parse_uuid(session_id)
        if sid is None:
            return False
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(sessions)
                .where(sessions.c.id == sid, sessions.c.snapshot.is_not(None))
                .values(
                    status=SESSION_DONE,
                    ended_at=func.coalesce(sessions.c.ended_at, self._clock()),
                )
            )
        return result.rowcount > 0

    async def is_closed(self, session_id: str) -> bool:
        sid = parse_uuid(session_id)
        if sid is None:
            return False
        async with self._engine.connect() as conn:
            status = await conn.scalar(select(sessions.c.status).where(sessions.c.id == sid))
        return status == SESSION_DONE


class SqlLearnerStore:
    """Learner profiles from the Talkwise ``users`` table."""

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
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).first()
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
    """Teachers from the Talkwise ``teacher_personas`` table, cached for ``ttl_s``."""

    def __init__(self, engine: AsyncEngine, ttl_s: float = CATALOG_TTL_S) -> None:
        self._engine = engine
        self._catalog: _Catalog[Persona] = _Catalog(self._load, ttl_s)

    async def list_personas(self) -> list[Persona]:
        return [p for p in (await self._catalog.items()).values() if p.status == "published"]

    async def get_persona(self, persona_id: str) -> Persona | None:
        return (await self._catalog.items()).get(persona_id)

    async def _load(self) -> dict[str, Persona]:
        async with self._engine.connect() as conn:
            rows = (await conn.execute(select(teacher_personas).order_by("id"))).all()
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
                    # Talkwise also has "review": anything not published is offered to no one.
                    status="published" if row.status == "published" else "draft",
                )
            except ValidationError:
                logger.warning("skipping invalid teacher persona %s", row.id, exc_info=True)
        return loaded


class SqlScenarioStore:
    """Active role-play scenarios from the Talkwise ``scenarios`` table, cached for ``ttl_s``."""

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
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
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
        raise ValueError(f"{field} must be a UUID for the Talkwise database")
    return parsed


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
