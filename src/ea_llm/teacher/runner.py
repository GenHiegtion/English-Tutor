"""TeacherService: opens sessions and runs one teacher turn at a time."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field

from ea_llm.config import DEFAULT_ROUTES
from ea_llm.models import (
    LearnerProfile,
    Persona,
    SessionSnapshot,
    StoredMessage,
    TurnRecord,
    UsageRecord,
)
from ea_llm.ports import HistoryStore, LearnerStore, PersonaStore
from ea_llm.prompts.builder import build_system
from ea_llm.prompts.registry import static_bundle
from ea_llm.providers.base import (
    CallMeta,
    LLMProvider,
    LLMRequest,
    Message,
    ProviderError,
    Stop,
    TextDelta,
    TextPart,
    ToolCall,
    ToolUsePart,
    UsageReport,
)
from ea_llm.teacher import guards, phrases
from ea_llm.teacher.context import Channel, Mode, ScenarioRef, TurnContext
from ea_llm.teacher.events import (
    BoardUpdate,
    MistakeNoted,
    OutputSegment,
    TeacherEvent,
    TerminalAction,
    TurnEnd,
    TurnFailed,
)
from ea_llm.teacher.hedge import hedged_stream
from ea_llm.teacher.history import (
    SESSION_START_NOTE,
    advance_window,
    pending_tool_results,
    render_segments,
    to_messages,
)
from ea_llm.teacher.input_checks import check_input
from ea_llm.teacher.parser import LangTagParser, Segment
from ea_llm.teacher.persona import compute_address
from ea_llm.teacher.tools import (
    RecordMistakeArgs,
    ShowBoardArgs,
    allowed_tool_names,
    is_terminal,
    tools_for,
    validate_args,
)

logger = logging.getLogger(__name__)
RECENT_RATIO_TURNS = 10
LEARNER_PREFIX: dict[Channel, str] = {"voice": "[Học viên nói]:", "text": "[Học viên viết]:"}


@dataclass(frozen=True)
class TeacherConfig:
    hedge_after_s: float = 2.5
    give_up_after_s: float = 6.0


@dataclass
class _Turn:
    """Mutable state of the turn being run."""

    ctx: TurnContext
    snapshot: SessionSnapshot
    persona: Persona
    parser: LangTagParser
    seq_base: int = 0
    segments: list[Segment] = field(default_factory=list)
    model_segments: int = 0
    out_seq: int = 0
    tool_parts: list[ToolUsePart] = field(default_factory=list)
    tool_statuses: dict[str, dict] = field(default_factory=dict)
    emitted_tool_ids: set[str] = field(default_factory=set)
    terminal: ToolCall | None = None
    flags: list[str] = field(default_factory=list)
    mistakes: int = 0
    usage: UsageReport | None = None
    stop: str | None = None
    ttft_ms: int | None = None
    fallback_used: bool = False
    completed: bool = False


class TeacherService:
    def __init__(
        self,
        *,
        primary: LLMProvider,
        history: HistoryStore,
        personas: PersonaStore,
        learners: LearnerStore,
        fallback: LLMProvider | None = None,
        config: TeacherConfig | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.history = history
        self.personas = personas
        self.learners = learners
        self.config = config or TeacherConfig()
        self._delivered: dict[str, int] = {}  # turn_id -> last seq the learner received

    # ------------------------------------------------------------------ sessions
    async def open_session(
        self,
        *,
        session_id: str,
        user_id: str,
        channel: Channel,
        mode: Mode,
        persona_id: str,
        scenario: ScenarioRef | None = None,
    ) -> SessionSnapshot:
        if (mode == "scenario") != (scenario is not None):
            raise ValueError("scenario is required for (and only for) mode 'scenario'")
        if mode == "lesson":
            raise NotImplementedError("lesson mode is not implemented yet")
        persona = await self._require_persona(persona_id)
        learner = await self.learners.get_profile(user_id) or LearnerProfile(user_id=user_id)
        snapshot = SessionSnapshot(
            session_id=session_id,
            user_id=user_id,
            channel=channel,
            mode=mode,
            persona_id=persona.id,
            address=compute_address(persona, learner.age_band),
            learner=learner,
            prompt_version=static_bundle(channel, mode).version,
            scenario=scenario,
        )
        await self.history.save_session(snapshot)
        return snapshot

    def mark_delivered(self, turn_id: str, seq: int) -> None:
        """Tell the runner the highest segment ``seq`` the learner actually received.

        Used when a turn is cancelled: only delivered segments are kept in the history.
        Process-local state, so call it on the same process that runs the turn.
        """
        self._delivered[turn_id] = max(seq, self._delivered.get(turn_id, -1))

    async def _require_persona(self, persona_id: str) -> Persona:
        persona = await self.personas.get_persona(persona_id)
        if persona is None or persona.status != "published":
            raise ValueError(f"unknown or unpublished persona: {persona_id}")
        return persona

    # --------------------------------------------------------------------- turns
    async def run_turn(self, ctx: TurnContext) -> AsyncIterator[TeacherEvent]:
        snapshot = await self.history.load_session(ctx.session_id)
        stored = await self.history.load_messages(ctx.session_id) if snapshot else []
        if (
            snapshot is None
            or (snapshot.channel, snapshot.mode) != (ctx.channel, ctx.mode)
            or any(m.turn_id == ctx.turn_id for m in stored)
        ):
            yield TurnFailed(turn_id=ctx.turn_id, reason="invalid_context")
            return

        persona = await self.personas.get_persona(ctx.persona_id)
        if persona is None or persona.status != "published":
            yield TurnFailed(turn_id=ctx.turn_id, reason="invalid_context")
            return

        turn = _Turn(ctx, snapshot, persona, LangTagParser(ctx.channel))
        switch_note = None
        if ctx.persona_id != snapshot.persona_id:
            switch_note = await self._switch_persona(turn)
            yield self._segment_event(turn, "vi", persona.switch_greeting)

        if ctx.learner_input is None:
            if ctx.mode == "scenario" and not stored:
                async with aclosing(self._scenario_opener(turn)) as inner:
                    async for ev in inner:
                        yield ev
            else:
                yield TurnFailed(turn_id=ctx.turn_id, reason="invalid_context")
            return

        # aclosing: closing this generator must close the inner one, so cancel rules run.
        async with aclosing(self._llm_turn(turn, stored, switch_note)) as inner:
            async for ev in inner:
                yield ev

    # ------------------------------------------------------------ persona switch
    async def _switch_persona(self, turn: _Turn) -> str:
        persona, snap = turn.persona, turn.snapshot
        snap.persona_id = persona.id
        snap.address = compute_address(persona, snap.learner.age_band)
        await self.history.save_session(snap)
        turn.flags.append("persona_switched")
        return (
            f"[System note: the learner just switched teachers. From now on you are "
            f'{persona.name}; call yourself "{snap.address.teacher}" and the learner '
            f'"{snap.address.learner}". Continue exactly where the lesson stopped]'
        )

    # ----------------------------------------------------------- scenario opener
    async def _scenario_opener(self, turn: _Turn) -> AsyncIterator[TeacherEvent]:
        scenario = turn.snapshot.scenario
        assert scenario is not None
        if scenario.goal_vi:
            text = phrases.scenario_intro(turn.snapshot.address, scenario.goal_vi)
            yield self._segment_event(turn, "vi", text)
        else:
            turn.flags.append("missing_goal_vi")
        yield self._segment_event(turn, "en", scenario.opener)
        start = StoredMessage(
            turn_id=turn.ctx.turn_id,
            message=Message(role="user", content=[TextPart(text=SESSION_START_NOTE)]),
        )
        await self._save(turn, [start, self._assistant_stored(turn)], usage=None)
        turn.completed = True
        yield TerminalAction(turn_id=turn.ctx.turn_id, tool="open_turn")
        yield self._turn_end(turn, provider="none")

    # ------------------------------------------------------------------ LLM turn
    async def _llm_turn(
        self, turn: _Turn, stored: list[StoredMessage], switch_note: str | None
    ) -> AsyncIterator[TeacherEvent]:
        ctx, snapshot = turn.ctx, turn.snapshot
        assert ctx.learner_input is not None
        check = check_input(ctx.channel, ctx.learner_input)
        turn.flags += check.flags

        user_msg = self._user_message(turn, stored, check, switch_note)
        if advance_window(snapshot, stored):
            await self.history.save_session(snapshot)
        window = [*stored[snapshot.window_start :], user_msg]
        messages = to_messages(window)
        messages[-1] = messages[-1].model_copy(update={"cache": "5m"})

        route = DEFAULT_ROUTES["teacher"]  # token limit only; the provider picks the model
        request = LLMRequest(
            role="teacher",
            system=build_system(snapshot, turn.persona),
            messages=messages,
            tools=tools_for(ctx.channel, ctx.mode),
            max_tokens=route.max_tokens,
            metadata=CallMeta(
                session_id=ctx.session_id,
                user_id=ctx.user_id,
                turn_id=ctx.turn_id,
                channel=ctx.channel,
                mode=ctx.mode,
                prompt_version=snapshot.prompt_version,
            ),
        )

        started = time.monotonic()
        error: ProviderError | None = None
        stream = hedged_stream(
            self.primary,
            self.fallback,
            request,
            hedge_after_s=self.config.hedge_after_s,
            give_up_after_s=self.config.give_up_after_s,
        )
        try:
            async for event, source in stream:
                if isinstance(event, ProviderError):
                    error = event
                    break
                turn.fallback_used = source == "fallback"
                if isinstance(event, TextDelta):
                    if turn.ttft_ms is None:
                        turn.ttft_ms = int((time.monotonic() - started) * 1000)
                    for seg in turn.parser.feed(event.text):
                        for out in self._emit_segment(turn, seg):
                            yield out
                elif isinstance(event, ToolCall):
                    for out in self._handle_tool(turn, event):
                        yield out
                elif isinstance(event, UsageReport):
                    turn.usage = event
                elif isinstance(event, Stop):
                    turn.stop = event.reason

            if error is not None and not turn.segments and not turn.tool_parts:
                logger.warning(
                    "teacher turn failed: session=%s turn=%s kind=%s: %s",
                    ctx.session_id,
                    ctx.turn_id,
                    error.kind,
                    error.message,
                )
                turn.completed = True
                yield TurnFailed(turn_id=ctx.turn_id, reason="providers_down")
                return
            if error is not None:
                logger.warning(
                    "teacher stream broke mid-turn, keeping partial output: session=%s turn=%s "
                    "kind=%s",
                    ctx.session_id,
                    ctx.turn_id,
                    error.kind,
                )
                turn.flags.append("stream_error")

            for seg in turn.parser.finish():
                for out in self._emit_segment(turn, seg):
                    yield out
            self._finalize_flags(turn, stored)
            for out in self._fallback_phrases(turn):
                yield out

            usage = self._usage_record(turn)
            await self._save(turn, [user_msg, self._assistant_stored(turn)], usage)
            turn.completed = True
            terminal = turn.terminal
            yield TerminalAction(
                turn_id=ctx.turn_id,
                tool=terminal.name if terminal else "open_turn",  # type: ignore[arg-type]
                tool_use_id=terminal.id if terminal else None,
                args=terminal.args if terminal else {},
            )
            yield self._turn_end(turn)
        finally:
            await stream.aclose()  # cancels any provider task still running
            if not turn.completed:  # the caller cancelled the turn (barge-in / new message)
                await self._save_cancelled(turn, user_msg)

    # ------------------------------------------------------------------- helpers
    def _user_message(
        self, turn: _Turn, stored: list[StoredMessage], check, switch_note: str | None
    ) -> StoredMessage:
        ctx = turn.ctx
        assert ctx.learner_input is not None
        parts: list = list(pending_tool_results(stored, ctx.terminal_result))
        notes = [f"[System note: {n}]" for n in check.notes]
        if switch_note:
            notes.insert(0, switch_note)
        if notes:
            parts.append(TextPart(text="\n".join(notes)))
        if ctx.learner_input.silence_seconds is not None:
            parts.append(
                TextPart(text=f"[Học viên im lặng {ctx.learner_input.silence_seconds} giây]")
            )
        elif not check.skip_text:
            parts.append(TextPart(text=f"{LEARNER_PREFIX[ctx.channel]} {check.text}"))
        return StoredMessage(
            turn_id=ctx.turn_id,
            message=Message(role="user", content=parts),
            guard_flags=check.flags,
        )

    def _segment_event(self, turn: _Turn, lang: str, text: str) -> OutputSegment:
        seg = Segment(seq=turn.out_seq, lang=lang, text=text)  # type: ignore[arg-type]
        turn.segments.append(seg)
        turn.out_seq += 1
        return OutputSegment(
            turn_id=turn.ctx.turn_id, seq=seg.seq, lang=seg.lang, text=seg.text, is_filler=False
        )

    def _emit_segment(self, turn: _Turn, seg: Segment) -> list[TeacherEvent]:
        result = guards.check_segment(seg)
        turn.flags += result.flags
        if result.segment is None:
            return []
        kept = result.segment
        out_seq = turn.out_seq
        turn.out_seq += 1
        stored_seg = Segment(**{**kept.__dict__, "seq": out_seq})
        turn.segments.append(stored_seg)
        turn.model_segments += 1
        return [
            OutputSegment(
                turn_id=turn.ctx.turn_id,
                seq=out_seq,
                lang=kept.lang,
                text=kept.text,
                slow=kept.slow and turn.ctx.channel == "voice",
                protected=kept.protected,
            )
        ]

    def _handle_tool(self, turn: _Turn, call: ToolCall) -> list[TeacherEvent]:
        ctx = turn.ctx
        turn.tool_parts.append(ToolUsePart(id=call.id, name=call.name, args=call.args))
        if call.name not in allowed_tool_names(ctx.channel, ctx.mode):
            turn.flags.append("tool_not_allowed")
            turn.tool_statuses[call.id] = {"status": "invalid", "error": "tool"}
            return []
        parsed, bad_field = validate_args(call.name, call.args, ctx.channel)
        if parsed is None:
            turn.flags.append("invalid_tool_args")
            turn.tool_statuses[call.id] = {"status": "invalid", "error": bad_field}
            return []
        if is_terminal(call.name):
            if turn.terminal is not None:
                turn.flags.append("multi_terminal")
                turn.tool_statuses[call.id] = {"status": "ignored"}
            else:
                turn.terminal = call
            return []
        turn.tool_statuses[call.id] = {"status": "ok"}
        turn.emitted_tool_ids.add(call.id)
        if isinstance(parsed, ShowBoardArgs):
            return [BoardUpdate(turn_id=ctx.turn_id, **parsed.model_dump())]
        assert isinstance(parsed, RecordMistakeArgs)
        turn.mistakes += 1
        return [MistakeNoted(turn_id=ctx.turn_id, **parsed.model_dump())]

    def _finalize_flags(self, turn: _Turn, stored: list[StoredMessage]) -> None:
        recent = _recent_segments(stored, turn.segments)
        turn.flags += guards.check_turn(
            turn.segments,
            address=turn.snapshot.address,
            cefr=turn.snapshot.learner.cefr,
            mistakes_recorded=turn.mistakes,
            recent_vi_ratio=guards.vi_ratio(recent),
        )
        if turn.parser.dropped_chars:
            turn.flags.append("dropped_chars")
        if turn.parser.unclosed_tag:
            turn.flags.append("unclosed_tag")
        if turn.stop == "max_tokens":
            turn.flags.append("max_tokens")

    def _fallback_phrases(self, turn: _Turn) -> list[TeacherEvent]:
        """Fixed lines for a refused or empty turn; they are stored in the history too."""
        address = turn.snapshot.address
        if turn.stop == "refusal":
            turn.flags.append("refusal")
            return [self._segment_event(turn, "vi", phrases.back_to_lesson(address))]
        if turn.model_segments == 0:
            turn.flags.append("empty_turn")
            text = phrases.retry_prompt(address, turn.ctx.channel)
            return [self._segment_event(turn, "vi", text)]
        return []

    def _assistant_stored(
        self, turn: _Turn, segments: list[Segment] | None = None, *, cancelled: bool = False
    ) -> StoredMessage:
        """Assistant message of the turn.

        A completed turn keeps every tool_use (each gets a tool_result next turn). A cancelled
        turn keeps only action tools that already emitted their event; the unexecuted terminal
        tool is dropped.
        """
        spoken = turn.segments if segments is None else segments
        content: list = []
        if spoken:
            content.append(TextPart(text=render_segments(spoken)))
        content += [p for p in turn.tool_parts if not cancelled or p.id in turn.emitted_tool_ids]
        return StoredMessage(
            turn_id=turn.ctx.turn_id,
            message=Message(role="assistant", content=content),
            persona_id=turn.persona.id,
            guard_flags=sorted(set(turn.flags)),
            tool_statuses=turn.tool_statuses,
        )

    def _usage_record(self, turn: _Turn, *, cancelled: bool = False) -> UsageRecord | None:
        usage = turn.usage
        if usage is None:
            return None
        return UsageRecord(
            turn_id=turn.ctx.turn_id,
            session_id=turn.ctx.session_id,
            user_id=turn.ctx.user_id,
            role="teacher",
            model=usage.model,
            provider=usage.provider,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cost_usd=usage.cost_usd,
            ttft_ms=turn.ttft_ms,
            fallback_used=turn.fallback_used,
            cancelled=cancelled,
        )

    def _turn_end(self, turn: _Turn, provider: str | None = None) -> TurnEnd:
        usage = turn.usage
        return TurnEnd(
            turn_id=turn.ctx.turn_id,
            ttft_ms=turn.ttft_ms,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            cache_read_tokens=usage.cache_read_tokens if usage else 0,
            cache_write_tokens=usage.cache_write_tokens if usage else 0,
            dropped_chars=turn.parser.dropped_chars,
            guard_flags=sorted(set(turn.flags)),
            provider=provider or (usage.provider if usage else "unknown"),
            fallback_used=turn.fallback_used,
        )

    async def _save(
        self, turn: _Turn, messages: list[StoredMessage], usage: UsageRecord | None
    ) -> None:
        await self.history.append_turn(
            turn.ctx.session_id,
            TurnRecord(turn_id=turn.ctx.turn_id, messages=messages, usage=usage),
        )
        self._delivered.pop(turn.ctx.turn_id, None)

    async def _save_cancelled(self, turn: _Turn, user_msg: StoredMessage) -> None:
        """Apply the cancel rules: keep only delivered segments, drop the unexecuted terminal."""
        delivered = self._delivered.get(turn.ctx.turn_id)
        kept = [s for s in turn.segments if delivered is None or s.seq <= delivered]
        turn.flags.append("cancelled")
        assistant = self._assistant_stored(turn, kept, cancelled=True)
        messages = [user_msg]
        if assistant.message.content:  # an empty assistant message is dropped entirely
            messages.append(assistant)
        await self._save(turn, messages, self._usage_record(turn, cancelled=True))


def _recent_segments(stored: list[StoredMessage], current: list[Segment]) -> list[Segment]:
    """Segments of the last assistant turns plus the current one, for the Vietnamese ratio."""
    assistants = [m for m in stored if m.message.role == "assistant"][-RECENT_RATIO_TURNS:]
    segments: list[Segment] = []
    for item in assistants:
        parser = LangTagParser()
        for part in item.message.content:
            if isinstance(part, TextPart):
                segments += parser.feed(part.text)
        segments += parser.finish()
    return [*segments, *current]
