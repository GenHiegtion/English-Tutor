import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from english_tutor.teacher.parser import LangTagParser, Segment


def run(text: str, channel="text", chunks=None):
    p = LangTagParser(channel)
    out: list[Segment] = []
    for part in chunks if chunks is not None else [text]:
        out += p.feed(part)
    out += p.finish()
    return out, p


def view(segs):
    return [(s.lang, s.text) for s in segs]


# ---- splitting the stream at every position ----------------------------------
SAMPLES = [
    "<vi>Chào bạn, hôm nay mình học về đồ ăn nhé. Bạn sẵn sàng chưa?</vi>"
    "<en>What did you eat today?</en>",
    '<vi>Nói theo cô nhé.</vi><en slow="true">I go to the market, and I buy some fresh fruit.</en>',
    "<en>The price is 3.5 dollars. Mr. Smith arrives at 10.30 a.m. today, so please wait.</en>",
    "<vi>Hmm... để cô nghĩ nhé. Được rồi!</vi>",
    "<vi>a < b nhé, và x<y cũng vậy</vi>",
    "<b>bold</b><vi>Xin <i>chào</i> bạn</vi> junk outside tags",
    "<vi>Unclosed tag still flushes",
    "<en>My name is Hùng. I live in Đà Nẵng 😀</en>",
    "<en>Tôi tên là Hùng và tôi sống ở Hà Nội</en>",
]


@pytest.mark.parametrize("text", SAMPLES)
def test_split_at_every_position_equals_single_feed(text):
    whole, _ = run(text)
    for i in range(len(text) + 1):
        parts, _ = run(text, chunks=[text[:i], text[i:]])
        assert parts == whole, f"split at {i}"
    chars, _ = run(text, chunks=list(text))
    assert chars == whole


@settings(max_examples=300, deadline=None)
@given(
    st.lists(
        st.sampled_from(
            [
                "<vi>",
                "</vi>",
                "<en>",
                "</en>",
                '<en slow="true">',
                "<",
                ">",
                ".",
                "?",
                ". ",
                ", ",
                "3.5",
                "Mr. ",
                "Hello there",
                " ",
                "Chào bạn",
                "…",
                "e.g.",
                "<b>",
                "</",
            ]
        ),
        max_size=25,
    ),
    st.lists(st.integers(1, 7), min_size=1, max_size=10),
)
def test_fuzz_chunking_invariance(tokens, sizes):
    text = "".join(tokens)
    whole, pw = run(text)
    chunks, i, k = [], 0, 0
    while i < len(text):
        n = sizes[k % len(sizes)]
        chunks.append(text[i : i + n])
        i += n
        k += 1
    split, ps = run(text, chunks=chunks)
    assert split == whole
    assert (ps.dropped_chars, ps.unclosed_tag) == (pw.dropped_chars, pw.unclosed_tag)


# ---- behaviour ------------------------------------------------------------
def test_basic_two_languages():
    segs, _ = run("<vi>Chào bạn.</vi><en>Hello there.</en>")
    assert view(segs) == [("vi", "Chào bạn."), ("en", "Hello there.")]
    assert all(s.final for s in segs)


def test_sentence_flush_needs_12_chars():
    segs, _ = run("<en>Hi. How are you doing today?</en>")
    assert view(segs) == [("en", "Hi. How are you doing today?")]
    segs, _ = run("<en>I am fine, thank you. And you?</en>")
    assert view(segs) == [("en", "I am fine, thank you."), ("en", "And you?")]
    assert segs[0].final is False and segs[1].final is True


@pytest.mark.parametrize(
    "text",
    [
        "The price is 3.5 dollars today, ok",
        "It starts at 10.30 in the morning",
        "Hello there, Mr. Smith is here now",
        "We meet at 9 a.m. in the school hall",
        "Well... let me think about that",
        "Stop at the end of the line.",
    ],
)
def test_no_false_sentence_breaks(text):
    segs, _ = run(f"<en>{text}</en>")
    assert len(segs) == 1 and segs[0].text == text


def test_dot_at_chunk_end_waits_for_next_chunk():
    segs, _ = run("", chunks=["<en>The price is 3.", "5 dollars today.</en>"])
    assert view(segs) == [("en", "The price is 3.5 dollars today.")]
    segs, _ = run("", chunks=["<en>The price is high today.", " Pay now.</en>"])
    assert view(segs) == [("en", "The price is high today."), ("en", "Pay now.")]


