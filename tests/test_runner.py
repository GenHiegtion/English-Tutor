import json

import pytest

from conftest import LearnerProfile, collect, make_service, turn_ctx
from english_tutor.providers.base import TextPart, ToolResultPart, ToolUsePart
from english_tutor.teacher.events import (
    BoardUpdate,
    MistakeNoted,
    OutputSegment,
    TerminalAction,
    TurnEnd,
    TurnFailed,
)
from english_tutor.testing import FakeProvider, failure, reply

MISTAKE = (
    "record_mistake",
    {"type": "grammar", "original": "go", "corrected": "went", "explanation_vi": "quá khứ"},
)
ASK = ("ask_question", {"question": "What did you buy?", "expected_structure": "past simple"})


async def open_session(service, scenario, persona="lan", **kw):
    return await service.open_session(
        session_id="s1",
        user_id="u1",
        channel="text",
        mode="scenario",
        persona_id=persona,
        scenario=scenario,
        **kw,
    )


def kinds(events):
    return [type(e).__name__ for e in events]


async def test_scenario_opener_does_not_call_llm(stores, scenario):
    provider = FakeProvider([])
    service = make_service(stores, provider)
    await open_session(service, scenario)
    events = await collect(service, turn_ctx("t0", None, scenario=scenario))
    segs = [e for e in events if isinstance(e, OutputSegment)]
    assert [s.lang for s in segs] == ["vi", "en"] and segs[1].text == scenario.opener
    assert isinstance(events[-2], TerminalAction) and events[-2].tool == "open_turn"
    assert not provider.requests
    stored = await stores[0].load_messages("s1")
    assert [m.message.role for m in stored] == ["user", "assistant"]


async def test_normal_turn_streams_segments_tools_and_persists(stores, scenario):
    text = "<vi>Bạn kể rõ lắm.</vi><en>What did you buy there?</en>"
    provider = FakeProvider([reply(text, [MISTAKE, ASK])])
    service = make_service(stores, provider)
    await open_session(service, scenario)
    events = await collect(service, turn_ctx("t1", "I go to market", scenario=scenario))

    assert kinds(events) == [
        "OutputSegment",
        "OutputSegment",
        "MistakeNoted",
        "TerminalAction",
        "TurnEnd",
    ]
    assert [e.seq for e in events if isinstance(e, OutputSegment)] == [0, 1]
    assert isinstance(events[2], MistakeNoted) and events[2].corrected == "went"
    terminal = events[3]
    assert terminal.tool == "ask_question" and terminal.tool_use_id == "tu_1"
    end = events[4]
    assert isinstance(end, TurnEnd) and end.input_tokens == 100 and not end.fallback_used

    req = provider.requests[0]
    assert [b.cache for b in req.system] == ["1h", "1h"]
    assert req.messages[-1].cache == "5m"
    assert req.messages[0].role == "user"
    assert req.messages[-1].content[-1].text == "[Học viên viết]: I go to market"

    stored = await stores[0].load_messages("s1")
    assistant = stored[-1].message
    assert assistant.role == "assistant"
    assert assistant.content[0].text == text
    assert [p.name for p in assistant.content if isinstance(p, ToolUsePart)] == [
        "record_mistake",
        "ask_question",
    ]
    assert stored[-1].persona_id == "lan"
    [usage] = stores[0].turns["t1"].usage
    assert usage.cost_usd == 0.0001 and not usage.cancelled


async def test_next_turn_pairs_every_tool_use_with_a_tool_result(stores, scenario):
    provider = FakeProvider([reply("<vi>Tốt.</vi>", [MISTAKE, ASK]), reply("<en>Nice.</en>")])
    service = make_service(stores, provider)
    await open_session(service, scenario)
    await collect(service, turn_ctx("t1", "I go to market", scenario=scenario))
    await collect(service, turn_ctx("t2", "I bought milk", scenario=scenario))

    last_user = provider.requests[1].messages[-1]
    results = [p for p in last_user.content if isinstance(p, ToolResultPart)]
    assert [r.tool_use_id for r in results] == ["tu_0", "tu_1"]
    assert [json.loads(r.content) for r in results] == [{"status": "ok"}, {"status": "answered"}]
    assert isinstance(last_user.content[len(results)], TextPart)


