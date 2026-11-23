"""OpenRouter adapter built on the Anthropic SDK."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import anthropic

from ea_llm.config import ConfigError, ModelRoute, load_routes
from ea_llm.providers.base import (
    LLMEvent,
    LLMRequest,
    Message,
    ProviderError,
    Stop,
    TextDelta,
    TextPart,
    ToolCall,
    ToolResultPart,
    ToolUsePart,
    UsageReport,
)

BASE_URL = "https://openrouter.ai/api"
API_KEY_ENV = "OPENROUTER_API_KEY"
logger = logging.getLogger(__name__)
_STOP_REASONS = {
    "end_turn": "end_turn",
    "stop_sequence": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}


def _cache_control(ttl: str) -> dict | None:
    if ttl == "none":
        return None
    return {"type": "ephemeral", "ttl": ttl} if ttl == "1h" else {"type": "ephemeral"}


def build_system(req: LLMRequest) -> list[dict]:
    blocks = []
    for b in req.system:
        d: dict[str, Any] = {"type": "text", "text": b.text}
        if cc := _cache_control(b.cache):
            d["cache_control"] = cc
        blocks.append(d)
    return blocks


def _part(p: TextPart | ToolUsePart | ToolResultPart) -> dict:
    if isinstance(p, TextPart):
        return {"type": "text", "text": p.text}
    if isinstance(p, ToolUsePart):
        return {"type": "tool_use", "id": p.id, "name": p.name, "input": p.args}
    return {"type": "tool_result", "tool_use_id": p.tool_use_id, "content": p.content}


def build_messages(messages: list[Message]) -> list[dict]:
    out = []
    for m in messages:
        parts = [_part(p) for p in m.content]
        if (cc := _cache_control(m.cache)) and parts:
            parts[-1]["cache_control"] = cc
        out.append({"role": m.role, "content": parts})
    return out


def map_error(exc: Exception) -> ProviderError:
    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderError(kind="timeout", retryable=True, message=str(exc))
    if isinstance(exc, anthropic.APIStatusError):
        s = exc.status_code
        if s == 429:
            return ProviderError(kind="rate_limit", status=s, retryable=True, message=str(exc))
        if s == 402:
            return ProviderError(kind="credit", status=s, retryable=False, message=str(exc))
        if s in (401, 403):
            return ProviderError(kind="auth", status=s, retryable=False, message=str(exc))
        if s >= 500 or s in (408, 409):
            return ProviderError(kind="server", status=s, retryable=True, message=str(exc))
        return ProviderError(kind="bad_request", status=s, retryable=False, message=str(exc))
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderError(kind="server", retryable=True, message=str(exc))
    raise exc


def _usage_report(model: str, usage: Any, provider: str, cost: float | None) -> UsageReport:
    return UsageReport(
        provider=provider,
        model=model,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        cost_usd=cost,
    )


class OpenRouterProvider:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        routes: dict[str, ModelRoute] | None = None,
        model: str | None = None,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self.routes = routes or load_routes()
        self.model_override = model  # used by the fallback provider
        if client is None:
            key = api_key or os.environ.get(API_KEY_ENV)
            if not key:
                raise ConfigError(f"{API_KEY_ENV} is not set")
            # max_retries=0: the module's own router decides when to fall back.
            client = anthropic.AsyncAnthropic(
                base_url=BASE_URL, auth_token=key, api_key=None, max_retries=0
            )
        self.client = client

    def with_model(self, model: str) -> OpenRouterProvider:
        return OpenRouterProvider(routes=self.routes, model=model, client=self.client)

    def model_for(self, role: str) -> str:
        return self.model_override or self.routes[role].model

    # ---------------------------------------------------------------- request
    def build_params(self, req: LLMRequest) -> dict[str, Any]:
        route = self.routes[req.role]
        model = self.model_for(req.role)
        provider_opts: dict[str, Any] = {"allow_fallbacks": False, "data_collection": "deny"}
        if model.startswith("anthropic/"):
            provider_opts["order"] = ["anthropic"]
        output_config: dict[str, Any] = {"effort": route.effort}
        if req.json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": req.json_schema}
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": req.max_tokens or route.max_tokens,
            "system": build_system(req),
            "messages": build_messages(req.messages),
            "thinking": {"type": route.thinking},
            "output_config": output_config,
            "timeout": route.timeout_s,
            "extra_body": {"provider": provider_opts, "session_id": req.metadata.session_id},
        }
        if req.tools:
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in req.tools
            ]
        return params

    # ---------------------------------------------------------------- stream
    async def stream(self, req: LLMRequest) -> AsyncIterator[LLMEvent]:
        params = self.build_params(req)
        model = params["model"]
        tool_blocks: dict[int, dict[str, Any]] = {}
        usage: Any = None
        provider = "anthropic"
        cost: float | None = None
        stop_reason: str | None = None
        try:
            raw = await self.client.messages.create(stream=True, **params)
        except Exception as exc:
            yield self._error(model, req, exc)
            return
        try:
            async for ev in raw:
                t = ev.type
                if t == "message_start":
                    usage = ev.message.usage
                    extra = getattr(ev.message, "model_extra", None) or {}
                    provider = extra.get("provider") or provider
                    cost = _cost(usage)
                elif t == "content_block_start" and ev.content_block.type == "tool_use":
                    tool_blocks[ev.index] = {
                        "id": ev.content_block.id,
                        "name": ev.content_block.name,
                        "json": "",
                    }
                elif t == "content_block_delta":
                    d = ev.delta
                    if d.type == "text_delta":
                        if d.text:
                            yield TextDelta(text=d.text)
                    elif d.type == "input_json_delta" and ev.index in tool_blocks:
                        tool_blocks[ev.index]["json"] += d.partial_json
                elif t == "content_block_stop" and ev.index in tool_blocks:
                    tb = tool_blocks.pop(ev.index)
                    yield ToolCall(id=tb["id"], name=tb["name"], args=_loads(tb["json"]))
                elif t == "message_delta":
                    stop_reason = ev.delta.stop_reason or stop_reason
                    if ev.usage is not None:
                        usage = _merge_usage(usage, ev.usage)
                        cost = _cost(ev.usage) or cost
        except Exception as exc:
            yield self._error(model, req, exc)
            return
        finally:
            await raw.close()  # close the connection at once, even if the caller cancelled the turn
        if usage is not None:
            yield _usage_report(model, usage, provider, cost)
        yield Stop(reason=_STOP_REASONS.get(stop_reason or "end_turn", "end_turn"))  # type: ignore[arg-type]

    @staticmethod
    def _error(model: str, req: LLMRequest, exc: Exception) -> ProviderError:
        error = map_error(exc)
        logger.warning(
            "%s call failed: model=%s session=%s turn=%s kind=%s status=%s: %s",
            req.role,
            model,
            req.metadata.session_id,
            req.metadata.turn_id,
            error.kind,
            error.status,
            error.message,
        )
        return error

    # ------------------------------------------------------------ structured JSON
    async def complete_json(self, req: LLMRequest) -> tuple[dict, UsageReport]:
        if req.json_schema is None:
            raise ValueError("complete_json requires json_schema")
        params = self.build_params(req)
        msg = await self.client.messages.create(**params)
        text = "".join(b.text for b in msg.content if b.type == "text")
        extra = getattr(msg, "model_extra", None) or {}
        report = _usage_report(
            params["model"], msg.usage, extra.get("provider") or "anthropic", _cost(msg.usage)
        )
        return json.loads(text), report


def _loads(raw: str) -> dict:
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _cost(usage: Any) -> float | None:
    extra = getattr(usage, "model_extra", None) or {}
    value = extra.get("cost")
    return float(value) if isinstance(value, (int, float)) else None


def _merge_usage(start: Any, delta: Any) -> SimpleNamespace:
    """message_delta usually reports only output_tokens; keep the rest from message_start."""

    def pick(name: str) -> int:
        return getattr(delta, name, None) or getattr(start, name, 0) or 0

    return SimpleNamespace(
        input_tokens=pick("input_tokens"),
        output_tokens=pick("output_tokens"),
        cache_read_input_tokens=pick("cache_read_input_tokens"),
        cache_creation_input_tokens=pick("cache_creation_input_tokens"),
    )
