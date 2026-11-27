"""Judging teacher replies: deterministic rule checks and the LLM-judge handoff.

The LLM judge is decoupled from this code: ``build_judge_prompt`` writes one self-contained
markdown file (fixed instructions + every case to score), any capable model reads it and writes
``judge_scores.jsonl``, and ``load_judge_scores`` validates that file. The same file works for a
Claude Code subagent today and an API judge later.
"""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from english_tutor.evals.cases import EvalCase
from english_tutor.teacher.guards import vi_ratio
from english_tutor.teacher.parser import Segment

# Guard flags that fail any case: broken output, wrong pronouns, bad tools, blocked content.
HARD_FAIL_FLAGS = frozenset(
    {
        "address_mismatch",
        "blocked_content",
        "dropped_chars",
        "empty_turn",
        "format_leak",
        "invalid_tool_args",
        "max_tokens",
        "multi_terminal",
        "refusal",
        "score_spoken",
        "stream_error",
        "tool_not_allowed",
        "unclosed_tag",
    }
)
_SENTENCE_END_RE = re.compile(r"[.!?…]+(\s|$)")
_PUNCT_RE = re.compile(r"[\"'“”‘’.,!?;:()]")


class SegmentOut(BaseModel):
    lang: str
    text: str


class MistakeOut(BaseModel):
    type: str
    original: str
    corrected: str
    explanation_vi: str = ""


class CheckOutcome(BaseModel):
    passed: bool
    detail: str = ""


class CaseResult(BaseModel):
    """What one case produced, plus the outcome of every rule check."""

    id: str
    mode: str
    cefr: str
    persona: str
    learner: str
    model: str = ""
    prompt_version: str = ""
    segments: list[SegmentOut] = Field(default_factory=list)
    mistakes: list[MistakeOut] = Field(default_factory=list)
    terminal: str = "open_turn"
    terminal_args: dict = Field(default_factory=dict)
    flags: list[str] = Field(default_factory=list)
    ttft_ms: int | None = None
    cost_usd: float = 0.0
    failed: str | None = None  # TurnFailed reason
    checks: dict[str, CheckOutcome] = Field(default_factory=dict)
    reviewed: bool = False

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks.values())

    def text(self) -> str:
        return " ".join(s.text for s in self.segments)


class JudgeScore(BaseModel):
    id: str
    scores: dict[str, int]
    issues: str = ""


def _norm(text: str) -> str:
    return " ".join(_PUNCT_RE.sub(" ", text.lower()).split())


def _contains(haystack: str, needle: str) -> bool:
    return f" {_norm(needle)} " in f" {_norm(haystack)} "


def check_case(case: EvalCase, result: CaseResult) -> dict[str, CheckOutcome]:
    """Rule checks for one reply: always-on checks plus the case's ``expect`` block."""
    checks: dict[str, CheckOutcome] = {}

    def add(name: str, passed: bool, detail: str = "") -> None:
        checks[name] = CheckOutcome(passed=passed, detail="" if passed else detail)

    if result.failed:
        add("turn_completed", False, f"turn failed: {result.failed}")
        return checks
    add("turn_completed", True)
    hard = sorted(HARD_FAIL_FLAGS & set(result.flags))
    add("clean_output", not hard, f"flags: {', '.join(hard)}")
    add("has_reply", bool(result.segments), "no segment")

    text = result.text()
    if result.terminal == "ask_question":
        question = str(result.terminal_args.get("question", ""))
        add(
            "question_said",
            bool(question) and _norm(question) in _norm(text),
            f"ask_question {question!r} is not in the reply the learner saw",
        )

    exp = case.expect
    corrections = " ".join(f"{m.original} {m.corrected}" for m in result.mistakes)
    if exp.terminal is not None:
        add(
            "terminal",
            result.terminal in exp.terminal,
            f"got {result.terminal}, want {'/'.join(exp.terminal)}",
        )
    for item in exp.must_correct:
        alternatives = [a.strip() for a in item.split("|")]
        fixed = " ".join(m.corrected for m in result.mistakes) + " " + text
        add(
            f"must_correct[{alternatives[0]}]",
            any(_contains(fixed, a) for a in alternatives),
            f"none of {alternatives} in the corrections or reply",
        )
    for word in exp.must_not_correct:
        add(
            f"must_not_correct[{word}]",
            not _contains(corrections, word),
            f"a correction touches {word!r}",
        )
    if exp.no_correction:
        add(
            "no_correction",
            not result.mistakes,
            f"recorded {[f'{m.original} -> {m.corrected}' for m in result.mistakes]}",
        )
    if exp.max_corrections is not None:
        add(
            "max_corrections",
            len(result.mistakes) <= exp.max_corrections,
            f"{len(result.mistakes)} > {exp.max_corrections}",
        )
    segments = [Segment(seq=i, lang=s.lang, text=s.text) for i, s in enumerate(result.segments)]  # type: ignore[arg-type]
    if exp.vi_ratio is not None:
        ratio = (vi_ratio(segments) or 0.0) / 100
        lo, hi = exp.vi_ratio
        add("vi_ratio", lo <= ratio <= hi, f"{ratio:.2f} not in [{lo}, {hi}]")
    langs = {s.lang for s in result.segments}
    if exp.require_vi:
        add("require_vi", "vi" in langs, "no Vietnamese segment")
    if exp.en_only:
        add("en_only", langs <= {"en"}, "has a Vietnamese segment")
    searchable = text + " " + json.dumps(result.terminal_args, ensure_ascii=False)
    for phrase in exp.forbid:
        add(f"forbid[{phrase}]", phrase.lower() not in searchable.lower(), f"says {phrase!r}")
    if exp.must_include_any:
        add(
            "must_include_any",
            any(_contains(text, p) for p in exp.must_include_any),
            f"none of {exp.must_include_any}",
        )
    if exp.max_sentences is not None:
        count = sum(len(_SENTENCE_END_RE.findall(s.text)) or 1 for s in result.segments)
        add("max_sentences", count <= exp.max_sentences, f"{count} > {exp.max_sentences}")
    if exp.ends_with_question:
        last = result.segments[-1].text.rstrip() if result.segments else ""
        add(
            "ends_with_question",
            result.terminal == "ask_question" or last.endswith("?"),
            "the reply does not end with a question",
        )
    for flag in exp.no_flags:
        add(f"no_flag[{flag}]", flag not in result.flags, f"raised {flag}")
    return checks


