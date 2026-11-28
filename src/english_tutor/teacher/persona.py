"""Address computation and the per-session persona/learner prompt block (layer 3)."""

from __future__ import annotations

from english_tutor.models import Address, AgeBand, CefrLevel, LearnerProfile, Persona
from english_tutor.teacher.context import ScenarioRef

DEFAULT_ADDRESS = Address(teacher="mình", learner="bạn")
# Vietnamese pronouns that are also titles: a teacher who calls themself "cô" says "cô Lan".
TITLE_PRONOUNS = frozenset({"cô", "thầy", "anh", "chị", "chú", "bác", "dì", "cậu"})

# Target share of Vietnamese words by learner level.
AGE_LABELS: dict[str, str] = {
    "under_18": "under 18 (a child or teenager)",
    "adult": "adult",
    "senior": "older adult",
}
VI_TARGET_PCT: dict[CefrLevel, int] = {"A1": 70, "A2": 50, "B1": 35, "B2": 20, "C1": 10, "C2": 10}
# Repeated in the session block, next to the level, where the model weighs it most.
VI_USE_BY_LEVEL: dict[CefrLevel, str] = {
    "A1": "Use Vietnamese for explanations and short hints; keep English very simple.",
    "A2": "Use Vietnamese for explanations; react and ask in simple English.",
    "B1": "Use Vietnamese only for a short correction; everything else is in English.",
    "B2": "Use Vietnamese only for a short correction; everything else is in English.",
    "C1": "Use English for everything, corrections included, unless the learner writes in "
    "Vietnamese.",
    "C2": "Use English for everything, corrections included, unless the learner writes in "
    "Vietnamese.",
}
MAX_WORDS_PER_SENTENCE: dict[CefrLevel, int | None] = {
    "A1": 8,
    "A2": 12,
    "B1": 18,
    "B2": 25,
    "C1": None,
    "C2": None,
}


def compute_address(persona: Persona, learner_age_band: AgeBand) -> Address:
    """Pick the teacher's pronouns for this learner; fall back to neutral 'mình - bạn'."""
    return (
        persona.address.get(learner_age_band) or persona.address.get("unknown") or DEFAULT_ADDRESS
    )


def given_name(persona: Persona) -> str:
    """The teacher's name without a leading title ("Cô Lan" -> "Lan").

    Titles depend on who the learner is, so they come from the address, never from the name.
    Older rows may still store the title in the name.
    """
    words = persona.name.split()
    while len(words) > 1 and words[0].lower() in TITLE_PRONOUNS:
        words.pop(0)
    return " ".join(words)


def spoken_name(persona: Persona, address: Address) -> str:
    """How the teacher says their own name to this learner: "cô Lan" to a child, "Lan" else."""
    name = given_name(persona)
    teacher = address.teacher.lower()
    return f"{teacher} {name}" if teacher in TITLE_PRONOUNS else name


def render_session_block(
    persona: Persona,
    address: Address,
    learner: LearnerProfile,
    scenario: ScenarioRef | None,
) -> str:
    """Deterministic text: the same inputs always give the same bytes (cache-friendly)."""
    lines = [
        "# SESSION",
        "",
        "## Your persona",
        f"Name: {given_name(persona)} ({persona.gender}).",
        f"Personality: {persona.personality.strip()}",
        f'Pronouns in Vietnamese: you call yourself "{address.teacher}" and call the learner '
        f'"{address.learner}".',
        f'When you say your own name, say "{spoken_name(persona, address)}", with no other title.',
        "Personality only changes your tone, never the teaching rules above.",
        "",
        "## The learner",
        f"CEFR level: {learner.cefr}.",
        f"Target share of Vietnamese words: about {VI_TARGET_PCT[learner.cefr]}%. "
        + VI_USE_BY_LEVEL[learner.cefr],
    ]
    if learner.age_band in AGE_LABELS:
        lines.append(f"Age group: {AGE_LABELS[learner.age_band]}.")
    if learner.display_name:
        lines.append(f"Name: {learner.display_name}.")
    if learner.interests:
        lines.append("Interests: " + ", ".join(learner.interests) + ".")
    if learner.recent_mistakes:
        lines.append("Recent mistakes: " + "; ".join(learner.recent_mistakes[:5]) + ".")
    if learner.memory_notes:
        lines.append("Notes:")
        lines.extend(f"- {note}" for note in learner.memory_notes[:5])
    if scenario is not None:
        lines += [
            "",
            "## Scenario",
            f"Title: {scenario.title} (level {scenario.level}).",
            f"Goal: {scenario.goal}",
            "Target words and phrases: " + ", ".join(scenario.targets) + ".",
        ]
    return "\n".join(lines)
