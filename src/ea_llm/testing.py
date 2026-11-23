"""Test doubles that need no network: a scripted LLM provider and event helpers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable

from ea_llm.providers.base import (
    LLMEvent,
    LLMRequest,
    ProviderError,
    Stop,
    TextDelta,
    ToolCall,
    UsageReport,
)


def reply(
    text: str = "",
    tools: Iterable[tuple[str, dict]] = (),
    *,
    stop: str | None = None,
    chunk: int = 7,
    cost: float | None = 0.0001,
) -> list[LLMEvent]:
    """Build the events of one model reply: text in small chunks, tools, usage and stop."""
    events: list[LLMEvent] = [
        TextDelta(text=text[i : i + chunk]) for i in range(0, len(text), chunk)
    ]
    calls = [ToolCall(id=f"tu_{i}", name=n, args=a) for i, (n, a) in enumerate(tools)]
    events += calls
    events.append(
        UsageReport(
            provider="fake",
            model="fake/model",
            input_tokens=100,
            output_tokens=20,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=cost,
        )
    )
    events.append(Stop(reason=stop or ("tool_use" if calls else "end_turn")))  # type: ignore[arg-type]
    return events


def failure(kind: str = "server") -> list[LLMEvent]:
    return [ProviderError(kind=kind, retryable=True, message="scripted failure")]  # type: ignore[arg-type]


class FakeProvider:
    """Plays scripted replies in order and records every request it receives."""

    def __init__(
        self,
        scripts: list[list[LLMEvent]],
        *,
        first_token_delay: float = 0.0,
        model: str = "fake/model",
    ) -> None:
        self._scripts = list(scripts)
        self.model = model
        self.first_token_delay = first_token_delay
        self.requests: list[LLMRequest] = []
        self.cancelled = 0

    def model_for(self, role: str) -> str:
        return self.model

    async def stream(self, req: LLMRequest) -> AsyncIterator[LLMEvent]:
        self.requests.append(req)
        script = self._scripts.pop(0) if self._scripts else reply("<vi>Ok.</vi>")
        try:
            if self.first_token_delay:
                await asyncio.sleep(self.first_token_delay)
            for event in script:
                yield event
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise

    async def complete_json(self, req: LLMRequest):  # pragma: no cover - not used by the teacher
        raise NotImplementedError
