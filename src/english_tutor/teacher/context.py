"""Input of one turn: TurnContext and related types."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, model_validator

Channel = Literal["voice", "text"]
Mode = Literal["lesson", "free_chat", "scenario"]


class LearnerInput(BaseModel):
    text: str  # voice: STT transcript; text: typed message
    source: Literal["stt", "typed"]  # voice -> stt, text -> typed
    stt_confidence: float | None = None  # voice only
    end_reason: Literal["tap", "vad", "timeout"] | None = None  # voice only
    silence_seconds: int | None = None  # voice only: a "silence" turn


class OpenerSegment(BaseModel):
    lang: Literal["vi", "en"]
    text: str
    slow: bool = False


class Opener(BaseModel):  # prepared opening line
    segments: list[OpenerSegment]
    tags: list[str] = []


class OpenerTerminal(BaseModel):
    tool: Literal["ask_repeat", "ask_question"]
    args: dict


class LessonStepRef(BaseModel):
    lesson_id: str
    step_index: int  # starts at 1
    total_steps: int
    step_name: str
    goal: str
    model_sentences: list[str]
    pass_criteria_text: str
    max_attempts: int
    vi_target_pct: int
    openers: list[Opener] = []
    opener_terminal: OpenerTerminal | None = None


class ScenarioRef(BaseModel):  # matches the app's scenarios table
    id: str
    title: str
    level: str
    goal: str
    goal_vi: str | None = None
    opener: str  # English opening line
    targets: list[str]  # target words and phrases


class PronSummary(BaseModel):  # voice only
    best_accuracy: int | None = None


class TurnContext(BaseModel):
    session_id: str
    user_id: str
    turn_id: str
    channel: Channel
    mode: Mode
    persona_id: str
    step: LessonStepRef | None = None
    scenario: ScenarioRef | None = None
    terminal_result: dict | None = None  # result of the previous turn's terminal tool
    step_changed: bool = False
    learner_input: LearnerInput | None = None  # None when the system starts the turn
    pron_result: PronSummary | None = None  # voice only
    received_at: float

    @model_validator(mode="after")
    def _check(self) -> TurnContext:
        li = self.learner_input
        if self.channel == "text":
            if self.pron_result is not None:
                raise ValueError("kênh text không có pron_result")
            if li is not None:
                if li.source != "typed":
                    raise ValueError("kênh text phải có source = typed")
                if any(
                    v is not None for v in (li.stt_confidence, li.end_reason, li.silence_seconds)
                ):
                    raise ValueError(
                        "kênh text không có stt_confidence, end_reason, silence_seconds"
                    )
        elif li is not None and li.source != "stt":
            raise ValueError("kênh voice phải có source = stt")
        if self.mode == "lesson" and self.step is None:
            raise ValueError("mode lesson cần step")
        if self.mode != "lesson" and self.step is not None:
            raise ValueError("step chỉ có ở mode lesson")
        if self.mode == "scenario" and self.scenario is None:
            raise ValueError("mode scenario cần scenario")
        if self.mode != "scenario" and self.scenario is not None:
            raise ValueError("scenario chỉ có ở mode scenario")
        return self
