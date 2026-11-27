"""Outbound ports. The teacher core never imports a database driver or web framework.

Rules for every implementation (see README):
  1. All methods are async; blocking drivers must run in a thread.
  2. Never hold a DB connection across an LLM call; open a short session per method.
  3. Writes are idempotent on their natural key (``turn_id``), so retries are safe.
"""

from __future__ import annotations

from typing import Protocol

from ea_llm.models import LearnerProfile, Persona, SessionSnapshot, StoredMessage, TurnRecord
from ea_llm.teacher.context import ScenarioRef


class HistoryStore(Protocol):
    async def save_session(self, snapshot: SessionSnapshot) -> None:
        """Insert or update the session snapshot."""

    async def load_session(self, session_id: str) -> SessionSnapshot | None: ...

    async def load_messages(self, session_id: str) -> list[StoredMessage]: ...

    async def append_turn(self, session_id: str, turn: TurnRecord) -> None:
        """Atomically store the turn's messages and usage. No-op if ``turn.turn_id`` exists."""

    async def close_session(self, session_id: str) -> bool:
        """Mark the session as ended (idempotent). False if the session does not exist."""

    async def is_closed(self, session_id: str) -> bool:
        """True once ``close_session`` ran for this session."""


class PersonaStore(Protocol):
    async def list_personas(self) -> list[Persona]:
        """Published personas only."""

    async def get_persona(self, persona_id: str) -> Persona | None: ...


class LearnerStore(Protocol):
    async def get_profile(self, user_id: str) -> LearnerProfile | None: ...


class ScenarioStore(Protocol):
    async def list_scenarios(self) -> list[ScenarioRef]:
        """Active scenarios only."""

    async def get_scenario(self, scenario_id: str) -> ScenarioRef | None:
        """An active scenario, or None."""
