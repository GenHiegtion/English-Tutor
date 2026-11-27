"""Teacher tools: argument models, per-channel/mode tool sets and validation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from english_tutor.providers.base import ToolSpec
from english_tutor.teacher.context import Channel, Mode


class AskRepeatArgs(BaseModel):
    target_text: str
    focus_words: list[str] = Field(default_factory=list)
    slow_first: bool = False


class AskQuestionArgs(BaseModel):
    question: str = Field(
        description="The question you just asked the learner, copied word for word "
        "(not an instruction about it)."
    )
    expected_structure: str = Field(
        description="The English structure a good answer uses, e.g. 'My favourite ... is ...'."
    )
    model_answer: str | None = Field(
        default=None, description="A short sample answer at the learner's level."
    )


class ReasonArgs(BaseModel):
    reason: str


class ShowBoardArgs(BaseModel):
    text: str
    ipa: str | None = None
    highlights: list[str] = Field(default_factory=list)
    note_vi: str | None = None


class RecordMistakeArgs(BaseModel):
    type: Literal["pronunciation", "grammar", "vocabulary", "spelling"]
    original: str = Field(description="Only the wrong words, as the learner wrote them.")
    corrected: str = Field(description="The corrected words.")
    explanation_vi: str = Field(description="One short sentence in Vietnamese: the rule.")
    subtype: str | None = None


_DEFINITIONS: dict[str, tuple[str, type[BaseModel], bool]] = {
    "ask_repeat": (
        "End the turn by asking the learner to repeat a target sentence.",
        AskRepeatArgs,
        True,
    ),
    "ask_question": (
        "End the turn with a question for the learner to answer.",
        AskQuestionArgs,
        True,
    ),
    "next_step": ("End the turn and move on to the next lesson step.", ReasonArgs, True),
    "end_lesson": ("End the turn and finish the lesson.", ReasonArgs, True),
    "show_board": (
        "Show text on the learner's board (a word, sentence, IPA or note).",
        ShowBoardArgs,
        False,
    ),
    "record_mistake": (
        "Record a mistake you just corrected, so it can be reviewed later.",
        RecordMistakeArgs,
        False,
    ),
}


def allowed_tool_names(channel: Channel, mode: Mode) -> list[str]:
    names = ["ask_question", "show_board", "record_mistake"]
    if mode == "lesson":
        names = [
            "ask_repeat",
            "ask_question",
            "next_step",
            "end_lesson",
            "show_board",
            "record_mistake",
        ]
    return names


def _schema(model: type[BaseModel], channel: Channel) -> dict:
    schema = model.model_json_schema()
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    schema.pop("title", None)
    if model is RecordMistakeArgs:
        allowed = ["grammar", "vocabulary", "spelling" if channel == "text" else "pronunciation"]
        schema["properties"]["type"]["enum"] = allowed
    return schema


def tools_for(channel: Channel, mode: Mode) -> list[ToolSpec]:
    """Deterministic list, so the tools layer is byte-identical for every user."""
    specs = []
    for name in allowed_tool_names(channel, mode):
        description, model, terminal = _DEFINITIONS[name]
        specs.append(
            ToolSpec(
                name=name,
                description=description,
                input_schema=_schema(model, channel),
                terminal=terminal,
            )
        )
    return specs


def is_terminal(name: str) -> bool:
    return name in _DEFINITIONS and _DEFINITIONS[name][2]


def validate_args(name: str, args: dict, channel: Channel) -> tuple[BaseModel | None, str | None]:
    """Return (parsed, None) or (None, field name that failed)."""
    model = _DEFINITIONS[name][1]
    try:
        parsed = model.model_validate(args)
    except ValidationError as exc:
        return None, str(exc.errors()[0]["loc"][0]) if exc.errors()[0]["loc"] else "args"
    if isinstance(parsed, RecordMistakeArgs):
        forbidden = "pronunciation" if channel == "text" else "spelling"
        if parsed.type == forbidden:
            return None, "type"
    return parsed, None
