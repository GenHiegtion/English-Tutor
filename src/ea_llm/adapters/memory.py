"""In-memory port implementations for development, tests, the CLI and evals."""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from ea_llm.models import LearnerProfile, Persona, SessionSnapshot, StoredMessage, TurnRecord
from ea_llm.teacher.context import ScenarioRef

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class MemoryHistoryStore:
    def __init__(self) -> None:
        self.sessions: dict[str, SessionSnapshot] = {}
        self.messages: dict[str, list[StoredMessage]] = {}
        self.turns: dict[str, TurnRecord] = {}  # turn_id -> record (idempotency + usage log)

    async def save_session(self, snapshot: SessionSnapshot) -> None:
        self.sessions[snapshot.session_id] = snapshot.model_copy(deep=True)
        self.messages.setdefault(snapshot.session_id, [])

    async def load_session(self, session_id: str) -> SessionSnapshot | None:
        snap = self.sessions.get(session_id)
        return snap.model_copy(deep=True) if snap else None

    async def load_messages(self, session_id: str) -> list[StoredMessage]:
        return copy.deepcopy(self.messages.get(session_id, []))

    async def append_turn(self, session_id: str, turn: TurnRecord) -> None:
        if turn.turn_id in self.turns:
            return
        self.turns[turn.turn_id] = turn.model_copy(deep=True)
        self.messages.setdefault(session_id, []).extend(copy.deepcopy(turn.messages))


class MemoryPersonaStore:
    def __init__(self, personas: list[Persona]) -> None:
        self._by_id = {p.id: p for p in personas}

    @classmethod
    def from_seed(cls) -> MemoryPersonaStore:
        raw = yaml.safe_load((DATA_DIR / "personas.yaml").read_text(encoding="utf-8"))
        return cls([Persona.model_validate(item) for item in raw])

    async def list_personas(self) -> list[Persona]:
        return [p for p in self._by_id.values() if p.status == "published"]

    async def get_persona(self, persona_id: str) -> Persona | None:
        return self._by_id.get(persona_id)


class MemoryLearnerStore:
    def __init__(self, profiles: list[LearnerProfile] | None = None) -> None:
        self._by_id = {p.user_id: p for p in profiles or []}

    async def get_profile(self, user_id: str) -> LearnerProfile | None:
        return self._by_id.get(user_id)

    def put(self, profile: LearnerProfile) -> None:
        self._by_id[profile.user_id] = profile


def load_seed_scenarios() -> list[ScenarioRef]:
    raw = yaml.safe_load((DATA_DIR / "scenarios.yaml").read_text(encoding="utf-8"))
    return [ScenarioRef.model_validate(item) for item in raw]
