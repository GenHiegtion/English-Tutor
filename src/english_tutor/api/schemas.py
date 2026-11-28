"""Request and response bodies of the HTTP API (the OpenAPI contract for both callers)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from english_tutor.models import (
    Address,
    AgeBand,
    CefrLevel,
    Gender,
    LearnerProfile,
    SessionSnapshot,
)
from english_tutor.teacher.context import ScenarioRef
from english_tutor.teacher.display import DisplayMessage

MAX_TEXT_CHARS = 4000  # the runner also trims long messages before they reach the model
ID_PATTERN = r"^[A-Za-z0-9_-]{1,40}$"  # fits the 40-char turn_id/persona_id columns


class LearnerOverride(BaseModel):
    """Profile fields a backend service may set for this session (browsers may not)."""

    display_name: str | None = Field(default=None, max_length=80)
    cefr: CefrLevel | None = None
    age_band: AgeBand | None = None
    interests: list[str] | None = Field(default=None, max_length=20)

    def apply(self, profile: LearnerProfile) -> LearnerProfile:
        """``profile`` with the fields set here replaced."""
        return profile.model_copy(update=self.model_dump(exclude_none=True))


class CreateSession(BaseModel):
    user_id: str | None = Field(
        default=None,
        max_length=64,
        description="User id in the app. Required with an API key; taken from the token otherwise.",
    )
    persona_id: str = Field(pattern=ID_PATTERN, examples=["lan"])
    mode: Literal["scenario", "free_chat"] = "scenario"
    scenario_id: str | None = Field(
        default=None, max_length=60, description="Required in 'scenario' mode, only there."
    )
    learner: LearnerOverride | None = Field(
        default=None, description="API-key callers only: overrides the stored profile."
    )

    @model_validator(mode="after")
    def _scenario_matches_mode(self) -> CreateSession:
        if (self.mode == "scenario") != (self.scenario_id is not None):
            raise ValueError("scenario_id is required for, and only for, mode 'scenario'")
        return self


class LearnerOut(BaseModel):
    cefr: CefrLevel
    age_band: AgeBand
    display_name: str | None = None


class SessionOut(BaseModel):
    session_id: str
    user_id: str
    channel: Literal["text", "voice"]
    mode: Literal["scenario", "free_chat", "lesson"]
    persona_id: str
    address: Address = Field(description="How the teacher and the learner are called.")
    prompt_version: str
    learner: LearnerOut
    scenario: ScenarioRef | None = None

    @classmethod
    def of(cls, snapshot: SessionSnapshot) -> SessionOut:
        """Public view of a session snapshot."""
        return cls(
            session_id=snapshot.session_id,
            user_id=snapshot.user_id,
            channel=snapshot.channel,
            mode=snapshot.mode,
            persona_id=snapshot.persona_id,
            address=snapshot.address,
            prompt_version=snapshot.prompt_version,
            learner=LearnerOut(
                cefr=snapshot.learner.cefr,
                age_band=snapshot.learner.age_band,
                display_name=snapshot.learner.display_name,
            ),
            scenario=snapshot.scenario,
        )


class CreateTurn(BaseModel):
    text: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_TEXT_CHARS,
        description=(
            "Learner message. Omit it only for the first turn of a session: the teacher opens "
            "the 'scenario' or 'free_chat' session with fixed lines (no LLM call)."
        ),
    )
    turn_id: str | None = Field(
        default=None,
        pattern=ID_PATTERN,
        description="Idempotency key chosen by the client; generated if omitted.",
    )
    persona_id: str | None = Field(
        default=None, pattern=ID_PATTERN, description="Set to switch teacher from this turn on."
    )


class Delivered(BaseModel):
    seq: int = Field(ge=0, description="Highest segment seq the learner actually received.")


class PersonaOut(BaseModel):
    id: str
    name: str
    gender: Gender
    age_band: AgeBand


class PersonaList(BaseModel):
    items: list[PersonaOut]


class ScenarioList(BaseModel):
    items: list[ScenarioRef]


class MessageList(BaseModel):
    items: list[DisplayMessage]


class SessionEnded(BaseModel):
    session_id: str
    status: Literal["done"] = "done"


class Health(BaseModel):
    status: Literal["ok"] = "ok"
    database: Literal["ok", "not_configured"]
