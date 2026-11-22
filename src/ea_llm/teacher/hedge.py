"""Hedged streaming: race a fallback provider when the primary is slow or failing."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Literal

from ea_llm.providers.base import (
    LLMEvent,
    LLMProvider,
    LLMRequest,
    ProviderError,
)

logger = logging.getLogger(__name__)
Source = Literal["primary", "fallback"]
_DONE = object()


async def _pump(name: Source, provider: LLMProvider, req: LLMRequest, queue: asyncio.Queue) -> None:
    stream = provider.stream(req)
    try:
        async for event in stream:
            await queue.put((name, event))
    finally:
        with contextlib.suppress(Exception):
            await stream.aclose()  # type: ignore[attr-defined]
        await queue.put((name, _DONE))


async def hedged_stream(
    primary: LLMProvider,
    fallback: LLMProvider | None,
    req: LLMRequest,
    *,
    hedge_after_s: float,
    give_up_after_s: float,
) -> AsyncIterator[tuple[LLMEvent, Source]]:
    """Yield ``(event, source)`` from whichever provider produces content first.

    - The fallback starts when the primary has no token after ``hedge_after_s`` or fails first.
    - The loser is cancelled as soon as the winner emits its first non-error event.
    - If nobody produces content within ``give_up_after_s``, a timeout ``ProviderError`` is
      yielded. Each turn starts the fallback at most once.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    queue: asyncio.Queue = asyncio.Queue()
    tasks: dict[Source, asyncio.Task] = {
        "primary": asyncio.create_task(_pump("primary", primary, req, queue))
    }
    finished: set[Source] = set()
    last_error: ProviderError | None = None
    winner: Source | None = None

    def start_fallback() -> None:
        if fallback is not None and "fallback" not in tasks:
            logger.info(
                "starting fallback after %.1fs: session=%s turn=%s",
                loop.time() - started,
                req.metadata.session_id,
                req.metadata.turn_id,
            )
            tasks["fallback"] = asyncio.create_task(_pump("fallback", fallback, req, queue))

    try:
        while True:
            now = loop.time() - started
            if winner is None:
                deadline = give_up_after_s
                if "fallback" not in tasks and fallback is not None:
                    deadline = min(deadline, hedge_after_s)
                try:
                    name, event = await asyncio.wait_for(
                        queue.get(), timeout=max(deadline - now, 0)
                    )
                except TimeoutError:
                    if "fallback" not in tasks and fallback is not None and now < give_up_after_s:
                        start_fallback()
                        continue
                    yield (
                        ProviderError(
                            kind="timeout", retryable=True, message="no token before deadline"
                        ),
                        "primary",
                    )
                    return
            else:
                name, event = await queue.get()

            if event is _DONE:
                finished.add(name)
                if winner == name:
                    return
                if finished >= set(tasks):  # every started provider ended without content
                    if last_error is not None:
                        yield last_error, name
                    return
                continue
            if winner is not None and name != winner:
                continue  # stale event from the loser
            if winner is None:
                if isinstance(event, ProviderError):
                    last_error = event
                    if name == "primary":
                        start_fallback()
                    continue
                winner = name  # first non-error event: content, usage or stop
                for other, task in tasks.items():
                    if other != name:
                        task.cancel()
            yield event, name
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