def test_ellipsis_run_is_single_end():
    segs, _ = run("<vi>Để cô nghĩ nhé... Được rồi nhé!</vi>")
    assert view(segs) == [("vi", "Để cô nghĩ nhé..."), ("vi", "Được rồi nhé!")]


def test_long_buffer_cut_at_comma_then_space():
    words = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda"
    text = f"{words}, {words} {words} {words}"
    segs, _ = run(f"<en>{text}</en>")
    assert len(segs) > 1 and segs[0].text.endswith(",")
    assert (
        " ".join(s.text for s in segs).replace(", ", ",").replace(",", ", ").split() == text.split()
    )
    assert all(len(s.text) <= 121 for s in segs)
    nospace = "a" * 300
    segs, _ = run(f"<en>{nospace}</en>")
    assert "".join(s.text for s in segs) == nospace


def test_text_outside_tags_is_dropped_and_counted():
    segs, p = run("junk<vi>Xin chào</vi>more junk")
    assert view(segs) == [("vi", "Xin chào")]
    assert p.dropped_chars == len("junkmorejunk")
    assert p.dropped_samples == ["junk", "more junk"]  # whole phrases, one per run between tags


def test_unknown_tags_dropped_keep_text():
    segs, _ = run("<vi>Xin <b>chào</b> <foo bar=1>bạn</foo></vi>")
    assert view(segs) == [("vi", "Xin chào bạn")]


def test_unclosed_tag_flushes_and_flags():
    segs, p = run("<vi>Unclosed tag")
    assert view(segs) == [("vi", "Unclosed tag")] and p.unclosed_tag and segs[0].final
    segs, p = run("<vi>One</vi")
    assert p.unclosed_tag


def test_new_open_tag_closes_previous_with_flag():
    segs, p = run("<vi>One<en>Two</en>")
    assert view(segs) == [("vi", "One"), ("en", "Two")] and p.unclosed_tag


def test_lone_less_than_does_not_hang():
    segs, _ = run("<vi>a < b và c <= d</vi>")
    assert view(segs) == [("vi", "a < b và c <= d")]
    segs, _ = run("<vi>" + "<" + "x" * 30 + " xong</vi>")
    assert segs and segs[0].text.startswith("<xxxx")
    segs, _ = run("<vi>so sánh 1 <")
    assert view(segs) == [("vi", "so sánh 1 <")]


def test_emoji_and_diacritics_survive():
    segs, _ = run("<en>Great job 😀 well done</en>")
    assert segs[0].text == "Great job 😀 well done"


def test_english_with_vietnamese_name_stays_english():
    segs, p = run("<en>My name is Hùng and I live here.</en>")
    assert segs[0].lang == "en" and segs[0].en_has_vi and p.en_has_vi


def test_vietnamese_sentence_in_en_becomes_vi():
    segs, _ = run("<en>Tôi tên là Hùng và tôi sống ở Hà Nội</en>")
    assert segs[0].lang == "vi" and segs[0].en_has_vi


def test_plain_english_has_no_vi_flag():
    segs, p = run("<en>I went to the market</en>")
    assert not segs[0].en_has_vi and not p.en_has_vi


def test_slow_attribute():
    segs, _ = run('<en slow="true">Say it again</en><en>Normal one</en>')
    assert [s.slow for s in segs] == [True, False]


def test_protected_flag_voice_only():
    segs, _ = run("<en>I went to the market yesterday.</en>", channel="voice")
    assert segs[0].protected
    segs, _ = run("<en>I went to the market yesterday.</en>", channel="text")
    assert not segs[0].protected
    long = " ".join(["word"] * 13)
    segs, _ = run(f"<en>{long}</en>", channel="voice")
    assert not segs[0].protected
    segs, _ = run("<en>I am fine, thank you. And you?</en>", channel="voice")
    assert not any(s.protected for s in segs)
    segs, _ = run("<vi>Nói theo cô nhé.</vi>", channel="voice")
    assert not segs[0].protected


def test_seq_is_monotonic_and_starts_at_zero():
    segs, _ = run("<vi>Một câu dài đủ chữ. Hai câu dài đủ chữ.</vi><en>Three</en>")
    assert [s.seq for s in segs] == [0, 1, 2]
