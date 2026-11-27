"""Fixed Vietnamese phrases played without calling the LLM."""

from __future__ import annotations

from english_tutor.models import Address, CefrLevel
from english_tutor.teacher.context import Channel

# "Chúng ta" includes both sides and fits every pronoun pair, so this line needs no address.
BACK_TO_LESSON = "Chúng ta quay lại bài học nhé."


def retry_prompt(address: Address, channel: Channel) -> str:
    verb = "viết" if channel == "text" else "nói"
    return address.fill(f"{{Learner}} thử {verb} lại câu vừa rồi nhé.")


# Free chat opener: a Vietnamese line for beginners, then one English question at the level.
BEGINNER_LEVELS: frozenset[CefrLevel] = frozenset({"A1", "A2"})
FREE_CHAT_INTRO = "{Teacher} và {learner} cùng nói chuyện bằng tiếng Anh nhé, chủ đề gì cũng được."
FREE_CHAT_QUESTION_BEGINNER = "What do you want to talk about?"
FREE_CHAT_QUESTION = "What would you like to talk about today?"


def scenario_intro(address: Address, goal_vi: str) -> str:
    return address.fill("{Teacher} và {learner} cùng luyện một tình huống: ") + goal_vi


def free_chat_opener(address: Address, cefr: CefrLevel) -> list[tuple[str, str]]:
    """The (lang, text) lines that open a free chat, without calling the LLM."""
    if cefr in BEGINNER_LEVELS:
        return [("vi", address.fill(FREE_CHAT_INTRO)), ("en", FREE_CHAT_QUESTION_BEGINNER)]
    return [("en", FREE_CHAT_QUESTION)]