# ---------------------------------------------------------------------------- LLM judge
def judge_instructions() -> str:
    path = resources.files("english_tutor.evals").joinpath("judge_instructions.md")
    return path.read_text(encoding="utf-8").strip()


def _render_reply(result: CaseResult) -> str:
    lines = [f"<{s.lang}>{s.text}</{s.lang}>" for s in result.segments] or ["(no reply)"]
    for m in result.mistakes:
        lines.append(f"[record_mistake {m.type}: {m.original!r} -> {m.corrected!r}]")
    ending = result.terminal
    if result.terminal_args.get("question"):
        ending += f": {result.terminal_args['question']!r}"
    lines.append(f"[ending: {ending}]")
    return "\n".join(lines)


def build_judge_prompt(pairs: list[tuple[EvalCase, CaseResult]], scores_path: Path) -> str:
    """One markdown document: instructions, then every case that has rubric items."""
    blocks = [judge_instructions(), "", f"Write the scores to: `{scores_path}`", "", "# Cases"]
    for case, result in pairs:
        if not case.judge or result.failed:
            continue
        context = f"mode {case.mode}, learner CEFR {case.cefr}, teacher {case.persona}, "
        context += f"learner age band {case.age_band}"
        if case.scenario:
            context += f", scenario {case.scenario}"
        history = "\n".join(f"Learner: {h.learner}\nTeacher: {h.teacher}" for h in case.history)
        blocks += [
            "",
            f"## {case.id}",
            f"Context: {context}.",
            f"Score these items: {', '.join(case.judge)}",
        ]
        if case.notes:
            blocks.append(f"What the case tests: {case.notes}")
        if history:
            blocks += ["Earlier turns:", "```", history, "```"]
        blocks += [
            f"Learner: {case.learner}",
            "Teacher reply:",
            "```",
            _render_reply(result),
            "```",
        ]
    return "\n".join(blocks) + "\n"


def load_judge_scores(path: Path, cases: dict[str, EvalCase]) -> dict[str, JudgeScore]:
    """Parse ``judge_scores.jsonl``; every score must be a requested item scored 1-5."""
    scores: dict[str, JudgeScore] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            score = JudgeScore.model_validate_json(line)
        except ValidationError as exc:
            raise ValueError(f"{path.name}:{n}: {exc.errors()[0]['msg']}") from exc
        case = cases.get(score.id)
        if case is None:
            raise ValueError(f"{path.name}:{n}: unknown case {score.id!r}")
        extra = set(score.scores) - set(case.judge)
        if extra:
            raise ValueError(f"{path.name}:{n}: {score.id} scored unrequested items {extra}")
        if any(not 1 <= v <= 5 for v in score.scores.values()):
            raise ValueError(f"{path.name}:{n}: {score.id} has a score outside 1-5")
        scores[score.id] = score
    return scores
