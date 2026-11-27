"""Service settings, read from the environment (Railway variables, or ``.env`` in development).

| Variable | Meaning |
| --- | --- |
| ``DATABASE_URL`` | app Postgres (Neon) URL; required in production |
| ``ENGLISH_TUTOR_BACKEND_API_KEY`` | key of the app backend (``X-API-Key`` header) |
| ``ENGLISH_TUTOR_PIPECAT_API_KEY`` | key of the voice bot (``X-API-Key`` header) |
| ``ENGLISH_TUTOR_TOKEN_SECRET`` | HS256 secret shared with the backend for browser tokens |
| ``ENGLISH_TUTOR_CORS_ORIGINS`` | comma-separated web origins allowed to call the service |
| ``ENGLISH_TUTOR_DOCS`` | ``off`` hides ``/docs`` and ``/openapi.json`` (default ``on``) |
| ``ENGLISH_TUTOR_TURNS_PER_MINUTE`` | turns one learner may start per minute (default 20) |
| ``ENGLISH_TUTOR_SESSIONS_PER_HOUR`` | sessions one learner may open per hour (default 30) |
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, Field

from english_tutor.config import ConfigError

DATABASE_URL_ENV = "DATABASE_URL"
API_KEY_ENVS: dict[str, str] = {
    "backend": "ENGLISH_TUTOR_BACKEND_API_KEY",
    "pipecat": "ENGLISH_TUTOR_PIPECAT_API_KEY",
}
TOKEN_SECRET_ENV = "ENGLISH_TUTOR_TOKEN_SECRET"
CORS_ORIGINS_ENV = "ENGLISH_TUTOR_CORS_ORIGINS"
DOCS_ENV = "ENGLISH_TUTOR_DOCS"
TURNS_PER_MINUTE_ENV = "ENGLISH_TUTOR_TURNS_PER_MINUTE"
SESSIONS_PER_HOUR_ENV = "ENGLISH_TUTOR_SESSIONS_PER_HOUR"

MIN_SECRET_CHARS = 32  # e.g. `openssl rand -hex 32` gives 64
DEFAULT_TURNS_PER_MINUTE = 20
DEFAULT_SESSIONS_PER_HOUR = 30
TOKEN_AUDIENCE = "english-tutor"
MAX_TOKEN_LIFETIME_S = 3600  # browser tokens are short-lived (5-15 minutes planned)
TURN_LOCK_WAIT_S = 5.0  # a new turn waits this long for the previous one of the session


class ApiSettings(BaseModel):
    """Everything the HTTP layer needs; secrets never leave this object."""

    database_url: str | None = Field(default=None, repr=False)
    api_keys: dict[str, str] = Field(default_factory=dict, repr=False)  # caller -> key
    token_secret: str | None = Field(default=None, repr=False)
    token_audience: str = TOKEN_AUDIENCE
    max_token_lifetime_s: int = MAX_TOKEN_LIFETIME_S
    cors_origins: list[str] = Field(default_factory=list)
    docs_enabled: bool = True
    turns_per_minute: int = DEFAULT_TURNS_PER_MINUTE
    sessions_per_hour: int = DEFAULT_SESSIONS_PER_HOUR
    turn_lock_wait_s: float = TURN_LOCK_WAIT_S
    auth_disabled: bool = False  # local dev server only; never set from the environment

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ApiSettings:
        """Read the variables listed in the module docstring; empty values count as unset."""
        env = os.environ if environ is None else environ

        def value(name: str) -> str | None:
            raw = env.get(name, "").strip()
            return raw or None

        keys = {caller: key for caller, name in API_KEY_ENVS.items() if (key := value(name))}
        origins = [o.strip().rstrip("/") for o in (value(CORS_ORIGINS_ENV) or "").split(",")]
        return cls(
            database_url=value(DATABASE_URL_ENV),
            api_keys=keys,
            token_secret=value(TOKEN_SECRET_ENV),
            cors_origins=[o for o in origins if o],
            docs_enabled=(value(DOCS_ENV) or "on").lower() not in {"off", "false", "0", "no"},
            turns_per_minute=_positive_int(env, TURNS_PER_MINUTE_ENV, DEFAULT_TURNS_PER_MINUTE),
            sessions_per_hour=_positive_int(env, SESSIONS_PER_HOUR_ENV, DEFAULT_SESSIONS_PER_HOUR),
        )

    def validate_for_production(self) -> None:
        """Raise ConfigError unless the database and at least one credential are configured."""
        problems: list[str] = []
        if not self.database_url:
            problems.append(f"{DATABASE_URL_ENV} is not set")
        if not self.api_keys and not self.token_secret:
            names = ", ".join([*API_KEY_ENVS.values(), TOKEN_SECRET_ENV])
            problems.append(f"no credential configured (set at least one of {names})")
        for caller, key in self.api_keys.items():
            if len(key) < MIN_SECRET_CHARS:
                problems.append(f"{API_KEY_ENVS[caller]} must have >= {MIN_SECRET_CHARS} chars")
        if self.token_secret is not None and len(self.token_secret) < MIN_SECRET_CHARS:
            problems.append(f"{TOKEN_SECRET_ENV} must have >= {MIN_SECRET_CHARS} chars")
        if len(set(self.api_keys.values())) < len(self.api_keys):
            problems.append("each caller needs its own API key")
        if problems:
            raise ConfigError("; ".join(problems))


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        number = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from None
    if number < 1:
        raise ConfigError(f"{name} must be at least 1")
    return number
