"""Data contracts between the module and LLM providers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, Field

CacheTtl = Literal["none", "5m", "1h"]


class SystemBlock(BaseModel):
    text: str
    cache: CacheTtl = "none"  # cache_control is attached at the end of this block


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ToolUsePart(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    args: dict


class ToolResultPart(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str  # always serialized JSON with sort_keys=True


ContentPart = Annotated[TextPart | ToolUsePart | ToolResultPart, Field(discriminator="type")]


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: list[ContentPart]
    cache: CacheTtl = "none"  # cache breakpoint on the last part of this message


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict
    terminal: bool = False  # True = ends the turn


class CallMeta(BaseModel):
    session_id: str
    user_id: str
    turn_id: str | None = None
    attempt_id: str | None = None
    channel: Literal["voice", "text"] | None = None  # None for the writer role
    mode: Literal["lesson", "free_chat", "scenario"] | None = None
    prompt_version: str


class LLMRequest(BaseModel):
    role: Literal["teacher", "examiner", "reporter", "writer"]
    system: list[SystemBlock]
    messages: list[Message]
    tools: list[ToolSpec] = []
    json_schema: dict | None = None  # examiner, reporter, writer
    max_tokens: int
    metadata: CallMeta


class TextDelta(BaseModel):
    text: str


class UsageReport(BaseModel):
    provider: str  # upstream provider OpenRouter actually used
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float | None = None  # cost reported by OpenRouter, if any


class ProviderError(BaseModel):
    kind: Literal["rate_limit", "server", "timeout", "auth", "bad_request", "credit"]
    status: int | None = None
    retryable: bool
    message: str


class ToolCall(BaseModel):
    id: str
    name: str
    args: dict  # re-validated with Pydantic in teacher/tools.py


class Stop(BaseModel):
    reason: Literal["end_turn", "tool_use", "max_tokens", "refusal", "cancelled"]


LLMEvent = TextDelta | ToolCall | UsageReport | Stop | ProviderError


class LLMProvider(Protocol):
    def model_for(self, role: str) -> str:
        """Model id this provider calls for ``role`` (used to price cancelled calls)."""
        ...

    def stream(self, req: LLMRequest) -> AsyncIterator[LLMEvent]: ...

    async def complete_json(self, req: LLMRequest) -> tuple[dict, UsageReport]: ...
