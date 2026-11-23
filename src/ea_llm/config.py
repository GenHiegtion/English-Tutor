"""Model routing configuration per role.

Model ids can be overridden from the environment (``.env``), without code changes:

- ``EA_LLM_<ROLE>_MODEL``: primary model of the role, e.g. ``EA_LLM_TEACHER_MODEL``.
- ``EA_LLM_<ROLE>_FALLBACK_MODEL``: hedged fallback; ``none`` disables it.

Unset or empty variables keep the defaults below. Token limits, effort and timeouts stay in code:
they are tuned together with the prompts.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel

Effort = Literal["low", "medium", "high", "xhigh", "max"]
ENV_PREFIX = "EA_LLM_"
DISABLED = "none"


class ConfigError(ValueError):
    """Invalid or missing configuration (environment variables)."""


class ModelRoute(BaseModel):
    model: str
    effort: Effort = "low"
    thinking: Literal["disabled", "adaptive"] = "disabled"
    max_tokens: int = 400
    timeout_s: float = 30.0
    fallback_model: str | None = None


# Starting values; re-measure with ea_llm.evals.bench_latency.
# Background roles default to Haiku to keep costs low; a stronger model (e.g. Sonnet) can be
# set per role through the environment once the budget allows.
DEFAULT_ROUTES: dict[str, ModelRoute] = {
    "teacher": ModelRoute(
        model="anthropic/claude-haiku-5.5",
        fallback_model="openai/gpt-6-luna",
        max_tokens=700,
        timeout_s=6.0,
    ),
    "examiner": ModelRoute(
        model="anthropic/claude-haiku-5.5", effort="medium", max_tokens=1500, timeout_s=30.0
    ),
    "reporter": ModelRoute(
        model="anthropic/claude-haiku-5.5", effort="medium", max_tokens=2500, timeout_s=60.0
    ),
    "writer": ModelRoute(
        model="anthropic/claude-haiku-5.5", effort="medium", max_tokens=4000, timeout_s=120.0
    ),
}


def load_routes(environ: Mapping[str, str] | None = None) -> dict[str, ModelRoute]:
    """``DEFAULT_ROUTES`` with model ids overridden by ``EA_LLM_<ROLE>_[FALLBACK_]MODEL``."""
    env = os.environ if environ is None else environ
    routes: dict[str, ModelRoute] = {}
    for role, route in DEFAULT_ROUTES.items():
        update: dict[str, str | None] = {}
        prefix = f"{ENV_PREFIX}{role.upper()}"
        if model := env.get(f"{prefix}_MODEL", "").strip():
            update["model"] = _model_id(f"{prefix}_MODEL", model)
        if fallback := env.get(f"{prefix}_FALLBACK_MODEL", "").strip():
            disabled = fallback.lower() == DISABLED
            update["fallback_model"] = (
                None if disabled else _model_id(f"{prefix}_FALLBACK_MODEL", fallback)
            )
        routes[role] = route.model_copy(update=update)
    return routes


def _model_id(name: str, value: str) -> str:
    vendor, _, model = value.partition("/")
    if not vendor or not model or " " in value:
        raise ConfigError(
            f"{name} must be an OpenRouter model id like 'vendor/model', got {value!r}"
        )
    return value
