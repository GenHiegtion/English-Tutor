from types import SimpleNamespace as NS

import anthropic
import httpx
import pytest

from english_tutor.config import ConfigError
from english_tutor.providers.base import (
    CallMeta,
    LLMRequest,
    Message,
    ProviderError,
    Stop,
    SystemBlock,
    TextDelta,
    TextPart,
    ToolCall,
    ToolResultPart,
    ToolSpec,
    ToolUsePart,
    UsageReport,
)
from english_tutor.providers.openrouter_provider import OpenRouterProvider, map_error

META = CallMeta(session_id="s1", user_id="u1", prompt_version="t@1#x")


def make_req(**kw):
    base = dict(
        role="teacher",
        system=[
            SystemBlock(text="rules", cache="1h"),
            SystemBlock(text="persona", cache="1h"),
            SystemBlock(text="x"),
        ],
        messages=[
            Message(role="user", content=[TextPart(text="hi")]),
            Message(
                role="assistant",
                content=[ToolUsePart(id="t1", name="ask_question", args={"q": "?"})],
            ),
            Message(
                role="user",
                content=[ToolResultPart(tool_use_id="t1", content="{}"), TextPart(text="go")],
                cache="5m",
            ),
        ],
        tools=[
            ToolSpec(
                name="ask_question", description="d", input_schema={"type": "object"}, terminal=True
            )
        ],
        max_tokens=300,
        metadata=META,
    )
    base.update(kw)
    return LLMRequest(**base)


class FakeStream:
    def __init__(self, events, boom=None):
        self.events, self.boom, self.closed = events, boom, False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for e in self.events:
            yield e
        if self.boom:
            raise self.boom

    async def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, stream=None, error=None, message=None):
        self.stream, self.error, self.message, self.calls = stream, error, message, []
        self.messages = self

    async def create(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return self.stream if kw.get("stream") else self.message


def ev(type, **kw):
    return NS(type=type, **kw)


def usage(**kw):
    d = dict(
        input_tokens=10,
        output_tokens=0,
        cache_read_input_tokens=7,
        cache_creation_input_tokens=3,
        model_extra={"cost": 0.0012},
    )
    d.update(kw)
    return NS(**d)


TEXT_EVENTS = [
    ev("message_start", message=NS(usage=usage(), model_extra={"provider": "Anthropic"})),
    ev("content_block_start", index=0, content_block=NS(type="text")),
    ev("content_block_delta", index=0, delta=NS(type="text_delta", text="<vi>Chào")),
    ev("content_block_delta", index=0, delta=NS(type="text_delta", text=" bạn</vi>")),
    ev("content_block_stop", index=0),
    ev(
        "content_block_start",
        index=1,
        content_block=NS(type="tool_use", id="tu1", name="ask_question"),
    ),
    ev("content_block_delta", index=1, delta=NS(type="input_json_delta", partial_json='{"quest')),
    ev(
        "content_block_delta",
        index=1,
        delta=NS(type="input_json_delta", partial_json='ion": "Hi?"}'),
    ),
    ev("content_block_stop", index=1),
    ev(
        "message_delta",
        delta=NS(stop_reason="tool_use"),
        usage=NS(output_tokens=42, model_extra={}),
    ),
]


async def collect(provider, req):
    return [e async for e in provider.stream(req)]


async def test_stream_events():
    fs = FakeStream(TEXT_EVENTS)
    out = await collect(OpenRouterProvider(client=FakeClient(stream=fs)), make_req())
    assert [type(e) for e in out] == [TextDelta, TextDelta, ToolCall, UsageReport, Stop]
    assert out[2].args == {"question": "Hi?"} and out[2].id == "tu1"
    u = out[3]
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens) == (
        10,
        42,
        7,
        3,
    )
    assert u.provider == "Anthropic" and u.cost_usd == 0.0012
    assert out[4].reason == "tool_use"
    assert fs.closed


def test_request_params_cache_and_routing():
    p = OpenRouterProvider(client=FakeClient()).build_params(make_req())
    assert p["model"] == "anthropic/claude-haiku-5.5"
    assert p["thinking"] == {"type": "disabled"} and p["output_config"] == {"effort": "low"}
    assert p["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in p["system"][2]
    last = p["messages"][2]["content"]
    assert last[-1]["cache_control"] == {"type": "ephemeral"} and "cache_control" not in last[0]
    assert p["messages"][1]["content"][0] == {
        "type": "tool_use",
        "id": "t1",
        "name": "ask_question",
        "input": {"q": "?"},
    }
    assert p["extra_body"] == {
        "provider": {"allow_fallbacks": False, "data_collection": "deny", "order": ["anthropic"]},
        "session_id": "s1",
    }
    assert not {"temperature", "top_p", "top_k"} & p.keys()
    assert p["tools"] == [
        {"name": "ask_question", "description": "d", "input_schema": {"type": "object"}}
    ]


def test_fallback_model_has_no_anthropic_order():
    p = (
        OpenRouterProvider(client=FakeClient())
        .with_model("openai/gpt-6-luna")
        .build_params(make_req())
    )
    assert p["model"] == "openai/gpt-6-luna" and "order" not in p["extra_body"]["provider"]


def test_client_has_no_sdk_retries():
    c = OpenRouterProvider(api_key="k").client
    assert c.max_retries == 0 and str(c.base_url).startswith("https://openrouter.ai/api")


def _status_error(code):
    r = httpx.Response(code, request=httpx.Request("POST", "https://x"))
    return anthropic.APIStatusError("boom", response=r, body=None)


@pytest.mark.parametrize(
    "code,kind,retryable",
    [
        (429, "rate_limit", True),
        (402, "credit", False),
        (401, "auth", False),
        (400, "bad_request", False),
        (503, "server", True),
    ],
)
def test_error_mapping(code, kind, retryable):
    e = map_error(_status_error(code))
    assert (e.kind, e.retryable, e.status) == (kind, retryable, code)


async def test_stream_error_before_and_during():
    out = await collect(OpenRouterProvider(client=FakeClient(error=_status_error(429))), make_req())
    assert len(out) == 1 and isinstance(out[0], ProviderError) and out[0].kind == "rate_limit"
    fs = FakeStream(
        TEXT_EVENTS[:3], boom=anthropic.APITimeoutError(httpx.Request("POST", "https://x"))
    )
    out = await collect(OpenRouterProvider(client=FakeClient(stream=fs)), make_req())
    assert isinstance(out[-1], ProviderError) and out[-1].kind == "timeout" and fs.closed


async def test_cancel_closes_connection():
    fs = FakeStream(TEXT_EVENTS)
    gen = OpenRouterProvider(client=FakeClient(stream=fs)).stream(make_req())
    first = await gen.__anext__()
    assert isinstance(first, TextDelta)
    await gen.aclose()
    assert fs.closed


async def test_complete_json():
    msg = NS(
        content=[NS(type="text", text='{"a": 1}')], usage=usage(output_tokens=5), model_extra={}
    )
    client = FakeClient(message=msg)
    data, report = await OpenRouterProvider(client=client).complete_json(
        make_req(role="examiner", json_schema={"type": "object"}, tools=[])
    )
    assert data == {"a": 1} and report.output_tokens == 5
    sent = client.calls[0]
    assert sent["output_config"]["format"]["type"] == "json_schema" and sent["model"].endswith(
        "haiku-5.5"
    )


def test_missing_api_key_fails_with_a_clear_message(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY is not set"):
        OpenRouterProvider()
