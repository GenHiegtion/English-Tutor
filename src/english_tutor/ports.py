"""Outbound ports. The teacher core never imports a database driver or web framework.

Rules for every implementation (see README):
  1. All methods are async; blocking drivers must run in a thread.
  2. Never hold a DB connection across an LLM call; open a short session per method.
  3. Writes are idempotent on their natural key (``turn_id``), so retries are safe.
"""

from __future__ import annotations

from typing import Protocol

from english_tutor.models import (
    LearnerProfile,
    Persona,
    SessionSnapshot,
    SessionState,
    StoredMessage,
    TurnRecord,
)
from english_tutor.teacher.context import ScenarioRef


class HistoryStore(Protocol):
    async def save_session(self, snapshot: SessionSnapshot) -> None:
        """Insert or update the session snapshot."""

    async def load_session(self, session_id: str) -> SessionSnapshot | None: ...

    async def load_session_state(self, session_id: str) -> SessionState | None:
        """The snapshot and whether ``close_session`` ran, in one read; None if unknown."""

    async def load_messages(self, session_id: str) -> list[StoredMessage]: ...

    async def load_conversation(
        self, session_id: str
    ) -> tuple[SessionSnapshot | None, list[StoredMessage]]:
        """The snapshot and its messages together: what a turn reads before calling the LLM."""

    async def append_turn(self, session_id: str, turn: TurnRecord) -> None:
        """Atomically store the turn's messages and usage. No-op if ``turn.turn_id`` exists."""

    async def close_session(self, session_id: str) -> bool:
        """Mark the session as ended (idempotent). False if the session does not exist."""


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
