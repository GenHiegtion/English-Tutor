import asyncio
import time

from ea_llm.providers.base import CallMeta, LLMRequest, ProviderError, Stop, TextDelta
from ea_llm.teacher.hedge import LoserCancelled, hedged_stream
from ea_llm.testing import FakeProvider, failure, reply

REQ = LLMRequest(
    role="teacher",
    system=[],
    messages=[],
    max_tokens=10,
    metadata=CallMeta(session_id="s", user_id="u", prompt_version="v"),
)


async def run(primary, fallback, hedge=0.05, give_up=0.3):
    return [
        e
        async for e in hedged_stream(
            primary, fallback, REQ, hedge_after_s=hedge, give_up_after_s=give_up
        )
    ]


def sources(events):
    return {src for _, src in events}


async def test_fast_primary_never_starts_fallback():
    fb = FakeProvider([reply("fallback")])
    events = await run(FakeProvider([reply("hello")]), fb)
    assert sources(events) == {"primary"} and not fb.requests
    assert "".join(e.text for e, _ in events if isinstance(e, TextDelta)) == "hello"


async def test_slow_primary_loses_to_fallback_and_is_cancelled():
    slow = FakeProvider([reply("slow")], first_token_delay=1.0)
    events = await run(slow, FakeProvider([reply("fast")]))
    assert events[0] == (LoserCancelled(source="primary"), "primary")
    assert sources(events[1:]) == {"fallback"}
    await asyncio.sleep(0.01)
    assert slow.cancelled == 1


async def test_primary_error_starts_fallback_immediately():
    started = time.monotonic()
    events = await run(FakeProvider([failure("rate_limit")]), FakeProvider([reply("ok")]), hedge=5)
    assert time.monotonic() - started < 1
    assert sources(events) == {"fallback"}
    assert not any(isinstance(e, LoserCancelled) for e, _ in events)  # the primary had failed


async def test_both_fail_yields_error():
    events = await run(FakeProvider([failure()]), FakeProvider([failure("timeout")]))
    assert len(events) == 1 and isinstance(events[0][0], ProviderError)


async def test_nobody_answers_before_give_up():
    events = await run(
        FakeProvider([reply("a")], first_token_delay=2),
        FakeProvider([reply("b")], first_token_delay=2),
        hedge=0.05,
        give_up=0.2,
    )
    assert len(events) == 1 and events[0][0].kind == "timeout"


async def test_no_fallback_configured():
    events = await run(FakeProvider([failure()]), None)
    assert isinstance(events[0][0], ProviderError)
    ok = await run(FakeProvider([reply("fine")]), None)
    assert isinstance(ok[-1][0], Stop)
