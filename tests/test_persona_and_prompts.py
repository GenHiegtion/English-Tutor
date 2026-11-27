import pytest

from english_tutor.adapters.memory import MemoryPersonaStore, load_seed_scenarios
from english_tutor.models import LearnerProfile, Persona, SessionSnapshot
from english_tutor.prompts.builder import build_system
from english_tutor.prompts.registry import static_bundle
from english_tutor.teacher.persona import (
    DEFAULT_ADDRESS,
    compute_address,
    given_name,
    render_session_block,
    spoken_name,
)
from english_tutor.teacher.tools import tools_for

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
        ("lan", "under_18", "Chào em, cô là Lan đây. Cô và em học tiếp từ chỗ vừa dừng nhé."),
        ("lan", "adult", "Chào bạn, mình là Lan đây. Mình và bạn học tiếp từ chỗ vừa dừng nhé."),
        ("minh", "senior", "Chào bác, cháu là Minh đây! Cháu và bác học tiếp luôn nhé."),
    ],
)
def test_switch_greeting_follows_the_learner_age_band(persona, age, expected):
    p = PERSONAS[persona]
    assert compute_address(p, age).fill(p.switch_greeting) == expected


@pytest.mark.parametrize(
    ("persona", "age", "spoken"),
    [
        ("lan", "under_18", "cô Lan"),
        ("lan", "adult", "Lan"),
        ("minh", "under_18", "anh Minh"),
        ("minh", "senior", "Minh"),  # "cháu là Minh", never "cháu là Anh Minh"
        ("hung", "under_18", "thầy Hùng"),
        ("hung", "adult", "Hùng"),
    ],
)
def test_the_teacher_says_their_name_with_the_title_of_the_address(persona, age, spoken):
    p = PERSONAS[persona]
    address = compute_address(p, age)
    assert spoken_name(p, address) == spoken
    block = render_session_block(p, address, LearnerProfile(user_id="u", age_band=age), None)
    assert f"Name: {given_name(p)} (" in block and f'say "{spoken}"' in block


def test_a_title_stored_in_the_name_is_not_repeated():
    old = PERSONAS["minh"].model_copy(update={"name": "Anh Minh"})
    assert given_name(old) == "Minh"
    assert spoken_name(old, compute_address(old, "senior")) == "Minh"
    assert given_name(old.model_copy(update={"name": "Anh"})) == "Anh"  # a name, not a title


def test_switch_greeting_rejects_unknown_placeholders():
    data = PERSONAS["lan"].model_dump() | {"switch_greeting": "Chào {name}"}
    with pytest.raises(ValueError, match="name"):
        Persona.model_validate(data)


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
