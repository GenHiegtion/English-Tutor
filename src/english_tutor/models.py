"""Domain models shared by the ports, the prompt builder and the teacher runner."""

from __future__ import annotations

from string import Formatter
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from english_tutor.providers.base import Message
from english_tutor.teacher.context import Channel, Mode, ScenarioRef

CefrLevel = Literal["A1", "A2", "B1", "B2", "C1", "C2"]
AgeBand = Literal["under_18", "adult", "senior", "unknown"]
Gender = Literal["female", "male"]


class Address(BaseModel):
    """How the teacher refers to itself and to the learner (Vietnamese pronouns)."""

    teacher: str
    learner: str

    def fill(self, template: str, name: str = "") -> str:
        """Fill {teacher}/{learner} (capitalised: {Teacher}/{Learner}) and {name} in a template."""
        return template.format(
            teacher=self.teacher,
            learner=self.learner,
            Teacher=self.teacher[:1].upper() + self.teacher[1:],
            Learner=self.learner[:1].upper() + self.learner[1:],
            name=name,
        )


ADDRESS_FIELDS = frozenset({"teacher", "learner", "Teacher", "Learner"})
# Persona lines may also say the teacher's name, with the title of the address ("cô Lan").
TEMPLATE_FIELDS = ADDRESS_FIELDS | {"name"}
# When a voice filler plays (PLAN fillers table): after ask_repeat, after ask_question, while
# the learner is silent, and when the lesson moves to its next step.
FILLER_CONTEXTS = frozenset({"ask_repeat", "ask_question", "silence", "next_step"})
FILLERS_PER_CONTEXT = (3, 5)  # min, max


def _check_template(value: str) -> str:
    names = {name for _, name, _, _ in Formatter().parse(value) if name is not None}
    if not names <= TEMPLATE_FIELDS:
        raise ValueError(f"unknown placeholders {sorted(names - TEMPLATE_FIELDS)}")
    return value


class Persona(BaseModel):
    id: str
    name: str
    gender: Gender
    age_band: AgeBand  # the teacher's own age group
    personality: str  # English description, rendered into the session prompt layer
    # Shown to learners where they pick a teacher (the app's teacher_personas.description_vi);
    # never part of the prompt.
    description_vi: str = ""
    address: dict[AgeBand, Address]  # keyed by the learner's age band
    # Fixed Vietnamese line played when the learner switches to this teacher; a template with
    # {teacher}/{learner} ({Teacher}/{Learner} capitalised) and {name} so it fits every learner
    # age band.
    switch_greeting: str
    # Voice channel only: fixed Vietnamese templates by FILLER_CONTEXTS, 3-5 lines each. A
    # filler never hints at a result ("Đúng rồi", "Giỏi lắm").
    fillers: dict[str, list[str]] = Field(default_factory=dict)
    voice_id: str | None = None
    status: Literal["draft", "published"] = "published"

    @field_validator("switch_greeting")
    @classmethod
    def _known_placeholders(cls, value: str) -> str:
        return _check_template(value)

    @field_validator("fillers")
    @classmethod
    def _known_filler_contexts(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        unknown = sorted(set(value) - FILLER_CONTEXTS)
        if unknown:
            raise ValueError(f"unknown filler contexts {unknown}")
        low, high = FILLERS_PER_CONTEXT
        for context, lines in value.items():
            if not low <= len(lines) <= high:
                raise ValueError(f"{context}: {len(lines)} fillers, expected {low}-{high}")
            for line in lines:
                _check_template(line)
        return value


class LearnerProfile(BaseModel):
    user_id: str
    display_name: str | None = None
    cefr: CefrLevel = "A1"
    age_band: AgeBand = "unknown"
    interests: list[str] = Field(default_factory=list)
    recent_mistakes: list[str] = Field(default_factory=list)  # e.g. "go -> went"
    memory_notes: list[str] = Field(default_factory=list)  # high-confidence LearnerMemory lines


class SessionSnapshot(BaseModel):
    """Frozen at session open so prompt layer 3 stays byte-identical for the whole session."""

    session_id: str
    user_id: str
    channel: Channel
    mode: Mode
    persona_id: str
    address: Address
    learner: LearnerProfile
    prompt_version: str
    scenario: ScenarioRef | None = None
    window_start: int = 0  # index of the first stored message sent to the model


class SessionState(BaseModel):
    """A stored session as the API checks it before a request: snapshot and open/ended."""

    snapshot: SessionSnapshot
    closed: bool


class StoredMessage(BaseModel):
    turn_id: str
    message: Message
    persona_id: str | None = None  # who spoke (assistant messages)
    guard_flags: list[str] = Field(default_factory=list)
    tool_statuses: dict[str, dict] = Field(default_factory=dict)  # tool_use_id -> result payload


class UsageRecord(BaseModel):
    turn_id: str
    session_id: str
    user_id: str
    role: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None
    ttft_ms: int | None = None
    fallback_used: bool = False
    cancelled: bool = False


class TurnRecord(BaseModel):
    """Everything one turn writes; a real adapter stores it in a single transaction."""

    turn_id: str
    messages: list[StoredMessage]
    usage: list[UsageRecord] = []  # one row per LLM call: the answer, plus any cancelled call