async def test_invalid_and_extra_tools_get_statuses_and_flags(stores, scenario):
    tools = [
        ("record_mistake", {"type": "grammar"}),  # missing fields
        ("next_step", {"reason": "x"}),  # not allowed in scenario mode
        ASK,
        ("ask_question", {"question": "again", "expected_structure": "x"}),
    ]
    provider = FakeProvider([reply("<en>Hi there my friend.</en>", tools)])
    service = make_service(stores, provider)
    await open_session(service, scenario)
    events = await collect(service, turn_ctx("t1", "hello", scenario=scenario))
    end = events[-1]
    assert {"invalid_tool_args", "tool_not_allowed", "multi_terminal"} <= set(end.guard_flags)
    assert events[-2].tool == "ask_question" and events[-2].tool_use_id == "tu_2"
    stored = await stores[0].load_messages("s1")
    assert stored[-1].tool_statuses == {
        "tu_0": {"error": "original", "status": "invalid"},
        "tu_1": {"error": "tool", "status": "invalid"},
        "tu_3": {"status": "ignored"},
    }


async def test_board_event_and_default_open_turn(stores, scenario):
    provider = FakeProvider(
        [reply("<en>Look here.</en>", [("show_board", {"text": "would like"})])]
    )
    service = make_service(stores, provider)
    await open_session(service, scenario)
    events = await collect(service, turn_ctx("t1", "hi", scenario=scenario))
    assert isinstance(events[1], BoardUpdate) and events[1].text == "would like"
    assert events[2].tool == "open_turn" and events[2].tool_use_id is None


async def test_text_outside_tags_is_dropped_and_blocked_scores_flagged(stores, scenario):
    provider = FakeProvider([reply("junk<vi>Bạn được 90 điểm.</vi><en>Well done today.</en>")])
    service = make_service(stores, provider)
    await open_session(service, scenario)
    events = await collect(service, turn_ctx("t1", "hi", scenario=scenario))
    segs = [e for e in events if isinstance(e, OutputSegment)]
    assert [s.text for s in segs] == ["Well done today."]
    assert {"dropped_chars", "score_spoken"} <= set(events[-1].guard_flags)
    # The dropped text is kept for evals but never serialized for the client.
    assert events[-1].dropped_samples == ["junk"]
    assert "junk" not in events[-1].model_dump_json()


async def test_refusal_and_empty_turn_use_fixed_phrases(stores, scenario):
    provider = FakeProvider([reply("", stop="refusal"), reply("")])
    service = make_service(stores, provider)
    await open_session(service, scenario)
    first = await collect(service, turn_ctx("t1", "hi", scenario=scenario))
    assert first[0].text == "Chúng ta quay lại bài học nhé." and "refusal" in first[-1].guard_flags
    second = await collect(service, turn_ctx("t2", "hello", scenario=scenario))
    assert second[0].text == "Bạn thử viết lại câu vừa rồi nhé."
    assert "empty_turn" in second[-1].guard_flags


async def test_fallback_wins_when_primary_is_slow(stores, scenario):
    primary = FakeProvider([reply("<en>Slow one.</en>")], first_token_delay=1.0)
    fallback = FakeProvider([reply("<en>Fast one.</en>")])
    service = make_service(stores, primary, fallback, hedge_after_s=0.05, give_up_after_s=0.5)
    await open_session(service, scenario)
    events = await collect(service, turn_ctx("t1", "hi", scenario=scenario))
    assert events[0].text == "Fast one." and events[-1].fallback_used


async def test_cancelled_loser_gets_an_estimated_usage_row(stores, scenario):
    haiku = "anthropic/claude-haiku-5.5"
    primary = FakeProvider([reply("<en>Slow one.</en>")], first_token_delay=1.0, model=haiku)
    fallback = FakeProvider([reply("<en>Fast one.</en>")])
    service = make_service(stores, primary, fallback, hedge_after_s=0.05, give_up_after_s=0.5)
    await open_session(service, scenario)
    await collect(service, turn_ctx("t1", "hi", scenario=scenario))

    answer, loser = stores[0].turns["t1"].usage
    assert answer.fallback_used and not answer.cancelled and answer.cost_usd == 0.0001
    assert loser.model == haiku and loser.cancelled and not loser.fallback_used
    assert loser.input_tokens == 100 and loser.output_tokens == 0  # the winner's prompt size
    assert loser.cost_usd == pytest.approx(100 * 0.10 / 1_000_000)


async def test_missing_reported_cost_is_estimated_from_the_price_table(stores, scenario):
    events = reply("<en>Hello there.</en>", cost=None)
    for e in events:
        if type(e).__name__ == "UsageReport":
            e.model = "anthropic/claude-haiku-5.5"
    service = make_service(stores, FakeProvider([events]))
    await open_session(service, scenario)
    await collect(service, turn_ctx("t1", "hi", scenario=scenario))
    [usage] = stores[0].turns["t1"].usage
    assert usage.cost_usd == pytest.approx((100 * 0.10 + 20 * 0.50) / 1_000_000)


