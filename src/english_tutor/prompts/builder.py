"""Builds the Teacher request layers: tools, static system, session system."""

from __future__ import annotations

from english_tutor.models import Persona, SessionSnapshot
from english_tutor.prompts.registry import static_bundle
from english_tutor.providers.base import SystemBlock
from english_tutor.teacher.persona import render_session_block


def build_system(snapshot: SessionSnapshot, persona: Persona) -> list[SystemBlock]:
    """Layer 2 (shared by all users of a channel/mode) + layer 3 (per session).

    Both layers use a 1 hour cache; the 5 minute breakpoint sits on the newest user message.
    """
    static = static_bundle(snapshot.channel, snapshot.mode)
    session = render_session_block(persona, snapshot.address, snapshot.learner, snapshot.scenario)
    return [
        SystemBlock(text=static.text, cache="1h"),
        SystemBlock(text=session, cache="1h"),
    ]
