"""Measure teacher latency and cost from this machine, through the real teacher pipeline.

Usage: uv run python -m ea_llm.evals.bench_latency [n] [model ...]

Each model gets its own ``free_chat`` session with the real prompt, tools and history, and no
fallback, so the numbers belong to that model alone. Reported per model:

- TTFT: request sent to the first token.
- first segment: request sent to the first ``OutputSegment`` (what the learner waits for).
- total: request sent to ``TurnEnd``.
- cache_read: tokens read from the prompt cache per turn.
- cost: sum of the usage rows of the session (OpenRouter's reported cost when available).
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
import uuid

from dotenv import load_dotenv

from ea_llm.adapters.memory import MemoryHistoryStore, MemoryLearnerStore, MemoryPersonaStore
from ea_llm.models import LearnerProfile
from ea_llm.providers.openrouter_provider import OpenRouterProvider
from ea_llm.teacher.context import LearnerInput, TurnContext
from ea_llm.teacher.events import OutputSegment, TurnEnd, TurnFailed
from ea_llm.teacher.runner import TeacherService

MODELS = ["anthropic/claude-haiku-5.5", "openai/gpt-6-luna"]
LINES = [
    "Hi, I want practice English today",
    "I like play football with my friends",
    "Yesterday I go to the stadium",
    "My favourite player is Messi because he very fast",
    "ko biet noi gi nua",
    "I watch the match on TV last night",
    "My team win two to one",
    "I am agree that football is the best sport",
    "Next week I will going to a concert",
    "I like music too, especially rock",
]


def _pct(values: list[float], q: int) -> float:
    return statistics.quantiles(values, n=10)[q - 1] if len(values) >= 2 else values[0]


async def bench(model: str, n: int) -> None:
    learners = MemoryLearnerStore([LearnerProfile(user_id="bench", cefr="A2", age_band="adult")])
    history = MemoryHistoryStore()
    service = TeacherService(
        primary=OpenRouterProvider().with_model(model),
        history=history,
        personas=MemoryPersonaStore.from_seed(),
        learners=learners,
    )
    sid = f"bench-{uuid.uuid4().hex[:8]}"
    await service.open_session(
        session_id=sid, user_id="bench", channel="text", mode="free_chat", persona_id="lan"
    )
    ttfts, firsts, totals, reads, errors = [], [], [], [], 0
    for i in range(n):
        turn_id = uuid.uuid4().hex[:10]
        ctx = TurnContext(
            session_id=sid,
            user_id="bench",
            turn_id=turn_id,
            channel="text",
            mode="free_chat",
            persona_id="lan",
            learner_input=LearnerInput(text=LINES[i % len(LINES)], source="typed"),
            received_at=time.time(),
        )
        started = time.perf_counter()
        first: float | None = None
        async for event in service.run_turn(ctx):
            if isinstance(event, OutputSegment):
                service.mark_delivered(turn_id, event.seq)
                first = first or (time.perf_counter() - started) * 1000
            elif isinstance(event, TurnEnd) and event.ttft_ms is not None and first is not None:
                ttfts.append(event.ttft_ms)
                firsts.append(first)
                totals.append((time.perf_counter() - started) * 1000)
                reads.append(event.cache_read_tokens)
            elif isinstance(event, TurnFailed):
                errors += 1
    cost = sum(
        row.cost_usd or 0.0
        for record in history.turns.values()
        for row in record.usage
        if row.session_id == sid
    )
    if not ttfts:
        print(f"{model}: no results ({errors} failed turns)")
        return
    print(
        f"{model}: n={len(ttfts)} failed={errors}\n"
        f"  TTFT          median={statistics.median(ttfts):.0f}ms p90={_pct(ttfts, 9):.0f}ms "
        f"max={max(ttfts):.0f}ms\n"
        f"  first segment median={statistics.median(firsts):.0f}ms p90={_pct(firsts, 9):.0f}ms\n"
        f"  total         median={statistics.median(totals):.0f}ms\n"
        f"  cache_read    median={statistics.median(reads):.0f} tokens\n"
        f"  cost          ${cost:.5f} total, ${cost / len(ttfts):.5f} per turn"
    )


async def main() -> None:
    load_dotenv()
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    models = sys.argv[2:] or MODELS
    for model in models:
        await bench(model, n)


if __name__ == "__main__":
    asyncio.run(main())
