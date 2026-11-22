"""Wiring helpers that build a TeacherService from environment settings."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator

from dotenv import load_dotenv

from ea_llm.adapters.memory import MemoryHistoryStore, MemoryLearnerStore, MemoryPersonaStore
from ea_llm.config import ConfigError, load_routes
from ea_llm.providers.base import LLMEvent, LLMProvider, LLMRequest
from ea_llm.providers.openrouter_provider import API_KEY_ENV, OpenRouterProvider
from ea_llm.teacher.runner import TeacherService
from ea_llm.testing import FakeProvider, reply

LOG_LEVEL_ENV = "EA_LLM_LOG_LEVEL"


def configure_logging() -> None:
    """Log to stderr; ``EA_LLM_LOG_LEVEL`` (default WARNING) sets the level of ``ea_llm`` loggers.

    Third-party libraries stay at WARNING. For the CLI and dev server only: a host application
    (the web backend, the voice bot) configures logging itself.
    """
    load_dotenv()
    level = os.environ.get(LOG_LEVEL_ENV, "WARNING").upper()
    if level not in logging.getLevelNamesMapping():
        level = "WARNING"
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("ea_llm").setLevel(level)


def build_service(
    *,
    offline: bool = False,
    learners: MemoryLearnerStore | None = None,
) -> TeacherService:
    """Service backed by in-memory stores. ``offline`` swaps OpenRouter for a canned provider."""
    load_dotenv()
    primary: LLMProvider
    fallback: LLMProvider | None
    if offline:
        primary = _OfflineProvider()
        fallback = None
    else:
        if not os.environ.get(API_KEY_ENV):
            raise ConfigError(f"{API_KEY_ENV} is not set (put it in .env or use --offline)")
        routes = load_routes()  # after load_dotenv, so .env overrides apply
        openrouter = OpenRouterProvider(routes=routes)
        primary = openrouter
        fallback_model = routes["teacher"].fallback_model
        fallback = openrouter.with_model(fallback_model) if fallback_model else None
    return TeacherService(
        primary=primary,
        fallback=fallback,
        history=MemoryHistoryStore(),
        personas=MemoryPersonaStore.from_seed(),
        learners=learners or MemoryLearnerStore(),
    )


class _OfflineProvider(FakeProvider):
    """Always answers with a fixed short reply, for trying the plumbing without an API key."""

    def __init__(self) -> None:
        super().__init__([])

    async def stream(self, req: LLMRequest) -> AsyncIterator[LLMEvent]:
        self._scripts = [
            reply(
                "<vi>Đây là câu trả lời mẫu ở chế độ offline.</vi><en>Please try again.</en>",
                [("ask_question", {"question": "Ready?", "expected_structure": "yes/no"})],
            )
        ]
        async for event in super().stream(req):
            yield event