async def test_providers_down_saves_nothing(stores, scenario):
    service = make_service(stores, FakeProvider([failure()]), FakeProvider([failure()]))
    await open_session(service, scenario)
    events = await collect(service, turn_ctx("t1", "hi", scenario=scenario))
    assert isinstance(events[-1], TurnFailed) and events[-1].reason == "providers_down"
    assert await stores[0].load_messages("s1") == []


async def test_invalid_contexts_and_duplicate_turn(stores, scenario):
    service = make_service(stores, FakeProvider([reply("<en>Hi there friend.</en>")]))
    unknown = await collect(service, turn_ctx("t0", "hi", session_id="nope", scenario=scenario))
    assert unknown[0].reason == "invalid_context"
    await open_session(service, scenario)
    ok = await collect(service, turn_ctx("t1", "hi", scenario=scenario))
    assert isinstance(ok[-1], TurnEnd)
    dup = await collect(service, turn_ctx("t1", "hi", scenario=scenario))
    assert dup[0].reason == "invalid_context"
    no_input = await collect(service, turn_ctx("t2", None, scenario=scenario))  # history not empty
    assert no_input[0].reason == "invalid_context"
    bad_persona = await collect(
        service, turn_ctx("t3", "hi", persona_id="ghost", scenario=scenario)
    )
    assert bad_persona[0].reason == "invalid_context"


async def test_persona_switch_updates_snapshot_and_greets(stores, scenario):
    provider = FakeProvider([reply("<en>Okay then.</en>"), reply("<en>Sure thing.</en>")])
    service = make_service(stores, provider)
    await open_session(service, scenario)
    await collect(service, turn_ctx("t1", "hi", scenario=scenario))
    events = await collect(service, turn_ctx("t2", "hello", persona_id="hung", scenario=scenario))
    assert events[0].text == "Chào bạn, tôi là thầy Hùng. Chúng ta tiếp tục từ chỗ đang học nhé."
    snap = await stores[0].load_session("s1")
    assert snap.persona_id == "hung" and snap.address.teacher == "tôi"
    req = provider.requests[1]
    assert (
        "Name: Hùng (male)." in req.system[1].text
        and req.system[0].text == provider.requests[0].system[0].text
    )
    notes = [p.text for p in req.messages[-1].content if isinstance(p, TextPart)]
    assert any("switched teachers" in n for n in notes)
    stored = await stores[0].load_messages("s1")
    assert stored[-1].persona_id == "hung" and stored[-3].persona_id == "lan"


async def test_cancelled_turn_keeps_only_delivered_segments_and_emitted_actions(stores, scenario):
    text = "<vi>Câu một đủ dài rồi nhé.</vi><en>This is the second one.</en>"
    provider = FakeProvider([reply(text, [MISTAKE, ASK])])
    service = make_service(stores, provider)
    await open_session(service, scenario)
    stream = service.run_turn(turn_ctx("t1", "hi", scenario=scenario))
    seen = []
    async for event in stream:
        seen.append(event)
        if isinstance(event, MistakeNoted):
            break
    service.mark_delivered("t1", 0)
    await stream.aclose()

    stored = await stores[0].load_messages("s1")
    assistant = stored[-1].message
    assert assistant.role == "assistant"
    assert assistant.content[0].text == "<vi>Câu một đủ dài rồi nhé.</vi>"
    assert [p.name for p in assistant.content if isinstance(p, ToolUsePart)] == ["record_mistake"]
    assert "cancelled" in stored[-1].guard_flags


async def test_cancel_before_anything_delivered_drops_assistant_message(stores, scenario):
    provider = FakeProvider([reply("<vi>Một câu khá dài đây nhé bạn.</vi>")])
    service = make_service(stores, provider)
    await open_session(service, scenario)
    stream = service.run_turn(turn_ctx("t1", "hi", scenario=scenario))
    await stream.__anext__()
    service.mark_delivered("t1", -1)
    await stream.aclose()
    stored = await stores[0].load_messages("s1")
    assert [m.message.role for m in stored] == ["user"]
    [usage] = stores[0].turns["t1"].usage  # cut off before the usage report: estimated
    assert usage.cancelled and usage.model == "fake/model" and usage.input_tokens > 0
    assert usage.cost_usd is None  # the fake model has no price


@pytest.mark.parametrize(("mode", "ok"), [("free_chat", True), ("lesson", False)])
async def test_open_session_mode_rules(stores, scenario, mode, ok):
    service = make_service(stores, FakeProvider([]))
    call = service.open_session(
        session_id="x", user_id="u", channel="text", mode=mode, persona_id="lan"
    )
    if ok:
        assert (await call).prompt_version.startswith("teacher.text.free_chat")
    else:
        with pytest.raises((ValueError, NotImplementedError)):
            await call


