"""Fixed Vietnamese phrases played without calling the LLM."""

from __future__ import annotations

from ea_llm.models import Address
from ea_llm.teacher.context import Channel

# "Chúng ta" includes both sides and fits every pronoun pair, so this line needs no address.
BACK_TO_LESSON = "Chúng ta quay lại bài học nhé."


def retry_prompt(address: Address, channel: Channel) -> str:
    verb = "viết" if channel == "text" else "nói"
    return address.fill(f"{{Learner}} thử {verb} lại câu vừa rồi nhé.")


def scenario_intro(address: Address, goal_vi: str) -> str:
    return address.fill("{Teacher} và {learner} cùng luyện một tình huống: ") + goal_vi
