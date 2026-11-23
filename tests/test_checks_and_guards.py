import pytest

from ea_llm.models import Address
from ea_llm.teacher import guards
from ea_llm.teacher.context import LearnerInput
from ea_llm.teacher.input_checks import check_input
from ea_llm.teacher.parser import Segment
from ea_llm.teacher.tools import validate_args
from ea_llm.teacher.wordlist import load_wordlist


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
    assert guards.check_segment(seg("Messi scored 91 goals", "en")).segment is not None


@pytest.mark.parametrize("text", ["That is shit", "Đéo biết", "Nói Mẹ kiếp là sai", "porn"])
def test_blocked_terms_drop_the_segment(text):
    result = guards.check_segment(seg(text))
    assert result.segment is None and result.flags == ["blocked_content"]


@pytest.mark.parametrize("text", ["Cái đĩa này đẹp", "Good skills", "Dickens wrote books"])
def test_blocked_terms_match_whole_words_only(text):
    assert guards.check_segment(seg(text)).segment is not None


def test_blocked_terms_match_decomposed_unicode():
    import unicodedata

    assert guards.contains_blocked_term(unicodedata.normalize("NFD", "đéo"))


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


def test_ungrounded_numbers_need_a_source():
    said = [seg("Messi scored 91 goals and won eight titles.", "en")]
    assert "ungrounded_number" in turn_flags(said)
    assert "ungrounded_number" not in turn_flags(said, grounding=["he scored 91, he has 8"])
    assert "ungrounded_number" not in turn_flags(said, check_numbers=False)
    five = [seg("Five years is a long time. Which one do you like?", "en")]
    assert "ungrounded_number" not in turn_flags(five, grounding=["I work here for 5 years"])
    assert guards.ungrounded_numbers([seg("It costs 1,000 dollars", "en")], ["1000"]) == set()


WORDLIST_YAML = """
source: test fixture
reviewed_by: tester
levels:
  A1: [i, you, a, the, like, coffee, want, to, go, is, it, stop, city]
  A2: [would, cup, of, please]
  B1: [delicious]
"""


@pytest.fixture
def wordlist(tmp_path):
    path = tmp_path / "cefr.yaml"
    path.write_text(WORDLIST_YAML, encoding="utf-8")
    return load_wordlist(path)


def test_wordlist_is_cumulative_and_handles_simple_inflections(wordlist):
    assert wordlist.knows("coffee", "B1") and not wordlist.knows("would", "A1")
    assert wordlist.knows("likes", "A1") and wordlist.knows("liked", "A1")
    assert wordlist.knows("stopped", "A1") and wordlist.knows("cities", "A1")
    assert wordlist.reviewed_by == "tester"


def test_wordlist_unknown_share_skips_proper_nouns_and_exempt_words(wordlist):
    assert wordlist.unknown_share("I like coffee in Hanoi", "A1") == pytest.approx(0.25)
    assert wordlist.unknown_share("I like coffee in Hanoi", "A1", frozenset({"in"})) == 0.0
    # A1 learners are checked against A2 words (level + 1)
    assert wordlist.unknown_share("I would like a cup of coffee", "A1") == 0.0


def test_cefr_overshoot_uses_the_wordlist(wordlist):
    rare = [seg("I want a delicious coffee", "en")]
    assert "cefr_overshoot" in turn_flags(rare, cefr="A1", wordlist=wordlist)
    assert "cefr_overshoot" not in turn_flags(rare, cefr="A2", wordlist=wordlist)
    exempt = turn_flags(rare, cefr="A1", wordlist=wordlist, exempt_words=["delicious food"])
    assert "cefr_overshoot" not in exempt


def test_wordlist_file_errors(tmp_path):
    assert load_wordlist(tmp_path / "missing.yaml") is None
    bad = tmp_path / "bad.yaml"
    bad.write_text("levels: {Z9: [x]}", encoding="utf-8")
    with pytest.raises(ValueError, match="Z9"):
        load_wordlist(bad)


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
