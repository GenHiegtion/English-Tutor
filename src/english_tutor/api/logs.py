"""JSON logs on stdout for the service, and one access line per request (no PII, no bodies)."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import UTC, datetime

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from english_tutor.bootstrap import LOG_LEVEL_ENV

ACCESS_LOGGER = "english_tutor.access"
DEFAULT_SERVICE_LOG_LEVEL = "INFO"
_RECORD_FIELDS = frozenset(vars(logging.makeLogRecord({})))


class JsonFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, message, extras and the traceback."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry.update({k: v for k, v in vars(record).items() if k not in _RECORD_FIELDS})
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_service_logging() -> None:
    """Root handler with JSON lines; ``ENGLISH_TUTOR_LOG_LEVEL`` (default INFO) for our loggers."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    level = os.environ.get(LOG_LEVEL_ENV, DEFAULT_SERVICE_LOG_LEVEL).upper()
    if level not in logging.getLevelNamesMapping():
        level = DEFAULT_SERVICE_LOG_LEVEL
    logging.getLogger("english_tutor").setLevel(level)


class AccessLogMiddleware:
    """Logs method, route template, status and duration; never query strings or bodies."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.logger = logging.getLogger(ACCESS_LOGGER)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            self.logger.info(
                "%s %s %s",
                scope["method"],
                getattr(route, "path", "unmatched"),
                status,
                extra={
                    "status": status,
                    "ms": int((time.monotonic() - started) * 1000),
                    "caller": scope.get("state", {}).get("caller"),
                },
            )
