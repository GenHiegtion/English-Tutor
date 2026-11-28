"""LangTagParser: streamed tokens -> `<vi>` / `<en>` segments.

The parser processes one character at a time, so the result never depends on how the stream is
chunked: feeding a text at once or one character at a time yields exactly the same segments."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

Lang = Literal["vi", "en"]

TERMINATORS = frozenset(".?!…")
CLOSERS = frozenset("\"'”’)»")
MIN_FLUSH_CHARS = 12  # flush rule 2: minimum buffer before flushing at sentence punctuation
MAX_BUFFER_CHARS = 120  # flush rule 3: force a cut
MAX_TAG_BODY = 16  # no `>` within this many chars: the `<` is plain text
PROTECTED_MAX_WORDS = 12
EN_AS_VI_RATIO = 0.5
MAX_DROPPED_SAMPLES = 3
MAX_DROPPED_SAMPLE_CHARS = 80

ABBREVIATIONS = frozenset(
    {"mr", "mrs", "ms", "dr", "st", "vs", "etc", "e.g", "i.e", "a.m", "p.m", "u.s", "tp"}
)

_TAG_RE = re.compile(r"^(/?)([A-Za-z][A-Za-z0-9]*)(.*)$", re.S)
_SLOW_RE = re.compile(r"""\bslow\s*=\s*["']?true["']?""", re.I)
_ABBREV_TOKEN_RE = re.compile(r"([A-Za-z][A-Za-z.]*)$")

# đ/Đ do not decompose in NFD; ă â ê ô ơ ư decompose into letter + breve/circumflex/horn.
_VI_MARKS = frozenset("\u0306\u0302\u031b\u0300\u0301\u0309\u0303\u0323")


@dataclass(frozen=True)
class Segment:
    seq: int
    lang: Lang
    text: str
    slow: bool = False
    final: bool = False  # emitted because the tag closed or the stream ended
    protected: bool = False  # voice only: a whole `<en>` tag of at most 12 words
    en_has_vi: bool = False  # the `<en>` text contains words with Vietnamese diacritics


def has_vietnamese_marks(word: str) -> bool:
    """True if the word has Vietnamese letters outside ASCII (đ, ă â ê ô ơ ư, tone marks)."""
    if "đ" in word or "Đ" in word:
        return True
    return any(ch in _VI_MARKS for ch in unicodedata.normalize("NFD", word))


def count_words(text: str) -> int:
    return sum(1 for token in text.split() if any(c.isalnum() for c in token))


class LangTagParser:
    def __init__(self, channel: Literal["voice", "text"] = "text") -> None:
        self.channel = channel
        self.dropped_chars = 0
        self.dropped_samples: list[str] = []
        self.unclosed_tag = False
        self.en_has_vi = False
        self._seq = 0
        self._lang: Lang | None = None
        self._slow = False
        self._buf = ""
        self._run_start: int | None = None  # start of a punctuation run waiting for the next char
        self._tag: list[str] | None = None  # characters collected after `<`
        self._emitted_in_tag = 0
        self._dropped_run = ""

    # ------------------------------------------------------------------ API
    def feed(self, chunk: str) -> list[Segment]:
        out: list[Segment] = []
        for ch in chunk:
            self._step(ch, out)
        return out

    def finish(self) -> list[Segment]:
        out: list[Segment] = []
        if self._tag is not None:  # dangling `<...`: treat as plain text
            pending = self._tag
            self._tag = None
            self._text("<", out)
            for ch in pending:
                self._step(ch, out)
        self._close_dropped_run()
        if self._lang is not None:
            self.unclosed_tag = True
            self._flush(out, final=True)
            self._lang = None
        return out

    # ------------------------------------------------------------ internals
    def _step(self, c: str, out: list[Segment]) -> None:
        queue = [c]
        while queue:
            ch = queue.pop(0)
            if self._tag is not None:
                self._step_in_tag(ch, out, queue)
            elif ch == "<":
                self._tag = []
            else:
                self._text(ch, out)

    def _step_in_tag(self, ch: str, out: list[Segment], queue: list[str]) -> None:
        assert self._tag is not None
        body = self._tag
        if ch == ">" and body:
            self._tag = None
            self._handle_tag("".join(body), out, queue)
            return
        invalid_start = not body and not ((ch.isalpha() and ch.isascii()) or ch == "/")
        if ch == "<" or invalid_start or len(body) + 1 > MAX_TAG_BODY:
            self._tag = None
            self._text("<", out)
            queue[0:0] = [*body, ch]
            return
        body.append(ch)

    def _handle_tag(self, body: str, out: list[Segment], queue: list[str]) -> None:
        match = _TAG_RE.match(body)
        if match is None:  # not a tag: give it back as text
            self._text("<", out)
            queue[0:0] = [*body, ">"]
            return
        closing, name, attrs = match.group(1) == "/", match.group(2).lower(), match.group(3)
        if name not in ("vi", "en"):
            return  # unknown tag: drop the tag, keep the text inside
        if closing:
            if self._lang is not None:
                self._flush(out, final=True)
                self._lang = None
            return
        if self._lang is not None:  # a new tag opened before the previous one closed
            self.unclosed_tag = True
            self._flush(out, final=True)
        self._close_dropped_run()
        self._lang = name  # type: ignore[assignment]
        self._slow = bool(_SLOW_RE.search(attrs))
        self._emitted_in_tag = 0

    def _text(self, c: str, out: list[Segment]) -> None:
        if self._lang is None:
            if not c.isspace():
                self.dropped_chars += 1
                self._dropped_run += c
            elif self._dropped_run and not self._dropped_run.endswith(" "):
                self._dropped_run += " "  # a run lasts until the next tag: keep whole phrases
            return

        if self._run_start is not None:
            if c in TERMINATORS or c in CLOSERS:
                self._buf += c
                return
            self._resolve_run(c, out)
        if c.isspace() and not self._buf:
            return
        self._buf += c
        if c in TERMINATORS:
            self._run_start = len(self._buf) - 1
        self._enforce_max_buffer(out)

    def _close_dropped_run(self) -> None:
        sample = self._dropped_run.strip()
        if sample and len(self.dropped_samples) < MAX_DROPPED_SAMPLES:
            self.dropped_samples.append(sample[:MAX_DROPPED_SAMPLE_CHARS])
        self._dropped_run = ""

    def _resolve_run(self, next_char: str, out: list[Segment]) -> None:
        """Decide whether to flush at a punctuation run, based on the next character."""
        start = self._run_start
        assert start is not None
        self._run_start = None
        if not next_char.isspace():
            return  # `3.5`, `e.g`, `a.m`: punctuation glued to the next character
        if len(self._buf.strip()) < MIN_FLUSH_CHARS:
            return
        if self._is_abbreviation(self._buf[:start], self._buf[start:]):
            return
        self._flush(out, final=False)

    @staticmethod
    def _is_abbreviation(before: str, run: str) -> bool:
        if run.strip("".join(CLOSERS)) != ".":
            return False
        token = _ABBREV_TOKEN_RE.search(before)
        return token is not None and token.group(1).lower() in ABBREVIATIONS

    def _enforce_max_buffer(self, out: list[Segment]) -> None:
        if self._run_start is not None or len(self._buf) <= MAX_BUFFER_CHARS:
            return
        cut = self._buf.rfind(",")
        if cut > 0:
            head, rest = self._buf[: cut + 1], self._buf[cut + 1 :]
        else:
            cut = self._buf.rfind(" ")
            if cut > 0:
                head, rest = self._buf[:cut], self._buf[cut + 1 :]
            else:
                head, rest = self._buf, ""
        self._buf = head
        self._flush(out, final=False)
        self._buf = rest.lstrip()

    def _flush(self, out: list[Segment], *, final: bool) -> None:
        assert self._lang is not None
        text = self._buf.strip()
        self._buf = ""
        self._run_start = None
        if not text:
            return

        lang: Lang = self._lang
        en_has_vi = False
        if lang == "en":
            words = [w for w in text.split() if any(c.isalnum() for c in w)]
            vi_words = sum(1 for w in words if has_vietnamese_marks(w))
            if vi_words:
                en_has_vi = True
                self.en_has_vi = True
                if words and vi_words / len(words) >= EN_AS_VI_RATIO:
                    lang = "vi"

        protected = (
            self.channel == "voice"
            and self._lang == "en"
            and lang == "en"
            and final
            and self._emitted_in_tag == 0
            and count_words(text) <= PROTECTED_MAX_WORDS
        )
        seq, self._seq = self._seq, self._seq + 1  # seq starts at 0
        self._emitted_in_tag += 1
        out.append(
            Segment(
                seq=seq,
                lang=lang,
                text=text,
                slow=self._slow,
                final=final,
                protected=protected,
                en_has_vi=en_has_vi,
            )
        )
