import pytest

from ea_llm.models import Address
from ea_llm.teacher import guards
from ea_llm.teacher.context import LearnerInput
from ea_llm.teacher.input_checks import check_input
from ea_llm.teacher.parser import Segment
from ea_llm.teacher.tools import validate_args


def seg(text, lang="vi", **kw):
    return Segment(seq=0, lang=lang, text=text, **kw)


# ---- input checks ---------------------------------------------------------------------
def test_long_text_message_is_truncated():
    result = check_input("text", LearnerInput(text="a" * 900, source="typed"))
    assert len(result.text) == 600 and "input_truncated" in result.flags and result.notes


def test_wrong_script_text_vs_voice():
    cyrillic = "Привет как дела сегодня"
    text = check_input("text", LearnerInput(text=cyrillic, source="typed"))
    assert "wrong_script" in text.flags and not text.skip_text
    voice = check_input("voice", LearnerInput(text=cyrillic, source="stt"))
    assert voice.skip_text and "stt_wrong_lang" in voice.flags


def test_voice_cut_off_unless_learner_tapped():
    cut = LearnerInput(text="Yesterday I go to the", source="stt", end_reason="vad")
    assert "maybe_cut_off" in check_input("voice", cut).flags
    tapped = LearnerInput(text="Yesterday I go to the", source="stt", end_reason="tap")
    assert "maybe_cut_off" not in check_input("voice", tapped).flags
    low = LearnerInput(text="I like apples a lot", source="stt", stt_confidence=0.3)
    assert "stt_low_confidence" in check_input("voice", low).flags


def test_vietnamese_text_is_not_flagged_as_foreign_script():
    result = check_input("text", LearnerInput(text="Tôi muốn một ly cà phê", source="typed"))
    assert not result.flags


# ---- blocking guards ------------------------------------------------------------------
def test_markdown_and_emoji_are_stripped_and_flagged():
    result = guards.check_segment(seg("**Great** job 😀"))
    assert result.segment.text == "Great job" and "format_leak" in result.flags


@pytest.mark.parametrize("text", ["Bạn được 85 điểm", "You got 90%", "Your score is 8/100"])
def test_spoken_scores_are_blocked(text):
    result = guards.check_segment(seg(text))
    assert result.segment is None and "score_spoken" in result.flags


def test_ordinary_numbers_pass():
    assert guards.check_segment(seg("Hãy nói 3 câu nhé")).segment is not None


# ---- flag-only guards -----------------------------------------------------------------
ADDRESS = Address(teacher="cô", learner="em")


def turn_flags(segments, **kw):
    base = {
        "address": ADDRESS,
        "cefr": "A2",
        "mistakes_recorded": 0,
        "recent_vi_ratio": None,
    }
    return guards.check_turn(segments, **{**base, **kw})


def test_address_mismatch_ignores_quotes_and_compounds():
    assert "address_mismatch" in turn_flags([seg("Bạn nói rất tốt")])
    assert "address_mismatch" not in turn_flags([seg('Câu "bạn có khỏe không" nghĩa là gì')])
    assert "address_mismatch" not in turn_flags([seg("Em nói với bạn bè nhé")])
    assert "address_mismatch" not in turn_flags([seg("Em nói rất tốt")])


def test_too_many_corrections_and_length_and_ratio():
    assert "too_many_corrections" in turn_flags([seg("Em nói tốt.")], mistakes_recorded=3)
    long = seg("Một. Hai. Ba. Bốn. Năm.")
    assert "too_long" in turn_flags([long])
    assert "vi_ratio_off" in turn_flags([seg("Em nói tốt.")], recent_vi_ratio=95.0)
    assert "vi_ratio_off" not in turn_flags([seg("Em nói tốt.")], recent_vi_ratio=55.0)


def test_cefr_overshoot_limits_english_sentence_length():
    long_en = seg("I would really like to order a large cup of coffee with milk today", "en")
    assert "cefr_overshoot" in turn_flags([long_en])
    assert "cefr_overshoot" not in turn_flags([long_en], cefr="C1")
    protected = seg(long_en.text, "en", protected=True)
    assert "cefr_overshoot" not in turn_flags([protected])


def test_vi_ratio_counts_words():
    assert guards.vi_ratio([seg("một hai ba bốn"), seg("one two", "en")]) == pytest.approx(
        66.67, 0.01
    )
    assert guards.vi_ratio([]) is None


# ---- tool validation ------------------------------------------------------------------
def test_tool_args_validation_and_channel_rules():
    ok, bad = validate_args(
        "record_mistake",
        {"type": "grammar", "original": "go", "corrected": "went", "explanation_vi": "quá khứ"},
        "text",
    )
    assert ok is not None and bad is None
    _, field = validate_args("record_mistake", {"type": "grammar"}, "text")
    assert field == "original"
    spoken = {"type": "pronunciation", "original": "a", "corrected": "b", "explanation_vi": "c"}
    assert validate_args("record_mistake", spoken, "text")[1] == "type"
    assert validate_args("ask_question", {"question": "?"}, "text")[1] == "expected_structure"
