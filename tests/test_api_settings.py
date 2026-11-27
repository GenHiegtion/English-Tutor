import pytest

from english_tutor.api.ratelimit import PRUNE_EVERY, SlidingWindowLimiter
from english_tutor.api.settings import ApiSettings
from english_tutor.config import ConfigError

KEY = "k" * 32
OTHER_KEY = "o" * 32
SECRET = "s" * 32


def test_from_env_reads_every_setting():
    settings = ApiSettings.from_env(
        {
            "DATABASE_URL": "postgresql://u:p@h/db",
            "ENGLISH_TUTOR_TALKWISE_API_KEY": KEY,
            "ENGLISH_TUTOR_PIPECAT_API_KEY": " ",
            "ENGLISH_TUTOR_TOKEN_SECRET": SECRET,
            "ENGLISH_TUTOR_CORS_ORIGINS": "https://a.example/, https://b.example,,",
            "ENGLISH_TUTOR_DOCS": "off",
            "ENGLISH_TUTOR_TURNS_PER_MINUTE": "5",
            "ENGLISH_TUTOR_SESSIONS_PER_HOUR": "7",
        }
    )
    assert settings.api_keys == {"talkwise": KEY}
    assert settings.cors_origins == ["https://a.example", "https://b.example"]
    assert not settings.docs_enabled
    assert (settings.turns_per_minute, settings.sessions_per_hour) == (5, 7)
    assert not settings.auth_disabled
    settings.validate_for_production()


def test_from_env_defaults():
    settings = ApiSettings.from_env({})
    assert settings.database_url is None and settings.api_keys == {}
    assert settings.docs_enabled and settings.cors_origins == []
    assert settings.turns_per_minute == 20


def test_secrets_stay_out_of_repr():
    settings = ApiSettings(database_url="postgresql://u:pw@h/db", token_secret=SECRET)
    assert "pw" not in repr(settings) and SECRET not in repr(settings)


@pytest.mark.parametrize("value", ["zero", "0", "-3"])
def test_bad_limits_are_rejected(value):
    with pytest.raises(ConfigError, match="ENGLISH_TUTOR_TURNS_PER_MINUTE"):
        ApiSettings.from_env({"ENGLISH_TUTOR_TURNS_PER_MINUTE": value})


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (ApiSettings(api_keys={"talkwise": KEY}), "DATABASE_URL is not set"),
        (ApiSettings(database_url="postgresql://h/db"), "no credential configured"),
        (
            ApiSettings(database_url="postgresql://h/db", api_keys={"talkwise": "short"}),
            "ENGLISH_TUTOR_TALKWISE_API_KEY must have >= 32 chars",
        ),
        (
            ApiSettings(database_url="postgresql://h/db", token_secret="short"),
            "ENGLISH_TUTOR_TOKEN_SECRET must have >= 32 chars",
        ),
        (
            ApiSettings(
                database_url="postgresql://h/db", api_keys={"talkwise": KEY, "pipecat": KEY}
            ),
            "each caller needs its own API key",
        ),
    ],
)
def test_production_needs_database_and_strong_credentials(settings, message):
    with pytest.raises(ConfigError, match=message):
        settings.validate_for_production()


def test_production_accepts_distinct_keys():
    ApiSettings(
        database_url="postgresql://h/db", api_keys={"talkwise": KEY, "pipecat": OTHER_KEY}
    ).validate_for_production()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_limiter_allows_limit_hits_per_window():
    clock = Clock()
    limiter = SlidingWindowLimiter(2, 60, clock=clock)
    assert limiter.hit("a") is None
    clock.now += 10
    assert limiter.hit("a") is None
    assert limiter.hit("a") == 50  # the first hit leaves the window in 50 s
    assert limiter.hit("b") is None  # keys are independent
    clock.now += 50
    assert limiter.hit("a") is None


def test_limiter_drops_idle_keys():
    clock = Clock()
    limiter = SlidingWindowLimiter(1, 1, clock=clock)
    for i in range(PRUNE_EVERY - 1):
        limiter.hit(f"user-{i}")
    clock.now += 5
    limiter.hit("last")
    assert set(limiter._hits) == {"last"}
