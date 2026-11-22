"""Measure teacher TTFT and cache reads from this machine.

Usage: uv run python -m ea_llm.evals.bench_latency [n]

The prompt is synthetic (~3k tokens, enough for caching); swap in the real prompt once the
builder output is final.
"""

import asyncio
import statistics
import sys
import time
import uuid

from dotenv import load_dotenv

from ea_llm.providers.base import (
    CallMeta,
    LLMRequest,
    Message,
    ProviderError,
    SystemBlock,
    TextDelta,
    TextPart,
    UsageReport,
)
from ea_llm.providers.openrouter_provider import OpenRouterProvider

RULES = (
    "You are a Vietnamese English teacher who teaches in Vietnamese. Everything you say is inside "
    "<vi> or <en> tags. At most 3 short sentences. Praise one specific thing before correcting. "
    "Correct at most 2 mistakes per turn. Never say scores. "
) * 60
PERSONA = "The learner is CEFR A2. Address the learner as em. Moderate amount of Vietnamese. " * 10
MODELS = ["anthropic/claude-haiku-5.5", "openai/gpt-6-luna"]


async def bench(model: str, n: int) -> None:
    provider = OpenRouterProvider().with_model(model)
    sid = str(uuid.uuid4())
    history: list[Message] = []
    ttfts, totals, reads = [], [], []
    for i in range(n):
        user = Message(
            role="user",
            content=[TextPart(text=f"Yesterday I go to the market number {i}.")],
            cache="5m",
        )
        req = LLMRequest(
            role="teacher",
            system=[SystemBlock(text=RULES, cache="1h"), SystemBlock(text=PERSONA, cache="1h")],
            messages=[*history, user],
            max_tokens=200,
            metadata=CallMeta(session_id=sid, user_id="bench", prompt_version="bench@0"),
        )
        t0 = time.perf_counter()
        ttft, text, usage, err = None, "", None, None
        async for ev in provider.stream(req):
            if isinstance(ev, TextDelta):
                ttft = ttft or (time.perf_counter() - t0) * 1000
                text += ev.text
            elif isinstance(ev, UsageReport):
                usage = ev
            elif isinstance(ev, ProviderError):
                err = ev
        if err or ttft is None:
            print(f"{model} #{i} ERROR {err}")
            continue
        ttfts.append(ttft)
        totals.append((time.perf_counter() - t0) * 1000)
        reads.append(usage.cache_read_tokens if usage else 0)
        history += [
            Message(role="user", content=user.content),
            Message(role="assistant", content=[TextPart(text=text)]),
        ]
    if not ttfts:
        print(model, "no results")
        return
    q = statistics.quantiles(ttfts, n=10) if len(ttfts) >= 2 else ttfts
    print(
        f"{model}: n={len(ttfts)} "
        f"TTFT median={statistics.median(ttfts):.0f}ms p90={q[-1]:.0f}ms max={max(ttfts):.0f}ms "
        f"total median={statistics.median(totals):.0f}ms "
        f"cache_read median={statistics.median(reads):.0f}"
    )


async def main() -> None:
    load_dotenv()
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    for m in MODELS:
        await bench(m, n)


if __name__ == "__main__":
    asyncio.run(main())
