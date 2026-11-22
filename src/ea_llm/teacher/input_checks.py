"""Cheap, deterministic checks on the learner's text before it is sent to the LLM."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from ea_llm.teacher.context import Channel, LearnerInput

MAX_CHARS = 600
WRONG_SCRIPT_RATIO = 0.30
LOW_STT_CONFIDENCE = 0.6
MIN_WORDS_COMPLETE = 3
DANGLING_WORDS = frozenset(
    [
        "the",
        "a",
        "an",
        "to",
        "and",
        "but",
        "because",
        "of",
        "in",
        "at",
        "my",
        "on",
        "for",
        "with",
        "or",
        "so",
        "if",
        "that",
        "is",
        "are",
        "was",
    ]
)


@dataclass
class InputCheck:
    text: str
    notes: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    skip_text: bool = False  # voice only: wrong language, send a note instead of the text


def _non_latin_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    foreign = sum(1 for c in letters if "LATIN" not in unicodedata.name(c, ""))
    return foreign / len(letters)


def check_input(channel: Channel, learner: LearnerInput) -> InputCheck:
    result = InputCheck(text=learner.text.strip())

    if channel == "text" and len(result.text) > MAX_CHARS:
        result.text = result.text[:MAX_CHARS]
        result.notes.append("message was long and has been cut")
        result.flags.append("input_truncated")

    if _non_latin_ratio(result.text) > WRONG_SCRIPT_RATIO:
        if channel == "voice":
            result.skip_text = True
            result.notes.append("could not hear clearly")
            result.flags.append("stt_wrong_lang")
        else:
            result.notes.append("the learner wrote in another language")
            result.flags.append("wrong_script")

    if channel == "voice" and not result.skip_text:
        words = result.text.lower().rstrip(".,!?").split()
        dangling = bool(words) and words[-1] in DANGLING_WORDS and learner.end_reason != "tap"
        if dangling or (0 < len(words) < MIN_WORDS_COMPLETE and learner.end_reason != "tap"):
            result.notes.append("the sentence may be unfinished")
            result.flags.append("maybe_cut_off")
        if learner.stt_confidence is not None and learner.stt_confidence < LOW_STT_CONFIDENCE:
            result.notes.append("the transcript may be wrong")
            result.flags.append("stt_low_confidence")
    return result
