import pytest

from ea_llm.config import DEFAULT_ROUTES, ConfigError, load_routes


def test_defaults_when_nothing_is_set():
    assert load_routes({}) == DEFAULT_ROUTES


def test_model_and_fallback_overrides_apply_per_role():
    routes = load_routes(
        {
            "EA_LLM_TEACHER_MODEL": "openai/gpt-6-luna",
            "EA_LLM_TEACHER_FALLBACK_MODEL": "anthropic/claude-haiku-5.5",
            "EA_LLM_EXAMINER_MODEL": " anthropic/claude-opus-5.5 ",
        }
    )
    assert routes["teacher"].model == "openai/gpt-6-luna"
    assert routes["teacher"].fallback_model == "anthropic/claude-haiku-5.5"
    assert routes["teacher"].max_tokens == DEFAULT_ROUTES["teacher"].max_tokens
    assert routes["examiner"].model == "anthropic/claude-opus-5.5"
    assert routes["reporter"] == DEFAULT_ROUTES["reporter"]


def test_empty_keeps_default_and_none_disables_fallback():
    routes = load_routes({"EA_LLM_TEACHER_MODEL": "", "EA_LLM_TEACHER_FALLBACK_MODEL": "None"})
    assert routes["teacher"].model == DEFAULT_ROUTES["teacher"].model
    assert routes["teacher"].fallback_model is None


@pytest.mark.parametrize("bad", ["claude-haiku", "anthropic/", "/haiku", "anthropic/claude haiku"])
def test_malformed_model_id_is_rejected(bad):
    with pytest.raises(ConfigError, match="EA_LLM_TEACHER_MODEL"):
        load_routes({"EA_LLM_TEACHER_MODEL": bad})


def test_defaults_are_not_mutated():
    load_routes({"EA_LLM_TEACHER_MODEL": "openai/gpt-6-luna"})
    assert DEFAULT_ROUTES["teacher"].model == "anthropic/claude-haiku-5.5"
