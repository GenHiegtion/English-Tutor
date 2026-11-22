"""Events the teacher sends to the caller."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class OutputSegment(BaseModel):
    kind: Literal["segment"] = "segment"
    turn_id: str
    seq: int  # increases within a turn, starts at 0
    lang: Literal["vi", "en"]
    text: str
    slow: bool = False  # only meaningful on the voice channel
    protected: bool = False  # voice channel only
    is_filler: bool = False  # voice channel only


class BoardUpdate(BaseModel):
    kind: Literal["board"] = "board"
    turn_id: str
    text: str
    ipa: str | None = None
    highlights: list[str] = []
    note_vi: str | None = None


class MistakeNoted(BaseModel):
    kind: Literal["mistake"] = "mistake"
    turn_id: str
    type: Literal["pronunciation", "grammar", "vocabulary", "spelling"]
    subtype: str | None = None
    original: str
    corrected: str
    explanation_vi: str


class TerminalAction(BaseModel):
    kind: Literal["terminal"] = "terminal"
    turn_id: str
    tool: Literal["ask_repeat", "ask_question", "next_step", "end_lesson", "open_turn"]
    tool_use_id: str | None = None
    args: dict = {}


class TurnEnd(BaseModel):
    kind: Literal["turn_end"] = "turn_end"
    turn_id: str
    ttft_ms: int | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    dropped_chars: int
    guard_flags: list[str]
    provider: str
    fallback_used: bool


class TurnFailed(BaseModel):
    kind: Literal["turn_failed"] = "turn_failed"
    turn_id: str
    reason: Literal["providers_down", "invalid_context", "quota_exceeded"]


TeacherEvent = Annotated[
    OutputSegment | BoardUpdate | MistakeNoted | TerminalAction | TurnEnd | TurnFailed,
    Field(discriminator="kind"),
]
