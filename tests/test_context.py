import pytest
from pydantic import ValidationError

from ea_llm.teacher.context import LearnerInput, LessonStepRef, ScenarioRef, TurnContext

SC = ScenarioRef(id="s", title="Cafe", level="A2", goal="Order", opener="Hi!", targets=["coffee"])
STEP = LessonStepRef(
    lesson_id="l",
    step_index=1,
    total_steps=3,
    step_name="Intro",
    goal="g",
    model_sentences=["Hi"],
    pass_criteria_text="p",
    max_attempts=3,
    vi_target_pct=50,
)


def ctx(**kw):
    base = dict(
        session_id="s",
        user_id="u",
        turn_id="t",
        channel="text",
        mode="scenario",
        persona_id="p",
        scenario=SC,
        learner_input=LearnerInput(text="hi", source="typed"),
        received_at=0.0,
    )
    base.update(kw)
    return TurnContext(**base)


def test_valid_text_scenario():
    assert ctx().mode == "scenario"
    assert ctx(learner_input=None).learner_input is None


@pytest.mark.parametrize(
    "kw",
    [
        dict(learner_input=LearnerInput(text="x", source="stt")),
        dict(learner_input=LearnerInput(text="x", source="typed", stt_confidence=0.9)),
        dict(scenario=None),
        dict(mode="lesson"),
        dict(mode="free_chat"),
        dict(mode="lesson", step=STEP),
    ],
)
def test_invalid_contexts(kw):
    with pytest.raises(ValidationError):
        ctx(**kw)


def test_voice_and_lesson_ok():
    c = ctx(
        channel="voice",
        mode="lesson",
        scenario=None,
        step=STEP,
        learner_input=LearnerInput(text="hi", source="stt", end_reason="vad"),
    )
    assert c.step.step_index == 1
