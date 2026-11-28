"""Guards: blocking checks per segment and flag-only checks after the turn."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

from english_tutor.models import Address, CefrLevel
from english_tutor.teacher.parser import Segment, count_words
from english_tutor.teacher.persona import MAX_WORDS_PER_SENTENCE, VI_TARGET_PCT
from english_tutor.teacher.wordlist import MAX_UNKNOWN_SHARE, CefrWordlist

VI_RATIO_TOLERANCE = 20  # percentage points
# Vietnamese emergency lines the prompt may give a learner in distress: child protection,
# police, fire, ambulance. They need no source.
HOTLINES = frozenset({111.0, 113.0, 114.0, 115.0})
MAX_SENTENCES = 4
MAX_MISTAKES_PER_TURN = 2

_MARKDOWN_RE = re.compile(r"\*\*|__|`|^\s*#+\s|^\s*[-*•]\s", re.M)
_EMOJI_RE = re.compile("[\U0001f300-\U0001faff☀-➿\U0001f000-\U0001f2ff️‍]", re.UNICODE)
_SCORE_RE = re.compile(
    r"\b\d+([.,]\d+)?\s*(%|/\s*100|points?|percent|điểm)|\b(điểm|score)\b\D{0,12}\d+", re.I
)
_PERSONAL_INFO_RE = re.compile(
    r"số điện thoại|địa chỉ nhà|phone number|home address|học trường nào|which school", re.I
)
_QUOTED_RE = re.compile(r"\"[^\"]*\"|“[^”]*”|'[^']*'|‘[^’]*’")
_COMPOUND_RE = re.compile(
    r"bạn bè|bạn học|bạn cùng lớp|bạn thân|bạn trai|bạn gái|người bạn|kết bạn|chúng tôi", re.I
)
_SENTENCE_END_RE = re.compile(r"[.!?…]+(\s|$)")
_WORD_RE = re.compile(r"[^\W\d_]+")
# Vietnamese written without diacritics; none of these is a common English word.
_PLAIN_VIETNAMESE = (
    "ko khong dc duoc gi vay sao nghia biet noi minh toi lam nhu nao cua voi ve thich nhe nha roi "
    "chua giup hieu tieng viet"
)
_PLAIN_VIETNAMESE_WORDS = frozenset(_PLAIN_VIETNAMESE.split())
VIETNAMESE_WORD_SHARE = 0.5  # more than half the words
_SMALL_NUMBERS = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty"
)
_NUMBER_WORDS = {
    word: value
    for value, word in enumerate(_SMALL_NUMBERS.split())
    if word != "one"  # mostly a pronoun ("which one"), so it is never treated as a number
} | {
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundred": 100,
    "thousand": 1000,
    "million": 1_000_000,
    "billion": 1_000_000_000,
}
_NUMBER_RE = re.compile(
    r"\b(\d+(?:[.,]\d+)*|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")\b",
    re.I,
)
BLOCKED_TERMS_PATH = Path(__file__).resolve().parent.parent / "data" / "blocked_terms.yaml"


@cache
def _blocked_re() -> re.Pattern[str]:
    raw = yaml.safe_load(BLOCKED_TERMS_PATH.read_text(encoding="utf-8")) or {}
    terms = [
        _nfc(str(t).strip().lower()) for group in (raw.get("terms") or {}).values() for t in group
    ]
    alternatives = "|".join(re.escape(t) for t in sorted(set(terms), key=len, reverse=True) if t)
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.I)


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def contains_blocked_term(text: str) -> bool:
    return bool(_blocked_re().search(_nfc(text)))


@dataclass
class BlockResult:
    segment: Segment | None  # None when the segment must be dropped
    flags: list[str]


def check_segment(segment: Segment) -> BlockResult:
    """Cheap deterministic checks run on every segment before it is emitted."""
    flags: list[str] = []
    text = segment.text
    if contains_blocked_term(text):
        return BlockResult(None, ["blocked_content"])
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


def looks_vietnamese(text: str) -> bool:
    """Whether a learner message is mostly Vietnamese (with or without diacritics)."""
    words = _WORD_RE.findall(_nfc(text).lower())
    if not words:
        return False
    vietnamese = sum(not w.isascii() or w in _PLAIN_VIETNAMESE_WORDS for w in words)
    return vietnamese / len(words) > VIETNAMESE_WORD_SHARE


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
    grounding: Iterable[str] = (),
    check_numbers: bool = True,
    wordlist: CefrWordlist | None = None,
    exempt_words: Iterable[str] = (),
) -> list[str]:
    """Flag-only checks; they never change what the learner already received.

    ``grounding`` holds the texts a number may come from (learner messages, tool results,
    scenario data). ``check_numbers`` is off where inventing details is the point (role-play).
    """
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

    if _overshoots_level(segments, cefr, wordlist, exempt_words):
        flags.append("cefr_overshoot")

    if check_numbers and ungrounded_numbers(segments, grounding):
        flags.append("ungrounded_number")

    if any(_PERSONAL_INFO_RE.search(s.text) for s in segments):
        flags.append("asked_personal_info")
    return flags


def _overshoots_level(
    segments: list[Segment],
    cefr: CefrLevel,
    wordlist: CefrWordlist | None,
    exempt_words: Iterable[str],
) -> bool:
    """An English sentence is too long for the level, or uses too many words above it."""
    limit = MAX_WORDS_PER_SENTENCE[cefr]
    if limit is None:
        return False
    exempt = frozenset(w.lower() for phrase in exempt_words for w in phrase.split())
    for seg in segments:
        if seg.lang != "en" or seg.protected:
            continue
        for part in _SENTENCE_END_RE.split(seg.text):
            if not part or part.isspace():
                continue
            if count_words(part) > limit:
                return True
            if wordlist and wordlist.unknown_share(part, cefr, exempt) > MAX_UNKNOWN_SHARE:
                return True
    return False


def _number_values(text: str) -> set[float]:
    values: set[float] = set()
    for match in _NUMBER_RE.finditer(text):
        token = match.group(1).lower()
        if token in _NUMBER_WORDS:
            values.add(float(_NUMBER_WORDS[token]))
        else:
            try:
                values.add(float(token.replace(",", "")))
            except ValueError:  # "1.234.567": keep the digits only
                values.add(float(re.sub(r"\D", "", token)))
    return values


def ungrounded_numbers(segments: list[Segment], grounding: Iterable[str]) -> set[float]:
    """Numbers the teacher said that appear in none of the grounding texts."""
    said = set().union(*(_number_values(s.text) for s in segments)) if segments else set()
    if not said:
        return set()
    known = set().union(HOTLINES, *(_number_values(t) for t in grounding))
    return said - known
