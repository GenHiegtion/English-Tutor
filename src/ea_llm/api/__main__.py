"""Production entry point: ``python -m ea_llm.api`` (listens on ``$HOST``:``$PORT``)."""

from __future__ import annotations

import os

import uvicorn

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
GRACEFUL_SHUTDOWN_S = 15  # running turns get SHUTDOWN_GRACE_S of it to finish and save


def main() -> None:
    """Serve the production app; the access log comes from AccessLogMiddleware (JSON)."""
    uvicorn.run(
        "ea_llm.api.app:production_app",
        factory=True,
        host=os.environ.get("HOST", DEFAULT_HOST),
        port=int(os.environ.get("PORT", DEFAULT_PORT)),
        access_log=False,
        log_config=None,  # keep the JSON logging configured by production_app
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
    )


if __name__ == "__main__":
    main()
