"""Model prices (``data/pricing.yaml``) and cost estimates for calls without a reported cost."""

from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml
from pydantic import BaseModel

PRICING_PATH = Path(__file__).resolve().parent / "data" / "pricing.yaml"
PER_TOKENS = 1_000_000


class ModelPrice(BaseModel):
    """USD per 1 million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float


@cache
def load_prices(path: Path = PRICING_PATH) -> dict[str, ModelPrice]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {model: ModelPrice.model_validate(price) for model, price in raw.items()}


def estimate_cost(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float | None:
    """Cost in USD from the price table; ``None`` for a model that is not listed."""
    price = load_prices().get(model)
    if price is None:
        return None
    total = (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_read_tokens * price.cache_read
        + cache_write_tokens * price.cache_write
    )
    return total / PER_TOKENS
