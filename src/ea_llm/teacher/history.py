"""Conversation history: tool_result pairing, rendering, merging and block-wise trimming."""

from __future__ import annotations

import json

from ea_llm.models import SessionSnapshot, StoredMessage
from ea_llm.providers.base import (
    Message,
    TextPart,
    ToolResultPart,
    ToolUsePart,
)
from ea_llm.teacher.parser import Segment

HARD_LIMIT_TOKENS = 6000
TARGET_TOKENS = 3500
MIN_RECENT_USER_MESSAGES = 4
SESSION_START_NOTE = "[System note: the session has started]"


def estimate_tokens(message: Message) -> int:
    """Rough token estimate (Vietnamese is token-heavy, so ~3 chars per token)."""
    chars = 0
    for part in message.content:
        if isinstance(part, TextPart):
            chars += len(part.text)
        elif isinstance(part, ToolUsePart):
            chars += len(json.dumps(part.args, ensure_ascii=False)) + len(part.name)
        else:
            chars += len(part.content)
    return chars // 3 + 4


def render_segments(segments: list[Segment]) -> str:
    """Rebuild the tagged text the model produced, from the segments the learner received."""
    out = []
    for seg in segments:
        attr = ' slow="true"' if seg.slow and seg.lang == "en" else ""
        out.append(f"<{seg.lang}{attr}>{seg.text}</{seg.lang}>")
    return "".join(out)


def json_result(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def pending_tool_results(
    stored: list[StoredMessage], terminal_result: dict | None
) -> list[ToolResultPart]:
    """tool_result parts for every tool_use of the last assistant message, in order."""
    if not stored or stored[-1].message.role != "assistant":
        return []
    last = stored[-1]
    parts = []
    for part in last.message.content:
        if isinstance(part, ToolUsePart):
            payload = last.tool_statuses.get(part.id) or terminal_result or {"status": "answered"}
            parts.append(ToolResultPart(tool_use_id=part.id, content=json_result(payload)))
    return parts


def to_messages(stored: list[StoredMessage]) -> list[Message]:
    """Render stored messages for the API: merge consecutive user messages.

    A window can start at a user message whose leading tool_result parts answer a tool_use
    that fell outside the window; those parts are dropped so every tool_result stays paired.
    """
    out: list[Message] = []
    for item in stored:
        msg = item.message
        if not out and msg.role == "user":
            kept = [p for p in msg.content if not isinstance(p, ToolResultPart)]
            msg = msg.model_copy(update={"content": kept or msg.content})
        if out and out[-1].role == msg.role == "user":
            out[-1] = Message(role="user", content=[*out[-1].content, *msg.content])
        else:
            out.append(msg.model_copy(update={"cache": "none"}))
    return out


def advance_window(snapshot: SessionSnapshot, stored: list[StoredMessage]) -> bool:
    """Move ``window_start`` forward in one block once the history is too long.

    Cutting in blocks keeps the prompt prefix stable for many turns instead of changing it
    every turn. The cut always lands before a user message that has learner text, so no
    tool_use is separated from its tool_result. Returns True when the window moved.
    """
    window = stored[snapshot.window_start :]
    if sum(estimate_tokens(m.message) for m in window) <= HARD_LIMIT_TOKENS:
        return False
    boundaries = [
        i
        for i, m in enumerate(window)
        if m.message.role == "user" and any(isinstance(p, TextPart) for p in m.message.content)
    ]
    # Keep at least the most recent user messages.
    candidates = boundaries[: max(0, len(boundaries) - MIN_RECENT_USER_MESSAGES)]
    cut = None
    for i in candidates:
        remaining = sum(estimate_tokens(m.message) for m in window[i:])
        cut = i
        if remaining < TARGET_TOKENS:
            break
    if cut is None or cut == 0:
        return False
    snapshot.window_start += cut
    return True
