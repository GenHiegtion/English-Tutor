"""Loads prompt templates once and derives a stable ``prompt_version``."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import cache
from importlib import resources

from english_tutor.teacher.context import Channel, Mode

TEMPLATE_PACKAGE = "english_tutor.prompts.templates"


@dataclass(frozen=True)
class PromptBundle:
    """The static text of prompt layer 2 for one channel/mode combination."""

    text: str
    version: str  # e.g. "teacher.text.scenario#a1b2c3"; the hash changes with any template edit


@cache
def _read(name: str) -> str:
    path = resources.files(TEMPLATE_PACKAGE).joinpath("teacher", f"{name}.md")
    return path.read_text(encoding="utf-8").strip()


@cache
def static_bundle(channel: Channel, mode: Mode) -> PromptBundle:
    names = ("static_rules", f"channel_{channel}", f"mode_{mode}")
    text = "\n\n".join(_read(n) for n in names)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:6]
    return PromptBundle(text=text, version=f"teacher.{channel}.{mode}#{digest}")
