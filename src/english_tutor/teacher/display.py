"""What the learner sees of a stored message, for history screens and ``messages.text``."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from english_tutor.models import StoredMessage
from english_tutor.providers.base import TextPart
from english_tutor.teacher.parser import LangTagParser
from english_tutor.teacher.runner import LEARNER_PREFIX

_PREFIXES = tuple(f"{prefix} " for prefix in LEARNER_PREFIX.values())


class DisplaySegment(BaseModel):
    lang: Literal["vi", "en"]
    text: str


class DisplayMessage(BaseModel):
    """One line of the conversation as shown to the learner."""

    turn_id: str
    role: Literal["user", "assistant"]
    persona_id: str | None = None
    text: str  # plain text: the learner's words, or the teacher's segments joined
    segments: list[DisplaySegment] = []  # teacher messages only, with their language


def to_display(stored: StoredMessage) -> DisplayMessage | None:
    """The visible part of a stored message; None when nothing in it was shown to the learner.

    Learner messages keep only the learner's own text (no system notes or tool results).
    Teacher messages keep their ``<vi>``/``<en>`` segments, without tags or tool calls.
    """
    texts = [p.text for p in stored.message.content if isinstance(p, TextPart)]
    if stored.message.role == "user":
        said = [t[len(p) :] for t in texts for p in _PREFIXES if t.startswith(p)]
        if not said:
            return None
        return DisplayMessage(turn_id=stored.turn_id, role="user", text="\n".join(said))
    parser = LangTagParser()
    segments = [seg for text in texts for seg in parser.feed(text)] + parser.finish()
    if not segments:
        return None
    return DisplayMessage(
        turn_id=stored.turn_id,
        role="assistant",
        persona_id=stored.persona_id,
        text=" ".join(seg.text for seg in segments),
        segments=[DisplaySegment(lang=seg.lang, text=seg.text) for seg in segments],
    )


def display_text(stored: StoredMessage) -> str:
    """Plain visible text of a stored message ("" when nothing was shown)."""
    shown = to_display(stored)
    return shown.text if shown else ""
