import asyncio

import pytest

from conftest import make_service, turn_ctx
from ea_llm.api.turns import TurnBusy, TurnStreams
from ea_llm.providers.base import LLMRequest, TextDelta
from ea_llm.teacher.events import OutputSegment
from ea_llm.testing import FakeProvider


class StallingProvider:
    """Sends one finished segment, then waits until it is cancelled."""

    def __init__(self) -> None:
        self.stalled = asyncio.Event()
        self.cancelled = False

    def model_for(self, role: str) -> str:
        return "fake/model"

    async def stream(self, req: LLMRequest):
        yield TextDelta(text="<vi>Câu một.</vi><vi>Câu hai")
        self.stalled.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        yield TextDelta(text="")  # pragma: no cover

    async def complete_json(self, req: LLMRequest):  # pragma: no cover
        raise NotImplementedError


class CrashingHistory:
    """A history store whose writes always fail."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def append_turn(self, session_id, turn):
        raise RuntimeError("database is down")


async def open_free_chat(service, session_id="s1"):
    await service.open_session(
        session_id=session_id, user_id="u1", channel="text", mode="free_chat", persona_id="lan"
    )


async def test_abandoned_stream_cancels_the_turn_and_keeps_delivered_segments(stores):
    provider = StallingProvider()
    service = make_service(stores, provider, give_up_after_s=30.0)
    await open_free_chat(service)
    streams = TurnStreams(service, lock_wait_s=0.1)

    events = await streams.start(turn_ctx("t1", "hello", mode="free_chat"))
    first = await anext(events)
    assert isinstance(first, OutputSegment) and first.text == "Câu một."
    await provider.stalled.wait()
    assert streams.is_running("s1", "t1")
    service.mark_delivered("t1", first.seq)

    await events.aclose()  # the client disconnected
    tasks = list(streams._tasks)
    await asyncio.wait(tasks, timeout=2)
    assert all(task.cancelled() for task in tasks)

    assert provider.cancelled
    assert not streams.is_running("s1", "t1")
    stored = await service.history.load_messages("s1")
    assert [m.message.role for m in stored] == ["user", "assistant"]
    assert stored[-1].message.content[0].text == "<vi>Câu một.</vi>"
    assert "cancelled" in stored[-1].guard_flags
    assert streams._locks == {}


async def test_second_turn_of_a_session_waits_then_reports_busy(stores):
    provider = StallingProvider()
    service = make_service(stores, provider, give_up_after_s=30.0)
    await open_free_chat(service)
    streams = TurnStreams(service, lock_wait_s=0.05)

    events = await streams.start(turn_ctx("t1", "hello", mode="free_chat"))
    await anext(events)
    with pytest.raises(TurnBusy):
        await streams.start(turn_ctx("t2", "again", mode="free_chat"))

    await events.aclose()
    await streams.shutdown()
    assert streams._locks == {}


async def test_other_sessions_are_not_blocked(stores):
    provider = StallingProvider()
    service = make_service(stores, provider, give_up_after_s=30.0)
    await open_free_chat(service, "s1")
    await open_free_chat(service, "s2")
    streams = TurnStreams(service, lock_wait_s=0.05)

    first = await streams.start(turn_ctx("t1", "hello", mode="free_chat"))
    await anext(first)
    second = await streams.start(turn_ctx("t2", "hello", session_id="s2", mode="free_chat"))
    await anext(second)

    await first.aclose()
    await second.aclose()
    await streams.shutdown()


async def test_unexpected_error_ends_the_stream_with_internal_error(stores):
    history, personas, learners = stores
    service = make_service((CrashingHistory(history), personas, learners), FakeProvider([]))
    await open_free_chat(service)
    streams = TurnStreams(service, lock_wait_s=0.1)

    events = [e async for e in await streams.start(turn_ctx("t1", "hello", mode="free_chat"))]

    assert events[-1].kind == "turn_failed"
    assert events[-1].reason == "internal_error"
    assert streams._locks == {}
