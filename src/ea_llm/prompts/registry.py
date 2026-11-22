"""Loads prompt templates once and derives a stable ``prompt_version``."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import cache
from importlib import resources

from ea_llm.teacher.context import Channel, Mode

TEMPLATE_PACKAGE = "ea_llm.prompts.templates"
STATIC_RULES = "static_rules.v1"


@dataclass(frozen=True)
class PromptBundle:
    """The static text of prompt layer 2 for one channel/mode combination."""

    text: str
    version: str  # e.g. "teacher.text.scenario@v1#a1b2c3"


@cache
def _read(name: str) -> str:
    path = resources.files(TEMPLATE_PACKAGE).joinpath("teacher", f"{name}.md")
    return path.read_text(encoding="utf-8").strip()


@cache
def static_bundle(channel: Channel, mode: Mode) -> PromptBundle:
    names = (STATIC_RULES, f"channel_{channel}.v1", f"mode_{mode}.v1")
    text = "\n\n".join(_read(n) for n in names)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:6]
    return PromptBundle(text=text, version=f"teacher.{channel}.{mode}@v1#{digest}")
