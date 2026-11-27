"""Runs teacher turns for the HTTP layer: one turn at a time per session, safe to abandon.

Each turn runs in its own asyncio task and the response only reads its events from a queue.
When the client disconnects (barge-in, closed tab), the server cancels the response; the turn
task is then cancelled separately, outside the request's cancel scope, so the runner can still
await its database write of the cancelled turn (only the segments reported as delivered are
kept). A turn whose ``turn_end`` was sent is complete and is never cancelled: the runner writes
it after ``turn_end``, so clients may stop reading there, and the session stays locked until the
write is done. All state here lives in this process: the service must run as a single instance.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass

from english_tutor.teacher.context import TurnContext
from english_tutor.teacher.events import TeacherEvent, TurnFailed
from english_tutor.teacher.runner import TeacherService

logger = logging.getLogger(__name__)

SHUTDOWN_GRACE_S = 10.0  # on redeploy, give running turns this long to finish and save


class TurnBusy(Exception):
    """Another turn of the same session is still running."""


@dataclass
class _SessionLock:
    lock: asyncio.Lock
    users: int = 0  # requests holding or waiting for the lock; the entry is dropped at zero


class TurnStreams:
    """Starts turns, serialises them per session and keeps them alive until they are saved."""

    def __init__(self, service: TeacherService, lock_wait_s: float) -> None:
        self._service = service
        self._lock_wait_s = lock_wait_s
        self._locks: dict[str, _SessionLock] = {}
        self._running: dict[str, str] = {}  # turn_id -> session_id
        self._tasks: set[asyncio.Task[None]] = set()

    def is_running(self, session_id: str, turn_id: str) -> bool:
        """True while ``turn_id`` of ``session_id`` is being generated or saved."""
        return self._running.get(turn_id) == session_id

    async def start(self, ctx: TurnContext) -> AsyncGenerator[TeacherEvent]:
        """Start the turn and return its event stream; TurnBusy if the session stays busy."""
        entry = self._locks.setdefault(ctx.session_id, _SessionLock(asyncio.Lock()))
        entry.users += 1
        try:
            await asyncio.wait_for(entry.lock.acquire(), self._lock_wait_s)
        except TimeoutError:
            self._leave(ctx.session_id, entry)
            raise TurnBusy(ctx.session_id) from None
        queue: asyncio.Queue[TeacherEvent | None] = asyncio.Queue()
        self._running[ctx.turn_id] = ctx.session_id
        task = asyncio.create_task(self._produce(ctx, queue, entry), name=f"turn-{ctx.turn_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return self._consume(queue, task)

    async def shutdown(self) -> None:
        """Wait for running turns (they save on their own); cancel what is left after the grace."""
        if not self._tasks:
            return
        tasks = list(self._tasks)
        _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_S)
        for task in pending:
            task.cancel()
        if pending:
            logger.warning("cancelled %d turns still running at shutdown", len(pending))
            await asyncio.wait(pending, timeout=SHUTDOWN_GRACE_S)

    async def _produce(
        self, ctx: TurnContext, queue: asyncio.Queue[TeacherEvent | None], entry: _SessionLock
    ) -> None:
        try:
            async with aclosing(self._service.run_turn(ctx)) as events:
                async for event in events:
                    queue.put_nowait(event)
        except Exception:
            logger.error(
                "teacher turn crashed: session=%s turn=%s",
                ctx.session_id,
                ctx.turn_id,
                exc_info=True,
            )
            queue.put_nowait(TurnFailed(turn_id=ctx.turn_id, reason="internal_error"))
        finally:
            queue.put_nowait(None)
            self._running.pop(ctx.turn_id, None)
            entry.lock.release()
            self._leave(ctx.session_id, entry)

    @staticmethod
    async def _consume(
        queue: asyncio.Queue[TeacherEvent | None], task: asyncio.Task[None]
    ) -> AsyncGenerator[TeacherEvent]:
        ended = False  # turn_end went out: the turn is complete, only its database write is left
        try:
            while (event := await queue.get()) is not None:
                ended = ended or event.kind == "turn_end"  # set before yielding: the client
                yield event  # may leave as soon as it has turn_end
        finally:
            if not ended and not task.done():
                task.cancel()  # the client left mid-turn: the runner stores the cancelled turn

    def _leave(self, session_id: str, entry: _SessionLock) -> None:
        entry.users -= 1
        if entry.users == 0 and self._locks.get(session_id) is entry:
            del self._locks[session_id]