async def test_blocked_term_drops_rest_of_turn_and_terminal(stores, scenario):
    text = "<vi>Bạn nói tốt.</vi><en>That is shit.</en><en>Anything else?</en>"
    provider = FakeProvider([reply(text, [MISTAKE, ASK])])
    service = make_service(stores, provider)
    await open_session(service, scenario)
    events = await collect(service, turn_ctx("t1", "hi", scenario=scenario))

    segs = [e for e in events if isinstance(e, OutputSegment)]
    assert [s.text for s in segs] == ["Bạn nói tốt.", "Chúng ta quay lại bài học nhé."]
    assert not any(isinstance(e, MistakeNoted) for e in events)
    terminal = next(e for e in events if isinstance(e, TerminalAction))
    assert terminal.tool == "open_turn"
    assert "blocked_content" in events[-1].guard_flags

    assistant = (await stores[0].load_messages("s1"))[-1]
    assert "shit" not in assistant.message.content[0].text
    assert set(s["status"] for s in assistant.tool_statuses.values()) == {"ignored"}


async def test_numbers_are_grounded_in_free_chat_but_not_checked_in_scenario(stores, scenario):
    provider = FakeProvider(
        [
            reply("<en>Five years is a long time. Messi scored 91 goals in 2012.</en>"),
            reply("<en>That is three dollars.</en>"),
        ]
    )
    service = make_service(stores, provider)
    await service.open_session(
        session_id="fc", user_id="u1", channel="text", mode="free_chat", persona_id="lan"
    )
    events = await collect(
        service, turn_ctx("t1", "I work here for 5 years", session_id="fc", mode="free_chat")
    )
    assert "ungrounded_number" in events[-1].guard_flags

    await open_session(service, scenario)
    events = await collect(service, turn_ctx("t2", "how much?", scenario=scenario))
    assert "ungrounded_number" not in events[-1].guard_flags


async def test_turns_answering_a_vietnamese_question_do_not_count_for_the_vi_ratio(
    stores, scenario
):
    # 16 Vietnamese words, then 4 Vietnamese + 5 English: 44% alone (off the A1 target of 70%),
    # 80% (on target) if the first turn were counted too.
    answer = "<vi>Vì có yesterday là chuyện đã qua nên mình dùng went thay cho go nhé.</vi>"
    mixed = "<vi>Bạn nói đúng rồi.</vi><en>I went to school yesterday.</en>"
    service = make_service(stores, FakeProvider([reply(answer), reply(mixed)]))
    await open_session(service, scenario)

    asked = await collect(service, turn_ctx("t1", "Tại sao phải dùng went vậy?", scenario=scenario))
    assert "vi_ratio_off" not in asked[-1].guard_flags
    later = await collect(service, turn_ctx("t2", "I go to school yesterday", scenario=scenario))
    assert "vi_ratio_off" in later[-1].guard_flags


@pytest.mark.parametrize(
    ("cefr", "lines"),
    [
        (
            "A1",
            [
                ("vi", "Mình và bạn cùng nói chuyện bằng tiếng Anh nhé, chủ đề gì cũng được."),
                ("en", "What do you want to talk about?"),
            ],
        ),
        ("B2", [("en", "What would you like to talk about today?")]),
    ],
)
async def test_free_chat_opener_does_not_call_llm(stores, cefr, lines):
    history, _, learners = stores
    learners.put(LearnerProfile(user_id="u1", cefr=cefr))
    provider = FakeProvider([])
    service = make_service(stores, provider)
    await service.open_session(
        session_id="s1", user_id="u1", channel="text", mode="free_chat", persona_id="lan"
    )
    events = await collect(service, turn_ctx("t0", None, mode="free_chat"))
    segs = [(e.lang, e.text) for e in events if isinstance(e, OutputSegment)]
    assert segs == lines and provider.requests == []
    assert kinds(events)[-2:] == ["TerminalAction", "TurnEnd"]
    stored = await history.load_messages("s1")
    assert [m.message.role for m in stored] == ["user", "assistant"]
    again = await collect(service, turn_ctx("t1", None, mode="free_chat"))
    assert again[0].reason == "invalid_context"


async def test_the_fixed_opener_does_not_count_for_the_vi_ratio(stores, scenario):
    _, _, learners = stores
    learners.put(LearnerProfile(user_id="u1", cefr="C1"))  # target 10% Vietnamese
    service = make_service(stores, FakeProvider([reply("<en>Sure, coming right up.</en>")]))
    await open_session(service, scenario)
    await collect(service, turn_ctx("t0", None, scenario=scenario))  # a Vietnamese intro line
    events = await collect(service, turn_ctx("t1", "A latte, please.", scenario=scenario))
    assert "vi_ratio_off" not in events[-1].guard_flags
