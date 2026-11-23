"""Teacher eval cases: schema and loader for ``evals/cases/teacher/*.yaml``.

Each file is ``{reviewed_by: <name or PLACEHOLDER>, cases: [...]}``. A case is one learner line
(optionally after a few earlier turns) plus the deterministic expectations for the teacher's
reply and the rubric items an LLM judge scores.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from ea_llm.adapters.memory import load_seed_personas, load_seed_scenarios
from ea_llm.models import AgeBand, CefrLevel

CASES_DIR = Path(__file__).resolve().parent / "cases" / "teacher"
PLACEHOLDER = "PLACEHOLDER"

# Rubric items the judge may score; the definitions live in judge_instructions.md.
RUBRIC_ITEMS = (
    "correction_quality",
    "explanation_correct",
    "natural_vietnamese",
    "level_appropriate",
    "conversation_flow",
    "no_overcorrection",
    "task_boundaries",
    "checks_understanding",
)


class HistoryTurn(BaseModel):
    learner: str
    teacher: str  # tagged text, e.g. "<vi>...</vi><en>...</en>"


class Expect(BaseModel):
    """Deterministic checks on one teacher reply. Every field is optional."""

    terminal: list[Literal["ask_question", "open_turn"]] | None = None  # allowed ending
    must_correct: list[str] = Field(default_factory=list)  # each item: "alt1|alt2"
    must_not_correct: list[str] = Field(default_factory=list)  # words no correction may touch
    no_correction: bool = False
    max_corrections: int | None = None
    vi_ratio: tuple[float, float] | None = None  # share of Vietnamese words in this reply
    require_vi: bool = False
    en_only: bool = False
    forbid: list[str] = Field(default_factory=list)  # substrings that must not appear
    must_include_any: list[str] = Field(default_factory=list)
    max_sentences: int | None = None
    ends_with_question: bool = False
    no_flags: list[str] = Field(default_factory=list)  # guard flags that must not be raised


class EvalCase(BaseModel):
    id: str
    mode: Literal["scenario", "free_chat"]
    channel: Literal["text"] = "text"
    scenario: str | None = None  # seed scenario id, scenario mode only
    persona: str = "lan"
    age_band: AgeBand = "adult"
    cefr: CefrLevel = "A2"
    interests: list[str] = Field(default_factory=list)
    history: list[HistoryTurn] = Field(default_factory=list)
    learner: str
    expect: Expect = Field(default_factory=Expect)
    judge: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    notes: str = ""
    reviewed_by: str = PLACEHOLDER  # filled from the file header

    @model_validator(mode="after")
    def _check(self) -> EvalCase:
        if (self.mode == "scenario") != (self.scenario is not None):
            raise ValueError(f"{self.id}: scenario is required in scenario mode, only there")
        unknown = set(self.judge) - set(RUBRIC_ITEMS)
        if unknown:
            raise ValueError(f"{self.id}: unknown judge items {sorted(unknown)}")
        if self.expect.no_correction and self.expect.must_correct:
            raise ValueError(f"{self.id}: no_correction conflicts with must_correct")
        if self.expect.en_only and self.expect.require_vi:
            raise ValueError(f"{self.id}: en_only conflicts with require_vi")
        return self


def load_cases(directory: Path = CASES_DIR) -> list[EvalCase]:
    """All cases, validated against the seed scenarios and teachers; ids must be unique."""
    scenarios = {s.id for s in load_seed_scenarios()}
    personas = {p.id for p in load_seed_personas()}
    cases: list[EvalCase] = []
    for path in sorted(directory.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        reviewer = str(raw.get("reviewed_by") or PLACEHOLDER)
        for item in raw.get("cases") or []:
            case = EvalCase.model_validate({**item, "reviewed_by": reviewer})
            if case.scenario is not None and case.scenario not in scenarios:
                raise ValueError(f"{path.name}: {case.id}: unknown scenario {case.scenario!r}")
            if case.persona not in personas:
                raise ValueError(f"{path.name}: {case.id}: unknown persona {case.persona!r}")
            cases.append(case)
    ids = [c.id for c in cases]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"duplicate case ids: {duplicates}")
    return cases
