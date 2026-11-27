import json

from english_tutor.models import Address, LearnerProfile, SessionSnapshot, StoredMessage
from english_tutor.providers.base import Message, TextPart, ToolResultPart, ToolUsePart
from english_tutor.teacher.history import (
    advance_window,
    pending_tool_results,
    render_segments,
    to_messages,
)
from english_tutor.teacher.parser import Segment


def snap():
    return SessionSnapshot(
        session_id="s",
        user_id="u",
        channel="text",
        mode="scenario",
        persona_id="lan",
        address=Address(teacher="mình", learner="bạn"),
        learner=LearnerProfile(user_id="u"),
        prompt_version="v",
    )


def user(text, turn="t", results=()):
    parts = [ToolResultPart(tool_use_id=i, content="{}") for i in results] + [TextPart(text=text)]
    return StoredMessage(turn_id=turn, message=Message(role="user", content=parts))


def assistant(text, tool_ids=(), statuses=None, turn="t"):
    parts = [TextPart(text=text)] + [
        ToolUsePart(id=i, name="ask_question", args={}) for i in tool_ids
    ]
    return StoredMessage(
        turn_id=turn, message=Message(role="assistant", content=parts), tool_statuses=statuses or {}
    )


def test_render_segments_rebuilds_tags():
    out = render_segments([Segment(0, "vi", "Chào"), Segment(1, "en", "Hello", slow=True)])
    assert out == '<vi>Chào</vi><en slow="true">Hello</en>'


def test_pending_tool_results_use_statuses_then_terminal_result():
    stored = [assistant("x", ["a", "b"], {"a": {"status": "ok"}})]
    results = pending_tool_results(stored, {"status": "accepted"})
    assert [json.loads(r.content) for r in results] == [{"status": "ok"}, {"status": "accepted"}]
    assert [r.tool_use_id for r in results] == ["a", "b"]
    assert pending_tool_results([user("hi")], None) == []


def test_consecutive_user_messages_are_merged():
    msgs = to_messages([user("one"), user("two")])
    assert len(msgs) == 1 and len(msgs[0].content) == 2


def test_orphan_tool_results_dropped_at_window_start():
    msgs = to_messages([user("hello", results=["gone"]), assistant("ok")])
    assert not any(isinstance(p, ToolResultPart) for p in msgs[0].content)


def big_history(turns):
    stored = []
    for i in range(turns):
        stored.append(user("học " * 150, f"t{i}", results=[f"tu{i - 1}"] if i else ()))
        stored.append(assistant("trả lời " * 150, [f"tu{i}"], turn=f"t{i}"))
    return stored


def test_window_advances_in_one_block_and_stays_valid():
    stored, snapshot = big_history(20), snap()
    assert advance_window(snapshot, stored)
    start = snapshot.window_start
    assert start > 0 and stored[start].message.role == "user"
    again = advance_window(snapshot, stored)  # still within the limit: must not move again
    assert not again and snapshot.window_start == start
    window = to_messages(stored[start:])
    assert window[0].role == "user" and not any(
        isinstance(p, ToolResultPart) for p in window[0].content[:1]
    )
    assert sum(1 for m in stored[start:] if m.message.role == "user") >= 4


def test_short_history_is_untouched():
    snapshot = snap()
    assert not advance_window(snapshot, big_history(2)) and snapshot.window_start == 0
