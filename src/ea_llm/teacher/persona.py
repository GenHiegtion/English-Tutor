"""Address computation and the per-session persona/learner prompt block (layer 3)."""

from __future__ import annotations

from ea_llm.models import Address, AgeBand, CefrLevel, LearnerProfile, Persona
from ea_llm.teacher.context import ScenarioRef

DEFAULT_ADDRESS = Address(teacher="mình", learner="bạn")

# Target share of Vietnamese words by learner level.
VI_TARGET_PCT: dict[CefrLevel, int] = {"A1": 70, "A2": 50, "B1": 35, "B2": 20, "C1": 10, "C2": 10}
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
        f"Name: {persona.name} ({persona.gender}).",
        f"Personality: {persona.personality.strip()}",
        f'Pronouns in Vietnamese: you call yourself "{address.teacher}" and call the learner '
        f'"{address.learner}".',
        "Personality only changes your tone, never the teaching rules above.",
        "",
        "## The learner",
        f"CEFR level: {learner.cefr}.",
        f"Target share of Vietnamese words: about {VI_TARGET_PCT[learner.cefr]}%.",
    ]
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
