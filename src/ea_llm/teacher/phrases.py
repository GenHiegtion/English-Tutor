"""Fixed Vietnamese phrases played without calling the LLM."""

from __future__ import annotations

from ea_llm.models import Address
from ea_llm.teacher.context import Channel


def _cap(word: str) -> str:
    return word[:1].upper() + word[1:]


def back_to_lesson(address: Address) -> str:
    return f"{_cap(address.teacher)} quay lại bài nhé."


def retry_prompt(address: Address, channel: Channel) -> str:
    verb = "viết" if channel == "text" else "nói"
    return f"{_cap(address.learner)} thử {verb} lại câu vừa rồi nhé."


def scenario_intro(address: Address, goal_vi: str) -> str:
    return f"{_cap(address.teacher)} và {address.learner} cùng luyện một tình huống: {goal_vi}"
