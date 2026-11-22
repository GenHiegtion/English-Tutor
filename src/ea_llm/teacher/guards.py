"""Guards: blocking checks per segment and flag-only checks after the turn."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ea_llm.models import Address, CefrLevel
from ea_llm.teacher.parser import Segment, count_words
from ea_llm.teacher.persona import MAX_WORDS_PER_SENTENCE, VI_TARGET_PCT

VI_RATIO_TOLERANCE = 20  # percentage points
MAX_SENTENCES = 4
MAX_MISTAKES_PER_TURN = 2

_MARKDOWN_RE = re.compile(r"\*\*|__|`|^\s*#+\s|^\s*[-*•]\s", re.M)
_EMOJI_RE = re.compile("[\U0001f300-\U0001faff☀-➿\U0001f000-\U0001f2ff️‍]", re.UNICODE)
_SCORE_RE = re.compile(
    r"\b\d+([.,]\d+)?\s*(%|/\s*100|points?|percent|điểm)|\b(điểm|score)\D{0,12}\d+", re.I
)
_PERSONAL_INFO_RE = re.compile(
    r"số điện thoại|địa chỉ nhà|phone number|home address|học trường nào|which school", re.I
)
_QUOTED_RE = re.compile(r"\"[^\"]*\"|“[^”]*”|'[^']*'|‘[^’]*’")
_COMPOUND_RE = re.compile(r"bạn bè|bạn học|bạn cùng lớp|chúng tôi|bạn thân", re.I)
_SENTENCE_END_RE = re.compile(r"[.!?…]+(\s|$)")


@dataclass
class BlockResult:
    segment: Segment | None  # None when the segment must be dropped
    flags: list[str]


def check_segment(segment: Segment) -> BlockResult:
    """Cheap deterministic checks run on every segment before it is emitted."""
    flags: list[str] = []
    text = segment.text
    if _MARKDOWN_RE.search(text) or _EMOJI_RE.search(text):
        text = _EMOJI_RE.sub("", _MARKDOWN_RE.sub("", text)).strip()
        flags.append("format_leak")
    if _SCORE_RE.search(text):
        return BlockResult(None, [*flags, "score_spoken"])
    if not text:
        return BlockResult(None, flags)
    if text != segment.text:
        segment = Segment(**{**segment.__dict__, "text": text})
    return BlockResult(segment, flags)


def vi_ratio(segments: list[Segment]) -> float | None:
    vi = sum(count_words(s.text) for s in segments if s.lang == "vi")
    total = sum(count_words(s.text) for s in segments)
    return None if total == 0 else 100 * vi / total


def check_turn(
    segments: list[Segment],
    *,
    address: Address,
    cefr: CefrLevel,
    mistakes_recorded: int,
    recent_vi_ratio: float | None,
) -> list[str]:
    """Flag-only checks; they never change what the learner already received."""
    flags: list[str] = []

    vi_text = " ".join(_QUOTED_RE.sub("", s.text) for s in segments if s.lang == "vi")
    vi_text = _COMPOUND_RE.sub("", vi_text)
    if (address.learner != "bạn" and re.search(r"\bbạn\b", vi_text, re.I)) or (
        address.teacher != "tôi" and re.search(r"\btôi\b", vi_text, re.I)
    ):
        flags.append("address_mismatch")

    if mistakes_recorded > MAX_MISTAKES_PER_TURN:
        flags.append("too_many_corrections")

    sentences = sum(len(_SENTENCE_END_RE.findall(s.text)) or 1 for s in segments)
    if sentences > MAX_SENTENCES:
        flags.append("too_long")

    if (
        recent_vi_ratio is not None
        and abs(recent_vi_ratio - VI_TARGET_PCT[cefr]) > VI_RATIO_TOLERANCE
    ):
        flags.append("vi_ratio_off")

    limit = MAX_WORDS_PER_SENTENCE[cefr]
    if limit is not None:
        for seg in segments:
            if seg.lang != "en" or seg.protected:
                continue
            parts = [p for p in _SENTENCE_END_RE.split(seg.text) if p and not p.isspace()]
            if any(count_words(p) > limit for p in parts):
                flags.append("cefr_overshoot")
                break

    if any(_PERSONAL_INFO_RE.search(s.text) for s in segments):
        flags.append("asked_personal_info")
    return flags
