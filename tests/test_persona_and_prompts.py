import pytest

from ea_llm.adapters.memory import MemoryPersonaStore, load_seed_scenarios
from ea_llm.models import LearnerProfile, Persona, SessionSnapshot
from ea_llm.prompts.builder import build_system
from ea_llm.prompts.registry import static_bundle
from ea_llm.teacher.persona import DEFAULT_ADDRESS, compute_address, render_session_block
from ea_llm.teacher.tools import tools_for

PERSONAS = {p.id: p for p in MemoryPersonaStore.from_seed()._by_id.values()}


def snapshot(user, persona, *, age="adult", cefr="A2", name=None):
    learner = LearnerProfile(user_id=user, age_band=age, cefr=cefr, display_name=name)
    return SessionSnapshot(
        session_id=f"s-{user}",
        user_id=user,
        channel="text",
        mode="scenario",
        persona_id=persona.id,
        address=compute_address(persona, age),
        learner=learner,
        prompt_version=static_bundle("text", "scenario").version,
        scenario=load_seed_scenarios()[0],
    )


def test_seed_personas_are_valid_and_complete():
    assert set(PERSONAS) == {"lan", "minh", "hung", "chi"}
    for persona in PERSONAS.values():
        assert set(persona.address) == {"under_18", "adult", "senior", "unknown"}


@pytest.mark.parametrize(
    ("persona", "age", "expected"),
    [
        ("lan", "adult", ("mình", "bạn")),
        ("lan", "under_18", ("cô", "em")),
        ("minh", "under_18", ("anh", "em")),
        ("hung", "senior", ("tôi", "bác")),
        ("chi", "unknown", ("mình", "bạn")),
    ],
)
def test_address_by_learner_age(persona, age, expected):
    a = compute_address(PERSONAS[persona], age)
    assert (a.teacher, a.learner) == expected


def test_address_falls_back_to_neutral():
    bare: Persona = PERSONAS["lan"].model_copy(update={"address": {}})
    assert compute_address(bare, "senior") == DEFAULT_ADDRESS


def test_layers_1_and_2_are_byte_identical_across_users():
    a = build_system(snapshot("u1", PERSONAS["lan"], name="An"), PERSONAS["lan"])
    b = build_system(snapshot("u2", PERSONAS["hung"], age="senior", cefr="B1"), PERSONAS["hung"])
    assert a[0].text == b[0].text  # static layer shared by everyone
    assert a[1].text != b[1].text  # session layer differs
    assert a[0].cache == a[1].cache == "1h"
    assert tools_for("text", "scenario") == tools_for("text", "scenario")


def test_session_block_is_deterministic_and_persona_driven():
    snap = snapshot("u1", PERSONAS["lan"], age="under_18", name="Mai")
    one = render_session_block(PERSONAS["lan"], snap.address, snap.learner, snap.scenario)
    kid = snap.learner.model_copy(update={"age_band": "under_18"})
    assert "Age group: under 18" in render_session_block(
        PERSONAS["lan"], snap.address, kid, snap.scenario
    )
    two = render_session_block(PERSONAS["lan"], snap.address, snap.learner, snap.scenario)
    assert one == two
    assert '"cô"' in one and '"em"' in one and "A2" in one and "Mai" in one
    assert "Ordering at a cafe" in one


def test_static_prompt_is_long_enough_to_cache_and_versioned():
    bundle = static_bundle("text", "scenario")
    assert len(bundle.text) / 3 > 512  # well above Haiku's minimum cacheable prefix
    assert bundle.version.startswith("teacher.text.scenario#")
    assert "channel: text chat" in bundle.text.lower()
    assert "Mode: scenario" in bundle.text


def test_tool_sets_by_mode():
    names = [t.name for t in tools_for("text", "scenario")]
    assert names == ["ask_question", "show_board", "record_mistake"]
    mistake = next(t for t in tools_for("text", "scenario") if t.name == "record_mistake")
    assert "pronunciation" not in mistake.input_schema["properties"]["type"]["enum"]
    voice = next(t for t in tools_for("voice", "free_chat") if t.name == "record_mistake")
    assert "spelling" not in voice.input_schema["properties"]["type"]["enum"]
    assert "ask_repeat" in [t.name for t in tools_for("voice", "lesson")]
